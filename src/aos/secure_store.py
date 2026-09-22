"""Windows-backed secure provider credential store for AOS.

Secrets are stored as Windows Generic Credentials under the current user account.
Secret values are never written to repository files, AOS runtime JSON, logs, or HTTP
responses. The module is import-safe on non-Windows platforms so CI can exercise the
non-secret control flow.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Dict, Optional

PROVIDER_ENV_VARS: Dict[str, str] = {
    "NVIDIA": "NVIDIA_API_KEY",
    "GEMINI": "GEMINI_API_KEY",
    "GROQ": "GROQ_API_KEY",
    "OPENAI": "OPENAI_API_KEY",
    "GITHUB": "GITHUB_TOKEN",
    "CLOUDFLARE": "CLOUDFLARE_API_TOKEN",
    "OPENROUTER": "OPENROUTER_API_KEY",
    "CEREBRAS": "CEREBRAS_API_KEY",
    "HUGGINGFACE": "HF_TOKEN",
    "HF": "HF_TOKEN",
    "FREELLMAPI_LOCAL": "FREELLMAPI_LOCAL_API_KEY",
}

PROVIDER_ID_ENV_VARS: Dict[str, str] = {
    "nemotron": "NVIDIA_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "openrouter_free": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "huggingface_router": "HF_TOKEN",
    "openai": "OPENAI_API_KEY",
    "openai_paid_safety": "OPENAI_API_KEY",
    "freellmapi_local": "FREELLMAPI_LOCAL_API_KEY",
}


_TARGET_PREFIX = "AOS/Provider/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_SECRET_BYTES = 2048


def _normalize_provider(provider: str, allow_custom: bool = False) -> str:
    value = str(provider).strip().upper()
    if value not in PROVIDER_ENV_VARS and not allow_custom:
        raise ValueError(f"Unsupported provider: {provider}")
    if not value or not all(c.isalnum() or c in ("_", "-") for c in value):
        raise ValueError(f"Invalid provider name: {provider}")
    return value


def _target(provider: str, env_var: Optional[str] = None) -> str:
    normalized = _normalize_provider(provider, allow_custom=bool(env_var))
    target_key = env_var or PROVIDER_ENV_VARS[normalized]
    return f"{_TARGET_PREFIX}{target_key}"


def resolve_credential_env_var(
    provider_id: str,
    configured_env_var: Optional[str] = None,
) -> Optional[str]:
    """Resolve a runtime provider id to its secure credential identity."""
    if configured_env_var:
        return str(configured_env_var).strip() or None
    provider = str(provider_id).strip()
    mapped = PROVIDER_ID_ENV_VARS.get(provider.lower())
    if mapped:
        return mapped
    return PROVIDER_ENV_VARS.get(provider.upper())


def secure_store_identity(
    provider_id: str,
    configured_env_var: Optional[str] = None,
) -> Optional[str]:
    """Return the normalized Windows-vault identity without reading its value."""
    env_var = resolve_credential_env_var(provider_id, configured_env_var)
    if not env_var:
        return None
    for identity, known_env_var in PROVIDER_ENV_VARS.items():
        if known_env_var == env_var:
            return identity
    return str(provider_id).strip().upper() or None


def credential_is_configured(
    provider_id: str,
    configured_env_var: Optional[str] = None,
    *,
    presence: Optional[Dict[str, bool]] = None,
) -> bool:
    """Report credential presence without returning or logging a secret value."""
    env_var = resolve_credential_env_var(provider_id, configured_env_var)
    identity = secure_store_identity(provider_id, env_var)
    if not env_var or not identity:
        return False
    if os.environ.get(env_var):
        return True
    if presence is not None:
        return bool(presence.get(env_var) or presence.get(identity))
    try:
        return bool(read_provider_secret(identity, env_var=env_var))
    except OSError:
        return False


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is unavailable on this platform")
    dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    dll.CredFree.restype = None
    return dll


def write_provider_secret(provider: str, secret: str, env_var: Optional[str] = None) -> None:
    normalized = _normalize_provider(provider, allow_custom=bool(env_var))
    if not isinstance(secret, str):
        raise ValueError("Provider secret must be text")
    secret = secret.strip()
    if len(secret) < 8:
        raise ValueError("Provider secret is unexpectedly short")
    raw = secret.encode("utf-8")
    if len(raw) > _MAX_SECRET_BYTES:
        raise ValueError("Provider secret is too large")

    dll = _advapi32()
    buffer = ctypes.create_string_buffer(raw)
    credential = _CREDENTIALW()
    credential.Flags = 0
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = _target(normalized, env_var=env_var)
    credential.Comment = "AOS local reasoning provider credential"
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.AttributeCount = 0
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = "AOS"

    if not dll.CredWriteW(ctypes.byref(credential), 0):
        err = ctypes.get_last_error()
        raise OSError(err, "CredWriteW failed")


def read_provider_secret(provider: str, env_var: Optional[str] = None) -> Optional[str]:
    normalized = _normalize_provider(provider, allow_custom=bool(env_var))
    if os.name != "nt":
        return None
    dll = _advapi32()
    ptr = ctypes.POINTER(_CREDENTIALW)()
    if not dll.CredReadW(_target(normalized, env_var=env_var), _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise OSError(err, "CredReadW failed")
    try:
        cred = ptr.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-8")
    finally:
        dll.CredFree(ptr)


def delete_provider_secret(provider: str, env_var: Optional[str] = None) -> bool:
    normalized = _normalize_provider(provider, allow_custom=bool(env_var))
    if os.name != "nt":
        return False
    dll = _advapi32()
    if dll.CredDeleteW(_target(normalized, env_var=env_var), _CRED_TYPE_GENERIC, 0):
        return True
    err = ctypes.get_last_error()
    if err == _ERROR_NOT_FOUND:
        return False
    raise OSError(err, "CredDeleteW failed")


def provider_presence() -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for provider, env_name in PROVIDER_ENV_VARS.items():
        present = bool(os.environ.get(env_name))
        if not present:
            try:
                present = bool(read_provider_secret(provider))
            except OSError:
                present = False
        result[provider] = present
    return result


def hydrate_environment(*, overwrite: bool = True) -> Dict[str, bool]:
    """Load stored provider secrets into this process environment.

    Only the in-memory environment is populated. No secret values are returned.
    Existing values are retained when overwrite=False.
    """
    result: Dict[str, bool] = {}
    for provider, env_name in PROVIDER_ENV_VARS.items():
        current = os.environ.get(env_name)
        if current and not overwrite:
            result[provider] = True
            continue
        try:
            secret = read_provider_secret(provider)
        except OSError:
            secret = None
        if secret:
            os.environ[env_name] = secret
            result[provider] = True
        else:
            result[provider] = bool(current)
    return result
