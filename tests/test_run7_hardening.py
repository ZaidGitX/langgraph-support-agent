"""Live run 7 (2026-11-05): an empty registry variable from a Compose placeholder is unset."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app.main import app
from orchestrator import credentials


def test_drop_empty_env_removes_only_empty_registry_names(monkeypatch):
    env = {"AWS_PROFILE": "", "AWS_SESSION_TOKEN": "   ", "AWS_REGION": "us-east-1", "UNRELATED": "", "OPENAI_API_KEY": "x"}
    assert credentials.drop_empty_env(env) == ["AWS_PROFILE", "AWS_SESSION_TOKEN"]
    assert env == {"AWS_REGION": "us-east-1", "UNRELATED": "", "OPENAI_API_KEY": "x"}
    assert credentials.drop_empty_env(env) == []


def test_the_app_drops_empty_placeholders_at_startup(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "")
    monkeypatch.setenv("KUBECONFIG", "")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    with TestClient(app):
        assert "AWS_PROFILE" not in os.environ and "KUBECONFIG" not in os.environ
        assert os.environ["AWS_REGION"] == "eu-west-1"
