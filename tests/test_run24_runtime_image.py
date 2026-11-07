"""Live run 24 (2026-11-06): every CodeBuild stage ran its contract from the skeleton and five passed
at the first attempt; the image scan then failed the deployed image on a HIGH finding published that
day inside the GitHub CLI binary (golang.org/x/net in gh), a tool the support app never runs. The
deployed image (WITH_CLOUD_CLIS=0) carries no CI tooling: gh moves into the orchestrator-only block,
and the base image's Debian packages are brought to the current security fixes at build time."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _run_blocks(text: str) -> list[str]:
    """Each RUN instruction's text (continuation lines joined)."""
    blocks, current = [], None
    for line in text.splitlines():
        if line.startswith("RUN "):
            current = line
        elif current is not None and line.startswith((" ", "\t", "&&", "|")):
            current += "\n" + line
        else:
            if current is not None:
                blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def test_the_github_cli_is_installed_only_with_the_cloud_clis():
    text = _dockerfile()
    blocks = _run_blocks(text)
    with_gh = [block for block in blocks if re.search(r"apt-get install[^\n]*\bgh\b", block)]
    assert len(with_gh) == 1, "gh is installed in exactly one RUN instruction"
    assert 'if [ "$WITH_CLOUD_CLIS" = "1" ]' in with_gh[0]  # the orchestrator image only
    assert "cli.github.com" in with_gh[0] and "githubcli-archive-keyring" in with_gh[0]
    base = next(block for block in blocks if "ca-certificates curl" in block)
    assert "gh" not in base.replace("gnupg", "") and "gnupg" not in base and "cli.github.com" not in base
    assert "apt-get upgrade -y --no-install-recommends" in base  # Debian fixes current at build time
    assert text.index(base) < text.index(with_gh[0])


def test_the_deployed_image_still_drops_pip_and_runs_as_appuser():
    text = _dockerfile()
    assert 'if [ "$WITH_CLOUD_CLIS" = "0" ]; then pip uninstall -y pip' in text
    assert "\nUSER appuser\n" in text and 'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]' in text
    assert "COPY --from=frontend /src/dist ./frontend/dist" in text
