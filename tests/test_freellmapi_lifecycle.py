"""Deterministic tests for the bounded FreeLLMAPI local process envelope."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from aos.freellmapi_lifecycle import (
    APPROVED_FREE_BINDINGS,
    ENCRYPTION_KEY_ENV_VAR,
    FREELLMAPI_UPSTREAM_SHA,
    FreeLLMAPIConfig,
    FreeLLMAPILifecycle,
    FreeLLMAPILifecycleError,
    build_declarative_config,
    build_process_spec,
    default_data_dir,
)
from aos.providers.freellmapi_local import FreeLLMAPIReadiness


def _secret() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "pinned-source"
    entrypoint = install / "server" / "dist" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    (install / "package.json").write_text("{}", encoding="utf-8")
    entrypoint.write_text("// deterministic test fixture", encoding="utf-8")
    return install, tmp_path / "operator-data"


def _reader(values: Dict[str, str]):
    def read(identity: str, env_var: Optional[str]) -> Optional[str]:
        return values.get(env_var or identity)

    return read


class _FakeProcess:
    def __init__(self, pid: int, *, exited: bool = False, timeout_once: bool = False) -> None:
        self.pid = pid
        self.running = not exited
        self.timeout_once = timeout_once
        self.terminated = False
        self.killed = False
        self.closed = False
        self.wait_calls = 0

    def poll(self) -> Optional[int]:
        return None if self.running else 1

    def terminate_tree(self) -> None:
        self.terminated = True
        if not self.timeout_once:
            self.running = False

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.timeout_once and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.running = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def close(self) -> None:
        self.closed = True


class _Spawner:
    def __init__(self, *, exited: bool = False, timeout_once: bool = False) -> None:
        self.exited = exited
        self.timeout_once = timeout_once
        self.calls: list[Dict[str, Any]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, command: tuple[str, ...], **kwargs: Any) -> _FakeProcess:
        # Popen consumes/copies its environment before returning. The fake does
        # the same so the source mapping can be scrubbed immediately afterward.
        call = {"command": tuple(command), **kwargs, "env": dict(kwargs["env"])}
        self.calls.append(call)
        process = _FakeProcess(
            4100 + len(self.calls),
            exited=self.exited,
            timeout_once=self.timeout_once,
        )
        self.processes.append(process)
        return process


def test_defaults_are_fixed_loopback_and_local_app_data(tmp_path: Path) -> None:
    data = default_data_dir({"LOCALAPPDATA": str(tmp_path)})
    assert data == tmp_path / "AOS" / "freellmapi-local" / "data"

    config = FreeLLMAPIConfig(data_dir=data)
    assert config.host == "127.0.0.1"
    assert config.port == 3000
    assert config.base_url == "http://127.0.0.1:3000/v1"


def test_static_boundary_rejects_non_loopback_and_checkout_local_data(tmp_path: Path) -> None:
    install = tmp_path / "source"
    with pytest.raises(FreeLLMAPILifecycleError, match="exactly 127.0.0.1"):
        FreeLLMAPIConfig(install_path=install, data_dir=tmp_path / "data", host="0.0.0.0").validate_static()
    with pytest.raises(FreeLLMAPILifecycleError, match="outside"):
        FreeLLMAPIConfig(install_path=install, data_dir=install / "data").validate_static()


def test_bridge_only_includes_approved_free_credentials_in_memory(tmp_path: Path) -> None:
    install, data = _checkout(tmp_path)
    encryption_key = _secret()
    nvidia_key = f"nv-{_secret()}"
    openai_key = f"paid-{_secret()}"
    gateway_key = f"freellmapi-{_secret()}"
    source = {
        "PATH": "bounded-test-path",
        ENCRYPTION_KEY_ENV_VAR: encryption_key,
        "NVIDIA_API_KEY": nvidia_key,
        "OPENAI_API_KEY": openai_key,
        "FREELLMAPI_LOCAL_API_KEY": gateway_key,
    }
    spec = build_process_spec(
        FreeLLMAPIConfig(install_path=install, data_dir=data),
        environ=source,
        credential_reader=_reader({}),
    )

    child_config = json.loads(spec.environment["FREEAPI_CONFIG_JSON"])
    assert child_config["routing"] == {
        "strategy": "reliable",
        "keySelectionStrategy": "auto",
    }
    assert child_config["keys"] == [{
        "platform": "nvidia",
        "key": nvidia_key,
        "label": "aos-bridge-nvidia",
        "enabled": True,
    }]
    assert "openai" not in {binding.freellmapi_platform for binding in APPROVED_FREE_BINDINGS}
    assert spec.environment["ENCRYPTION_KEY"] == encryption_key
    assert "NVIDIA_API_KEY" not in spec.environment
    assert "OPENAI_API_KEY" not in spec.environment
    assert "FREELLMAPI_LOCAL_API_KEY" not in spec.environment
    assert spec.environment["HOST"] == "127.0.0.1"
    assert spec.environment["PORT"] == "3000"

    rendered = json.dumps(spec.to_telemetry(), sort_keys=True) + repr(spec)
    for secret in (encryption_key, nvidia_key, openai_key, gateway_key):
        assert secret not in rendered
    spec.clear_sensitive()
    assert spec.sensitive_cleared
    assert "ENCRYPTION_KEY" not in spec.environment
    assert "FREEAPI_CONFIG_JSON" not in spec.environment


def test_secure_store_bridge_and_cloudflare_pair_are_supported() -> None:
    token = f"cf-{_secret()}"
    config, platforms = build_declarative_config(
        environ={"CLOUDFLARE_ACCOUNT_ID": "account-for-test"},
        credential_reader=_reader({"CLOUDFLARE_API_TOKEN": token}),
    )
    cloudflare = next(row for row in config["keys"] if row["platform"] == "cloudflare")
    assert cloudflare["key"] == f"account-for-test:{token}"
    assert platforms == ["cloudflare"]


@pytest.mark.parametrize("missing_value", [None, "not-hex", "a" * 63])
def test_missing_or_invalid_encryption_key_fails_closed_without_value(
    tmp_path: Path,
    missing_value: Optional[str],
) -> None:
    install, data = _checkout(tmp_path)
    values = {} if missing_value is None else {ENCRYPTION_KEY_ENV_VAR: missing_value}
    with pytest.raises(FreeLLMAPILifecycleError) as caught:
        build_process_spec(
            FreeLLMAPIConfig(install_path=install, data_dir=data),
            environ={},
            credential_reader=_reader(values),
        )
    assert "64-hex" in str(caught.value)
    if missing_value:
        assert missing_value not in str(caught.value)


def test_checkout_must_match_pin_and_be_built_before_spawn(tmp_path: Path) -> None:
    install, data = _checkout(tmp_path)
    spawner = _Spawner()
    lifecycle = FreeLLMAPILifecycle(
        FreeLLMAPIConfig(install_path=install, data_dir=data),
        environ={ENCRYPTION_KEY_ENV_VAR: _secret()},
        credential_reader=_reader({}),
        revision_reader=lambda _path: "0" * 40,
        spawner=spawner,
    )
    with pytest.raises(FreeLLMAPILifecycleError, match="pinned upstream SHA"):
        lifecycle.start()
    assert spawner.calls == []
    assert not data.exists()

    (install / "server" / "dist" / "index.js").unlink()
    lifecycle = FreeLLMAPILifecycle(
        FreeLLMAPIConfig(install_path=install, data_dir=data),
        environ={ENCRYPTION_KEY_ENV_VAR: _secret()},
        credential_reader=_reader({}),
        revision_reader=lambda _path: FREELLMAPI_UPSTREAM_SHA,
        spawner=spawner,
    )
    with pytest.raises(FreeLLMAPILifecycleError, match="not built"):
        lifecycle.start()
    assert spawner.calls == []


def test_explicit_start_restart_stop_owns_only_spawned_process(tmp_path: Path) -> None:
    install, data = _checkout(tmp_path)
    encryption_key = _secret()
    provider_key = f"groq-{_secret()}"
    spawner = _Spawner(timeout_once=True)
    lifecycle = FreeLLMAPILifecycle(
        FreeLLMAPIConfig(install_path=install, data_dir=data, stop_timeout_seconds=0.01),
        environ={ENCRYPTION_KEY_ENV_VAR: encryption_key},
        credential_reader=_reader({"GROQ_API_KEY": provider_key}),
        revision_reader=lambda _path: FREELLMAPI_UPSTREAM_SHA,
        spawner=spawner,
    )

    started = lifecycle.start()
    assert started.state == "STARTING"
    assert started.to_telemetry()["production"] == "NO_GO"
    assert data.is_dir()
    assert list(data.iterdir()) == []
    assert spawner.calls[0]["cwd"] == str(install.resolve())
    assert Path(spawner.calls[0]["command"][-1]) == install.resolve() / "server" / "dist" / "index.js"
    assert json.loads(spawner.calls[0]["env"]["FREEAPI_CONFIG_JSON"])["keys"][0]["key"] == provider_key

    already_running = lifecycle.start()
    assert already_running.reason == "already_running"
    assert len(spawner.calls) == 1

    restarted = lifecycle.restart()
    assert restarted.state == "STARTING"
    assert len(spawner.calls) == 2
    assert spawner.processes[0].terminated
    assert spawner.processes[0].killed
    assert spawner.processes[0].closed

    stopped = lifecycle.stop()
    assert stopped.reason == "operator_stop"
    assert lifecycle.stop().reason == "not_running"
    for secret in (encryption_key, provider_key):
        assert secret not in json.dumps(started.to_telemetry())
        assert secret not in json.dumps(stopped.to_telemetry())


def test_immediate_child_exit_fails_closed_and_releases_owner(tmp_path: Path) -> None:
    install, data = _checkout(tmp_path)
    spawner = _Spawner(exited=True)
    lifecycle = FreeLLMAPILifecycle(
        FreeLLMAPIConfig(install_path=install, data_dir=data),
        environ={ENCRYPTION_KEY_ENV_VAR: _secret()},
        credential_reader=_reader({}),
        revision_reader=lambda _path: FREELLMAPI_UPSTREAM_SHA,
        spawner=spawner,
    )
    with pytest.raises(FreeLLMAPILifecycleError, match="exited during startup"):
        lifecycle.start()
    assert spawner.processes[0].closed


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [
        (FreeLLMAPIReadiness(True, True, "ready", 200, 2), ("RUNNING", True, True, False)),
        (
            FreeLLMAPIReadiness(True, False, "no_upstreams_configured", 503, 0),
            ("DEGRADED", True, False, False),
        ),
        (
            FreeLLMAPIReadiness(True, False, "all_upstreams_rate_limited", 503, 0),
            ("DEGRADED", True, False, True),
        ),
        (
            FreeLLMAPIReadiness(False, False, "connection_refused", None),
            ("UNAVAILABLE", False, False, False),
        ),
    ],
)
def test_probe_keeps_service_health_eligibility_and_quota_distinct(
    readiness: FreeLLMAPIReadiness,
    expected: tuple[str, bool, bool, bool],
) -> None:
    calls = 0

    def probe() -> FreeLLMAPIReadiness:
        nonlocal calls
        calls += 1
        return readiness

    status = FreeLLMAPILifecycle(readiness_probe=probe).probe()
    assert (status.state, status.service_available, status.eligible, status.quota_limited) == expected
    assert calls == 1
    assert status.to_telemetry()["secrets_exposed"] is False
