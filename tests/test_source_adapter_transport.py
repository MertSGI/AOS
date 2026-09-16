"""Offline and deterministic transport tests for ProjectSourceAdapter native system trust TLS context."""

import io
import ssl
import ssl as _ssl_mod
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import truststore
from aos.source_adapter import ProjectSourceAdapter, _create_system_trust_tls_context


def test_default_context_construction():
    """Verify default ProjectSourceAdapter context uses truststore.SSLContext with PROTOCOL_TLS_CLIENT."""
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main")
    assert isinstance(adapter.tls_context, truststore.SSLContext)
    assert adapter.tls_context.protocol == _ssl_mod.PROTOCOL_TLS_CLIENT


def test_explicit_context_injection():
    """Verify injected custom ssl_context is preserved unchanged."""
    custom_ctx = ssl.create_default_context()
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main", tls_context=custom_ctx)
    assert adapter.tls_context is custom_ctx


@patch("urllib.request.urlopen")
def test_resolve_ref_to_sha_passes_explicit_context(mock_urlopen):
    """Verify resolve_ref_to_sha passes self.tls_context to urllib.request.urlopen."""
    dummy_ctx = ssl.create_default_context()
    adapter = ProjectSourceAdapter("MertSGI/AOS", "feature-branch", tls_context=dummy_ctx)

    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"commit": {"sha": "0123456789abcdef0123456789abcdef01234567"}}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    sha = adapter.resolve_ref_to_sha()
    assert sha == "0123456789abcdef0123456789abcdef01234567"
    assert mock_urlopen.call_count == 1
    _, kwargs = mock_urlopen.call_args
    assert kwargs.get("context") is dummy_ctx


@patch("urllib.request.urlopen")
def test_resolve_exact_revision_passes_explicit_context(mock_urlopen):
    """Verify resolve_exact_revision passes self.tls_context to urllib.request.urlopen."""
    dummy_ctx = ssl.create_default_context()
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main", tls_context=dummy_ctx)

    target_sha = "0123456789abcdef0123456789abcdef01234567"
    mock_resp = MagicMock()
    mock_resp.read.return_value = f'{{"sha": "{target_sha}"}}'.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    res_sha = adapter.resolve_exact_revision(target_sha)
    assert res_sha == target_sha
    assert mock_urlopen.call_count == 1
    _, kwargs = mock_urlopen.call_args
    assert kwargs.get("context") is dummy_ctx


@patch("urllib.request.urlopen")
def test_fetch_file_at_sha_passes_explicit_context(mock_urlopen):
    """Verify fetch_file_at_sha passes self.tls_context to urllib.request.urlopen."""
    dummy_ctx = ssl.create_default_context()
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main", tls_context=dummy_ctx)

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"sample file content"
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    content = adapter.fetch_file_at_sha("docs/STATE.json", "0123456789abcdef0123456789abcdef01234567")
    assert content == "sample file content"
    assert mock_urlopen.call_count == 1
    _, kwargs = mock_urlopen.call_args
    assert kwargs.get("context") is dummy_ctx


@patch("aos.source_adapter.subprocess.run")
@patch("urllib.request.urlopen")
def test_resolve_ref_uses_read_only_git_transport_on_api_rate_limit(mock_urlopen, mock_run):
    api_error = urllib.error.HTTPError("https://api.github.com/example", 403, "rate limit", {}, None)
    mock_urlopen.side_effect = api_error
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="0123456789abcdef0123456789abcdef01234567\trefs/heads/control/main\n",
        stderr="",
    )
    adapter = ProjectSourceAdapter("MertSGI/AOS", "control/main", tls_context=ssl.create_default_context())

    assert adapter.resolve_ref_to_sha() == "0123456789abcdef0123456789abcdef01234567"
    args, kwargs = mock_run.call_args
    assert args[0] == [
        "git", "ls-remote", "https://github.com/MertSGI/AOS.git", "refs/heads/control/main"
    ]
    assert kwargs["check"] is True
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


@patch("urllib.request.urlopen")
def test_resolve_exact_revision_uses_immutable_html_on_api_rate_limit(mock_urlopen):
    api_error = urllib.error.HTTPError("https://api.github.com/example", 429, "rate limit", {}, None)
    html_response = MagicMock()
    html_response.status = 200
    html_response.__enter__.return_value = html_response
    mock_urlopen.side_effect = [api_error, html_response]
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main", tls_context=ssl.create_default_context())
    target_sha = "0123456789abcdef0123456789abcdef01234567"

    assert adapter.resolve_exact_revision(target_sha) == target_sha
    assert mock_urlopen.call_count == 2
    fallback_request = mock_urlopen.call_args_list[1].args[0]
    assert fallback_request.full_url == f"https://github.com/MertSGI/AOS/commit/{target_sha}"


@patch("urllib.request.urlopen")
def test_tls_certificate_failure_remains_fail_closed(mock_urlopen):
    """Verify SSLCertVerificationError yields fail-closed RuntimeError and no success fallback."""
    adapter = ProjectSourceAdapter("MertSGI/AOS", "main")
    mock_urlopen.side_effect = _ssl_mod.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    with pytest.raises(RuntimeError) as exc_info:
        adapter.resolve_ref_to_sha()

    assert "Failed to resolve ref" in str(exc_info.value)
    assert "CERTIFICATE_VERIFY_FAILED" in str(exc_info.value)


def test_no_process_global_truststore_injection():
    """Verify source_adapter source code does NOT call truststore.inject_into_ssl()."""
    import inspect
    import aos.source_adapter as sa_module

    source_text = inspect.getsource(sa_module)
    assert "inject_into_ssl" not in source_text


def test_no_insecure_ssl_bypass_patterns():
    """Verify source_adapter source code contains no insecure SSL bypass idioms."""
    import inspect
    import aos.source_adapter as sa_module

    source_text = inspect.getsource(sa_module)
    for forbidden in (
        "_create_unverified_context",
        "CERT_NONE",
        "check_hostname = False",
        "check_hostname=False",
        "PYTHONHTTPSVERIFY",
    ):
        assert forbidden not in source_text, f"Forbidden SSL bypass pattern '{forbidden}' found in source_adapter.py"
