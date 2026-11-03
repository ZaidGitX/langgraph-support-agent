from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from orchestrator import connect, credentials, redaction, vault

FAKE_LOGIN = Path(__file__).with_name("fakes") / "fake_login.py"


def _fake_value(tag: str) -> str:
    # Built at runtime so no credential-shaped literal sits on one line.
    return "vault-test-" + tag + "-" + "0123456789abcdef"


def test_round_trip_lists_names_only_and_exports_to_the_process(monkeypatch):
    value = _fake_value("jenkins")
    stored = vault.set_many({"JENKINS_API_TOKEN": value, "JENKINS_URL": "http://127.0.0.1:8080"})
    assert stored == ["JENKINS_API_TOKEN", "JENKINS_URL"]
    assert vault.names() == ["JENKINS_API_TOKEN", "JENKINS_URL"]
    assert os.environ["JENKINS_API_TOKEN"] == value
    assert credentials.is_present("JENKINS_API_TOKEN")
    # The file on disk is ciphertext with owner-only permissions; the value is not in it.
    encrypted = vault.vault_dir() / vault.FILE_NAME
    assert value.encode() not in encrypted.read_bytes()
    assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600
    assert stat.S_IMODE((vault.vault_dir() / vault.KEY_FILE_NAME).stat().st_mode) == 0o600
    # Redaction covers the value wherever it appears, including a config value.
    assert value not in redaction.redact(f"token={value} url=http://127.0.0.1:8080")
    assert "8080" not in redaction.redact("http://127.0.0.1:8080")
    assert vault.delete("JENKINS_API_TOKEN") is True
    assert vault.delete("JENKINS_API_TOKEN") is False
    assert "JENKINS_API_TOKEN" not in os.environ
    monkeypatch.delenv("JENKINS_URL", raising=False)


def test_only_registry_names_and_real_values_are_accepted():
    with pytest.raises(vault.VaultError, match="not a name"):
        vault.set_many({"NOT_A_NAME": "x" * 10})
    with pytest.raises(vault.VaultError, match="non-empty"):
        vault.set_many({"SNYK_TOKEN": "   "})
    with pytest.raises(vault.VaultError, match="longer"):
        vault.set_many({"SNYK_TOKEN": "x" * (vault.MAX_VALUE_CHARS + 1)})
    assert vault.names() == []


def test_a_configured_key_is_used_and_a_bad_key_is_refused(monkeypatch):
    key = vault.generate_key()
    monkeypatch.setenv("ORCHESTRATOR_VAULT_KEY", key)
    vault.set_many({"SNYK_TOKEN": _fake_value("snyk")})
    assert not (vault.vault_dir() / vault.KEY_FILE_NAME).exists()
    monkeypatch.setenv("ORCHESTRATOR_VAULT_KEY", "not-a-key")
    with pytest.raises(vault.VaultError, match="Fernet"):
        vault.names()


def test_export_env_overrides_an_older_variable(monkeypatch):
    monkeypatch.setenv("SNYK_TOKEN", "older-value-from-dotenv")
    vault.set_many({"SNYK_TOKEN": _fake_value("new")})
    assert os.environ["SNYK_TOKEN"] == _fake_value("new")
    scratch: dict[str, str] = {}
    assert vault.export_env(target_env=scratch) == ["SNYK_TOKEN"]
    assert scratch["SNYK_TOKEN"] == _fake_value("new")


def test_readiness_treats_a_verified_login_as_ready(monkeypatch):
    before = credentials.group_status("aws")
    assert before["mode"] == "plan" and before["authenticated"] is None and before["login"] is True
    connect._VERIFIED["aws"] = {"ok": True, "checked_at": "2026-11-03T00:00:00Z", "detail": "ok"}
    after = credentials.group_status("aws")
    assert after["authenticated"] is True and after["checked_at"]
    assert after["mode"] == ("apply" if after["cli"] else "plan")
    connect._VERIFIED["aws"] = {"ok": False, "checked_at": "2026-11-03T00:00:00Z", "detail": "no"}
    assert credentials.group_status("aws")["mode"] == "plan"
    text = json.dumps(credentials.readiness())
    assert "vault-test" not in text


def test_login_flow_parses_the_url_and_code_then_verifies(monkeypatch):
    spec = {"argv": [sys.executable, str(FAKE_LOGIN)], "url_pattern": r"https://github\.com/login/device", "code_pattern": r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", "input": "", "requires": []}
    monkeypatch.setattr(connect, "login_spec", lambda group: spec if group == "github" else None)
    monkeypatch.setattr(connect, "verify_spec", lambda group: {"argv": [sys.executable, "-c", "print('ok')"]} if group == "github" else None)
    flow = connect.start_login("github")
    deadline = time.time() + 10
    while flow["status"] not in {"done", "failed", "expired"} and time.time() < deadline:
        time.sleep(0.05)
        flow = connect.flow_status(flow["id"])
    assert flow["status"] == "done" and flow["exit_code"] == 0
    assert flow["url"] == "https://github.com/login/device" and flow["code"] == "ABCD-1234"
    assert "fake-user" in flow["message"]
    deadline = time.time() + 5
    while connect.verified("github") is None and time.time() < deadline:
        time.sleep(0.05)
    assert connect.verified("github")["ok"] is True


def test_login_flow_accepts_pasted_input_and_reports_failure(monkeypatch):
    spec = {"argv": [sys.executable, str(FAKE_LOGIN), "--needs-input", "--fail"], "url_pattern": r"https://\S+", "code_pattern": "", "input": "verification code", "requires": []}
    monkeypatch.setattr(connect, "login_spec", lambda group: spec if group == "gcp" else None)
    flow = connect.start_login("gcp")
    deadline = time.time() + 10
    while not flow["needs_input"] and flow["status"] not in {"done", "failed"} and time.time() < deadline:
        time.sleep(0.05)
        flow = connect.flow_status(flow["id"])
    assert flow["needs_input"] is True and flow["input_label"] == "verification code"
    flow = connect.provide_input(flow["id"], "4/0AbCdEf")
    deadline = time.time() + 10
    while flow["status"] not in {"done", "failed", "expired"} and time.time() < deadline:
        time.sleep(0.05)
        flow = connect.flow_status(flow["id"])
    assert flow["status"] == "failed" and flow["exit_code"] == 1
    assert "received 9 characters" in flow["message"]
    with pytest.raises(connect.ConnectError):
        connect.provide_input(flow["id"], "again")


def test_can_login_explains_why_not(monkeypatch):
    ok, reason = connect.can_login("jenkins")
    assert ok is False and "vault form" in reason
    monkeypatch.setattr(connect, "login_spec", lambda group: {"argv": ["definitely-not-a-cli-xyz"], "requires": []})
    ok, reason = connect.can_login("aws")
    assert ok is False and "not installed" in reason
    monkeypatch.setattr(connect, "login_spec", lambda group: {"argv": [sys.executable], "requires": ["AWS_PROFILE"]})
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    ok, reason = connect.can_login("aws")
    assert ok is False and "AWS_PROFILE" in reason


def test_verify_reports_a_missing_name_without_running_anything(monkeypatch):
    monkeypatch.delenv("SSH_HOST", raising=False)
    monkeypatch.delenv("SSH_USER", raising=False)
    result = connect.verify("ssh")
    assert result["ok"] is False and "SSH_USER" in result["detail"]
    monkeypatch.setattr(connect, "verify_spec", lambda group: {"http": "$JENKINS_URL/api/json", "auth": ["JENKINS_USER", "JENKINS_API_TOKEN"]})
    monkeypatch.delenv("JENKINS_URL", raising=False)
    result = connect.verify("jenkins")
    assert result["ok"] is False and "URL name is not set" in result["detail"]
