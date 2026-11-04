from __future__ import annotations

import os

import pytest

from orchestrator import credentials

APP_NAMES = {"OPENAI_API_KEY", "OPENAI_MODEL", "TYPESAFE_API_KEY", "TYPESAFE_MODEL"}


@pytest.fixture(autouse=True)
def _orchestrator_stays_manual(monkeypatch, tmp_path_factory):
    monkeypatch.delenv("ORCHESTRATOR_AUTOSTART", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_DISPATCH", raising=False)
    # Sprint 4: the vault, the dashboard token, and cached login checks never leak between
    # tests or from a developer's var/vault into a test.
    monkeypatch.setenv("ORCHESTRATOR_VAULT_PATH", str(tmp_path_factory.mktemp("vault")))
    monkeypatch.delenv("ORCHESTRATOR_VAULT_KEY", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_UI_TOKEN", raising=False)
    from orchestrator import connect, redaction

    connect.reset()
    redaction.forget_values()
    # Sprint 3: episodes and the retrieval index stay inside each test's tmp root, offline,
    # and a developer's shell never changes readiness: every cloud, scanner, and toggle name
    # from the credential registry is unset (the app keys from .env are left alone).
    for name in credentials.all_names():
        if name not in APP_NAMES:
            monkeypatch.delenv(name, raising=False)
    # A developer's .env may hold cloud names (phase 12 sets AWS_* for the live proof). The
    # app re-reads .env inside readiness, so keep that load to the app names during a test.
    from app import config

    original = config._load_env_file

    def app_names_only(path):
        before = set(os.environ)
        original(path)
        for name in credentials.all_names():
            if name not in APP_NAMES and name not in before:
                os.environ.pop(name, None)

    monkeypatch.setattr(config, "_load_env_file", app_names_only)
