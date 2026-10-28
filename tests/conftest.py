from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _orchestrator_stays_manual(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_AUTOSTART", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_DISPATCH", raising=False)
    # Sprint 3: episodes and the retrieval index stay inside each test's tmp root, offline.
    monkeypatch.delenv("ORCHESTRATOR_DB", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_EMBEDDINGS", raising=False)
