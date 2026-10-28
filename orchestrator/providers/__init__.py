"""Providers: where a stage runs and how (Sprint 3, phase 4).

A runner names where a stage executes; a provider is the adapter for that runner. Every
provider detects whether it can run now, renders the artifact skeletons its runner needs,
and executes in ``plan`` mode (records the exact command it would run and the names it is
missing) or ``apply`` mode (runs it). See ``base.py`` for the protocol and ``registry.py``
for the lookup.
"""

from .base import (
    LOCAL_RUNNERS,
    BaseProvider,
    Provider,
    ProviderStatus,
    StageRun,
    expand_env,
    plan_result,
)
from .registry import PROVIDERS, detect_all, for_runner, mode_for, status_from_readiness

__all__ = [
    "LOCAL_RUNNERS",
    "PROVIDERS",
    "BaseProvider",
    "Provider",
    "ProviderStatus",
    "StageRun",
    "detect_all",
    "expand_env",
    "for_runner",
    "mode_for",
    "plan_result",
    "status_from_readiness",
]
