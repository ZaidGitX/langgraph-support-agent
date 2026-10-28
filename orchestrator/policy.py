"""Policy for generated commands and artifacts (Sprint 3, phase 3).

Sprint 2 kept Astra away from shell by schema. From phase 5 on, Astra writes files that
contain commands, so this module checks every generated artifact before the person sees it
and every command again right before it runs. The rules are regexes and path rules, never a
model opinion. They mirror ``.claude/skills/sprint3-agentic-cicd/scripts/policy_rules.py``
(the hooks use that copy); ``tests/test_policy.py`` keeps the two identical.
"""

from __future__ import annotations

import re

from . import credentials
from .schemas import ArtifactSet, GeneratedArtifact, PolicyFinding, PolicyResult

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bya29\.[A-Za-z0-9_\-]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9\-]{10,}",
    r"hawk\.[A-Za-z0-9]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"snyk_uat\.[A-Za-z0-9._\-]+",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]

ALLOWED_PATH_PREFIXES = (
    ".github/workflows/gen-",
    "ci/",
    "deploy/",
    "docker/",
    "docs/deploy/",
    ".env.",
    "Dockerfile.generated",
    "docker-compose.generated",
)

WORKFLOW_KINDS = ("github_workflow",)
SHELL_KINDS = ("shell",)
FORBIDDEN_TRIGGERS = (
    "push", "pull_request", "pull_request_target", "schedule", "workflow_run", "release",
    "issues", "issue_comment", "create", "delete", "deployment",
)
MAX_ARTIFACT_BYTES = 32 * 1024


def _secret_name_alternation() -> str:
    names = credentials.secret_names()
    return "|".join(re.escape(name) for name in names) or "NO_SECRET_NAMES"


# (id, severity, pattern, message, kinds). kinds () means every kind. The ids and patterns
# are identical to the skill's policy_rules.py; the secret-name alternation is built from
# the same registry at import time.
RULES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("destructive.rm", "error",
     r"\brm\s+(-[A-Za-z]+\s+)*-[A-Za-z]*r[A-Za-z]*(\s+-[A-Za-z]+)*\s+(/(\s|$)|~(/?\s|/?$)|\$HOME\b|\.\.?(\s|$)|\*(\s|$)|/\*)",
     "rm -r on /, ~, $HOME, ., or * is not allowed", ()),
    ("destructive.curl_sh", "error", r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da|)sh\b",
     "piping a download into a shell is not allowed", ()),
    ("destructive.sudo", "error", r"(^|[;&|\s(`])sudo\s", "sudo is not allowed in generated artifacts or agent commands", ()),
    ("destructive.chmod777", "error", r"\bchmod\s+(-R\s+)?777\b", "chmod 777 is not allowed", ()),
    ("destructive.force_push", "error",
     r"\bgit\s+push\b[^\n]*((\s--force\b|\s-f\b|\s--force-with-lease\b)[^\n]*\b(main|master)\b|\s\+(main|master)\b)",
     "force-pushing main or master is not allowed", ()),
    ("destructive.terraform", "error", r"\bterraform\s+destroy\b", "terraform destroy is not allowed", ()),
    ("destructive.kubectl", "error", r"\bkubectl\s+delete\s+(ns|namespace|-A|--all)\b", "deleting namespaces or everything is not allowed", ()),
    ("destructive.aws", "error", r"\baws\s+[a-z0-9-]+\s+(delete-[a-z0-9-]+|rb|rm)\b", "aws delete/rb/rm commands are not allowed", ()),
    ("destructive.gcloud", "error", r"\bgcloud\s+[^\n|;&]*\bdelete\b", "gcloud delete commands are not allowed", ()),
    ("destructive.az", "error", r"(^|[;&|\s(`])az\s+[^\n|;&]*\bdelete\b", "az delete commands are not allowed", ()),
    ("destructive.docker_prune", "error", r"\bdocker\s+(system|volume|image|container|builder)\s+prune\b", "docker prune is not allowed", ()),
    ("prod.reference", "error", r"\b(production|prod)\b", "production is never a target; there is no path past UAT", ()),
    ("secret.literal", "error", "|".join(f"(?:{p})" for p in SECRET_PATTERNS), "a credential-shaped literal was found", ()),
    ("secret.assignment", "error",
     r"^[ \t]*(export[ \t]+)?[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*[ \t]*[=:][ \t]*[\"']?[^\s\"'$#{<][^\s\"']{7,}",
     "a secret-named variable is assigned a literal; use ${{ secrets.NAME }}, $NAME, or ${NAME}", ()),
    ("secret.gh_set_literal", "error", r"\bgh\s+secret\s+set\s+\S+\s+(-b|--body)\s*=?\s*[\"']?[^\s\"'$\-]",
     "gh secret set with a literal body is not allowed", ()),
    ("exfil.env_dump", "error",
     r"(^|[;&|\s(`])(printenv|env)\s*(\||>|$)|\bcat\s+[^\n|;&]*\.env\b(?!\.example)|\b(echo|printf)\b[^\n]*\$\{?(" + _secret_name_alternation() + r")\b|\baws\s+configure\s+get\b[^\n]*secret",
     "printing environment or secret values is not allowed", ()),
    ("shell.base64_exec", "error", r"\bbase64\s+(-d|--decode)\b[^\n]*\|\s*(ba|)sh\b", "decoding into a shell is not allowed", ()),
    ("toggle.in_artifact", "error", r"ORCHESTRATOR_(CLOUD_APPLY|WRITE_ARTIFACTS)\s*[=:]\s*[\"']?1", "an artifact may not switch on cloud apply or materialization", ()),
    ("pin.unpinned_action", "error", r"(?m)^\s*-?\s*uses:\s*(?!\./|docker://)[^\s@#]+\s*(#.*)?$", "every uses: must be pinned with @tag or @sha", WORKFLOW_KINDS),
    ("pin.floating_ref", "warn", r"\buses:\s*[^\s@]+@(main|master|latest)\b", "floating action refs drift; pin a tag or SHA", WORKFLOW_KINDS),
    ("shell.eval", "warn", r"(^|[;&|\s(`])eval\s", "eval makes the command opaque to the policy", ()),
    ("shell.no_verify", "warn", r"--no-verify\b", "--no-verify skips hooks", ()),
]

CODE_RULE_IDS = (
    "trigger.not_dispatch", "trigger.missing_dispatch", "perms.missing", "secret.unknown_name",
    "shell.no_strict", "size.too_large", "path.outside", "path.workflow_name",
)
CASE_SENSITIVE_RULES = {"secret.assignment", "secret.literal"}

ASK_PATTERNS: list[tuple[str, str]] = [
    ("cloud.aws_mutate", r"\baws\s+(codebuild\s+start-build|ecs\s+(update|create|run)-\S+|ecr\s+(create|put|batch-delete)\S*|cloudformation\s+(create|update|deploy)\S*|s3\s+(cp|sync|mb)\b|lambda\s+(create|update)\S*|iam\s+(create|put|attach)\S*|sts\s+assume-role|logs\s+(create|put)\S*)"),
    ("cloud.aws_login", r"\baws\s+(login|sso\s+login|configure\b(?!\s+(list|get)))"),
    ("cloud.gcloud_mutate", r"\bgcloud\s+(builds\s+submit|run\s+deploy|deploy\b|container\s+clusters\s+create|compute\s+instances\s+create|auth\s+login|auth\s+activate-service-account)"),
    ("cloud.az_mutate", r"(^|[;&|\s(`])az\s+(pipelines\s+run|containerapp\s+(create|up|update)|acr\s+build|webapp\s+(up|deploy)|deployment\s+(group|sub)\s+create|vm\s+create|login\b)"),
    ("cloud.gh_mutate", r"\bgh\s+(workflow\s+run|pr\s+merge|release\s+create|secret\s+set|auth\s+(login|refresh)|repo\s+(create|delete))"),
    ("cloud.docker_push", r"\bdocker\s+(push|login)\b"),
    ("cloud.kubectl_mutate", r"\bkubectl\s+(apply|rollout|scale|create|patch|set|replace)\b(?![^\n]*--dry-run)"),
    ("cloud.terraform_apply", r"\bterraform\s+apply\b"),
    ("cloud.helm", r"\bhelm\s+(install|upgrade|uninstall)\b"),
    ("cloud.jenkins_build", r"\b(curl|http|wget)\b[^\n]*(jenkins|/job/)[^\n]*(build|buildWithParameters)"),
    ("vcs.push", r"\bgit\s+push\b"),
]

READ_ONLY_PATTERNS: list[str] = [
    r"\baws\s+sts\s+get-caller-identity\b",
    r"\baws\s+configure\s+list\b",
    r"\baws\s+[a-z0-9-]+\s+(describe|list|get)-[a-z0-9-]+\b",
    r"\bgcloud\s+(config\s+list|[a-z-]+\s+(list|describe))\b",
    r"(^|[;&|\s(`])az\s+(account\s+show|[a-z-]+\s+(list|show))\b",
    r"\bgh\s+(run\s+(list|view)|workflow\s+(list|view)|auth\s+status|api\s+(?!-X\s*(POST|PUT|PATCH|DELETE)))",
    r"\bkubectl\s+[^\n]*--dry-run=client\b",
    r"\bdocker\s+(info|images|ps|version|compose\s+config)\b",
]


def _compile_rules():
    compiled = []
    for rule_id, severity, pattern, message, kinds in RULES:
        flags = re.MULTILINE if rule_id in CASE_SENSITIVE_RULES else (re.IGNORECASE | re.MULTILINE)
        compiled.append((rule_id, severity, re.compile(pattern, flags), message, kinds))
    return compiled


_COMPILED = _compile_rules()
_ASK = [(rule_id, re.compile(pattern, re.IGNORECASE)) for rule_id, pattern in ASK_PATTERNS]
_READ_ONLY = [re.compile(pattern, re.IGNORECASE) for pattern in READ_ONLY_PATTERNS]

# A line that refuses production is allowed to name it: comments stating the rule, case
# guards like `production|prod) ... exit 2`, and sentences such as "No path past UAT".
_PROD_EXEMPT = [
    re.compile(r"\b(no|never|not|non|without|zero|refus\w*|reject\w*|forbid\w*|is not a (choice|target|tier))\b[^\n]*\bprod(uction)?\b", re.IGNORECASE),
    re.compile(r"\bprod(uction)?\b[^\n]*\b(is not|is never|not a (choice|target|tier)|refused|rejected|forbidden|no path past uat)\b", re.IGNORECASE),
    re.compile(r"\b(production\|prod|prod\|production)\s*\)", re.IGNORECASE),
    re.compile(r"no path past uat", re.IGNORECASE),
]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _finding(rule: str, severity: str, message: str, line: int) -> PolicyFinding:
    return PolicyFinding(rule=rule, severity=severity, message=message, line=line)


def _on_block(text: str) -> str:
    """The top-level ``on:`` block of a GitHub workflow (inline or nested)."""
    lines = text.splitlines()
    for position, line in enumerate(lines):
        if re.match(r"^(\"?on\"?|true):", line):
            inline = line.split(":", 1)[1].strip()
            block = [inline] if inline else []
            for follow in lines[position + 1 :]:
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break
                block.append(follow)
            return "\n".join(block)
    return ""


def _trigger_findings(text: str) -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    block = _on_block(text)
    inline = block.splitlines()[0].strip() if block else ""
    found: set[str] = set()
    if inline and not inline.startswith("#"):
        if inline.startswith("["):
            found.update(re.findall(r"[a-z_]+", inline))
        elif re.match(r"^[a-z_]+$", inline):
            found.add(inline)
    for match in re.finditer(r"(?m)^\s+([a-z_]+)\s*:", block):
        found.add(match.group(1))
    forbidden = sorted(trigger for trigger in found if trigger in FORBIDDEN_TRIGGERS)
    if forbidden:
        findings.append(_finding("trigger.not_dispatch", "error", f"generated workflows trigger on workflow_dispatch only (found {', '.join(forbidden)})", 1))
    if "workflow_dispatch" not in found:
        findings.append(_finding("trigger.missing_dispatch", "error", "workflow has no workflow_dispatch trigger", 1))
    return findings


def check_text(text: str, *, kind: str = "shell", path: str = "") -> PolicyResult:
    """Apply every rule to one artifact's text (or one command line)."""
    findings: list[PolicyFinding] = []
    seen: set[tuple[str, int]] = set()
    lines = text.splitlines()
    for rule_id, severity, regex, message, kinds in _COMPILED:
        if kinds and kind not in kinds:
            continue
        for match in regex.finditer(text):
            line_no = _line_of(text, match.start())
            line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            if rule_id == "prod.reference" and any(rule.search(line_text) for rule in _PROD_EXEMPT):
                continue
            if rule_id == "secret.assignment" and "${{" in line_text:
                continue
            key = (rule_id, line_no)
            if key in seen:
                continue
            seen.add(key)
            findings.append(_finding(rule_id, severity, message, line_no))
    if kind in WORKFLOW_KINDS:
        findings.extend(_trigger_findings(text))
        if not re.search(r"(?m)^permissions:", text):
            findings.append(_finding("perms.missing", "warn", "workflow has no top-level permissions block", 1))
        known = set(credentials.all_names())
        for match in re.finditer(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", text):
            name = match.group(1)
            if name not in known and name != "GITHUB_TOKEN":
                findings.append(_finding("secret.unknown_name", "warn", f"secrets.{name} is not in the credential registry", _line_of(text, match.start())))
    if kind in SHELL_KINDS:
        head = "\n".join(lines[:5])
        if "set -euo pipefail" not in head and "set -eu" not in head:
            findings.append(_finding("shell.no_strict", "warn", "script does not start with set -euo pipefail", 1))
    if len(text.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        findings.append(_finding("size.too_large", "error", "artifact is over 32 KB", 1))
    return PolicyResult(ok=not any(item.severity == "error" for item in findings), findings=findings)


def check_path(path: str) -> PolicyResult:
    clean = (path or "").replace("\\", "/")
    findings: list[PolicyFinding] = []
    if not clean or clean.startswith("/") or ".." in clean.split("/"):
        findings.append(_finding("path.outside", "error", "artifact path must be relative and inside the checkout", 0))
        return PolicyResult(ok=False, findings=findings)
    if not any(clean.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        findings.append(_finding("path.outside", "error", f"artifact path must start with one of {ALLOWED_PATH_PREFIXES}", 0))
    if clean.startswith(".github/workflows/") and not re.fullmatch(r"\.github/workflows/gen-[a-z0-9_]+\.yml", clean):
        findings.append(_finding("path.workflow_name", "error", "generated workflows are named .github/workflows/gen-<stage>.yml", 0))
    return PolicyResult(ok=not findings, findings=findings)


def kind_for_path(path: str) -> str:
    clean = (path or "").replace("\\", "/")
    name = clean.rsplit("/", 1)[-1].removesuffix(".tpl")
    if (clean.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))) or name.startswith("github-workflow"):
        return "github_workflow"
    if name == "Jenkinsfile" or name.endswith(".Jenkinsfile") or name.startswith("Jenkinsfile"):
        return "jenkinsfile"
    if name.startswith("buildspec"):
        return "buildspec"
    if name.startswith("cloudbuild"):
        return "cloudbuild"
    if name.startswith("azure-pipelines"):
        return "azure_pipeline"
    if name.startswith("Dockerfile"):
        return "dockerfile"
    if name.startswith("docker-compose"):
        return "compose"
    if name.startswith((".env", "env.")) or name.endswith(".env") or ".env." in name:
        return "env_example"
    if name.endswith(".sh"):
        return "shell"
    if name.startswith("kubernetes") or name.endswith((".yml", ".yaml")):
        return "kubernetes_manifest"
    if name.endswith(".md"):
        return "doc"
    return "script"


def _merge(*results: PolicyResult) -> PolicyResult:
    findings = [item for result in results for item in result.findings]
    return PolicyResult(ok=not any(item.severity == "error" for item in findings), findings=findings)


def check_artifact(artifact: GeneratedArtifact) -> PolicyResult:
    """Text rules for the artifact's kind plus the path rules."""
    return _merge(check_text(artifact.content, kind=artifact.kind, path=artifact.path), check_path(artifact.path))


def check_artifact_set(artifact_set: ArtifactSet, stage: dict | None = None) -> PolicyResult:
    results = [check_artifact(artifact) for artifact in artifact_set.artifacts]
    merged = _merge(*results) if results else PolicyResult(ok=True, findings=[])
    if stage and len(artifact_set.artifacts) > 6:
        merged.findings.append(_finding("size.too_large", "error", "a stage may carry at most six artifacts", 0))
        merged.ok = False
    return merged


def check_commands(commands: list[dict]) -> PolicyResult:
    """The last check before execution: every argv list joined into one shell-shaped line."""
    text = "\n".join(" ".join(str(part) for part in (command.get("argv") or [])) for command in commands or [])
    result = check_text(text, kind="command")
    result.findings = [item for item in result.findings if item.rule not in {"shell.no_strict"}]
    result.ok = not any(item.severity == "error" for item in result.findings)
    return result


def summary(result: PolicyResult) -> dict:
    return {
        "ok": result.ok,
        "errors": sum(1 for item in result.findings if item.severity == "error"),
        "warns": sum(1 for item in result.findings if item.severity == "warn"),
    }


def records(result: PolicyResult, *, stage: str, path: str) -> list[dict]:
    """Findings as the flat rows kept in ``PipelineState.policy``."""
    return [
        {"stage": stage, "path": path, "rule": item.rule, "severity": item.severity, "message": item.message, "line": item.line}
        for item in result.findings
    ]


def rule_ids() -> list[str]:
    return sorted({rule[0] for rule in RULES} | set(CODE_RULE_IDS))


def rules_summary() -> list[str]:
    """One line per rule, for Astra's generation prompt."""
    lines = [f"{rule_id} ({severity}): {message}" for rule_id, severity, _pattern, message, _kinds in RULES]
    lines.extend(
        [
            "trigger.not_dispatch (error): a generated GitHub workflow triggers only on workflow_dispatch",
            "trigger.missing_dispatch (error): a generated GitHub workflow must declare workflow_dispatch",
            "perms.missing (warn): a workflow should declare a top-level permissions block",
            "secret.unknown_name (warn): secrets.NAME must be a name from the credential registry",
            "shell.no_strict (warn): a shell script starts with set -euo pipefail",
            "size.too_large (error): an artifact is at most 32 KB",
            f"path.outside (error): artifact paths are relative and start with one of {ALLOWED_PATH_PREFIXES}",
            "path.workflow_name (error): generated workflows are named .github/workflows/gen-<stage>.yml",
        ]
    )
    return lines


def classify_command(command: str) -> tuple[str, str, str]:
    """(decision, rule_id, message) for a shell command: deny | ask | allow."""
    text = command or ""
    for rule_id, severity, regex, message, kinds in _COMPILED:
        if kinds or severity != "error":
            continue
        if rule_id in {"toggle.in_artifact", "prod.reference"}:
            continue
        if regex.search(text):
            return "deny", rule_id, message
    if re.search(r"(--target|--environment|--env|environment=|ENV=|TARGET=|STAGE=|--namespace|-n)\s*=?\s*[\"']?(prod|production)\b", text, re.IGNORECASE):
        return "deny", "prod.reference", "production is never a target; there is no path past UAT"
    for regex in _READ_ONLY:
        if regex.search(text):
            return "allow", "read_only", "read-only command"
    for rule_id, regex in _ASK:
        if regex.search(text):
            return "ask", rule_id, "this command changes cloud, registry, or remote state; confirm before running it"
    return "allow", "", ""
