"""Runner -> provider lookup and host-wide detection."""

from __future__ import annotations

from collections.abc import Mapping

from .. import registry as stage_registry
from .aws import AwsProvider
from .azure import AzureProvider
from .base import LOCAL_RUNNERS, Provider, ProviderStatus
from .docker import DockerProvider
from .gcp import GcpProvider
from .github_actions import GithubActionsProvider
from .jenkins import JenkinsProvider
from .kubernetes import KubernetesProvider
from .local import LocalProvider
from .ssh import SshProvider

PROVIDERS: dict[str, Provider] = {
    provider.runner: provider
    for provider in (
        LocalProvider(),
        DockerProvider(),
        GithubActionsProvider(),
        JenkinsProvider(),
        AwsProvider(),
        GcpProvider(),
        AzureProvider(),
        SshProvider(),
        KubernetesProvider(),
    )
}

assert set(PROVIDERS) == set(stage_registry.RUNNERS), "every runner in the stage registry needs a provider"


def for_runner(runner: str) -> Provider:
    try:
        return PROVIDERS[str(runner or "").strip().lower()]
    except KeyError:
        raise KeyError(f"no provider for runner {runner!r}; expected one of {sorted(PROVIDERS)}") from None


def detect_all() -> dict[str, dict]:
    """Every runner's status as plain dicts (what ``credentials.readiness`` accepts)."""
    return {runner: provider.detect().as_dict() for runner, provider in PROVIDERS.items()}


def mode_for(runner: str, status: ProviderStatus | Mapping) -> str:
    if isinstance(status, ProviderStatus):
        return status.mode
    return str(status.get("mode") or "plan")


def status_from_readiness(readiness: Mapping | None, runner: str) -> ProviderStatus | None:
    """Rebuild a status from a stored readiness report, so a stage does not re-detect."""
    entry = ((readiness or {}).get("runners") or {}).get(runner)
    if not isinstance(entry, Mapping):
        return None
    group = ((readiness or {}).get("providers") or {}).get(entry.get("group") or "", {})
    return ProviderStatus(
        runner=runner,
        cli=bool(entry.get("cli")),
        cli_path=str(group.get("cli_path") or ""),
        present=list(group.get("present") or []),
        missing=list(entry.get("missing") or []),
        mode=str(entry.get("mode") or "plan"),
    )


__all__ = ["LOCAL_RUNNERS", "PROVIDERS", "detect_all", "for_runner", "mode_for", "status_from_readiness"]
