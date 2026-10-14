from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _orchestrator_stays_manual(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_AUTOSTART", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_DISPATCH", raising=False)
