# 13 — Vault and connections (Sprint 4, Phase 8)

Sprint 3 kept credentials names-only and read values from the process environment. Sprint 4
lets a person connect a platform from the dashboard without weakening that: values enter
through two narrow doors, an encrypted vault and the vendor CLI's own device-code login, and
still never reach state, a prompt, a report, a log, or an HTTP response.

## Registry additions (`orchestrator/credentials.json` v2, mirrored in `assets/`)

Per group, two optional blocks. Names only; `$NAME` is expanded at run time.

```json
"login":  {"argv": ["az", "login", "--use-device-code", "--output", "none"],
           "url_pattern": "https://microsoft\\.com/devicelogin", "code_pattern": "\\b[A-Z0-9]{8,10}\\b",
           "input": "", "requires": [], "note": "…"},
"verify": {"argv": ["az", "account", "show", "--output", "json"]}
"verify": {"http": "$JENKINS_URL/api/json", "auth": ["JENKINS_USER", "JENKINS_API_TOKEN"]}
```

| Group | Login | Verify |
| --- | --- | --- |
| github | `gh auth login --hostname github.com --git-protocol https --web` | `gh auth status` |
| aws | `aws login --remote --profile $AWS_PROFILE` (requires `AWS_PROFILE`; `input`: the authorization code from the browser pasted back; the console flow for an IAM user or root, Identity Center users run `aws sso login` in a terminal) | `aws sts get-caller-identity` |
| azure | `az login --use-device-code` | `az account show` |
| gcp | `gcloud auth login --no-launch-browser` (`input`: verification code pasted back) | `gcloud auth list --filter=status:ACTIVE` |
| jenkins | none (vault form) | HTTP `GET $JENKINS_URL/api/json` with basic auth |
| docker / kubernetes / ssh / git | none | `docker info`, `kubectl cluster-info`, `ssh … true`, `git ls-remote` |

New names: `ORCHESTRATOR_VAULT_KEY`, `ORCHESTRATOR_UI_TOKEN`, `ORCHESTRATOR_WEBHOOK_SECRET`,
`ORCHESTRATOR_WATCH_REMOTE`, `ORCHESTRATOR_WATCH_INTERVAL`, `AWS_CODEBUILD_ROLE_ARN`, and the
`git` group (`GIT_REMOTE_URL`, `GIT_REMOTE_USER`, `GIT_REMOTE_TOKEN`). `.env.example`,
`docker-compose.yml`, and the readiness panel derive from the registry as before.

## `orchestrator/vault.py`

- File `var/vault/secrets.enc` (`ORCHESTRATOR_VAULT_PATH` overrides the folder), Fernet
  (`cryptography`), written atomically with mode 0600. Key: `ORCHESTRATOR_VAULT_KEY`, else
  generated once into `var/vault/.key` (0600).
- `set_many({NAME: value})` accepts registry names only, non-empty values ≤ 8192 chars;
  stores, then `export_env` copies them into `os.environ` (a vault value overrides an older
  variable: the newest instruction wins) and `redaction.register_values` makes every value,
  whatever its kind, scrubbed verbatim from any output.
- `delete(name)`, `names()`, `clear()`, `generate_key()`, `token()`. `load()` is internal.
- `app/main.py` calls `vault.export_env()` at startup, so a restart keeps the connections.

## `orchestrator/connect.py`

- `start_login(group)` spawns the login argv without a TTY (`NO_COLOR`, `GH_PROMPT_DISABLED`,
  `AWS_PAGER=""`), reads stdout line by line (redacted, last 200 lines kept, 12 returned),
  finds the URL and one-time code with the registry patterns, and marks the flow `waiting`.
  Exit 0 → `done` (then `verify` runs automatically); otherwise `failed`; 15 minutes →
  `expired`. `provide_input(flow_id, text)` writes a pasted code to stdin. Flows are kept in
  memory (last 20). Never a value in `as_dict()`.
- `verify(group)` runs the verify argv (20 s) or the HTTP check and caches
  `{ok, checked_at, detail}` (detail ≤ 300 chars, redacted). It refuses to run when a `$NAME`
  in the argv is unset and says which. `credentials.group_status` reads the cache only:
  `authenticated`, `checked_at`, and `mode: apply` when the CLI is present and verified even
  if the static key names are missing. Nothing in readiness or the checkout node calls the
  network.
- `sync_secrets("github", groups=, names=, env=, runner=)` (phase 12, live run 4): copies the
  values present on the host (vault, `.env`, environment) whose names are `github_secret` in
  the registry into the repository's Actions secrets, one `gh secret set NAME --body-file -
  --repo owner/name` per name with the value on stdin (never on an argv). The repository comes
  from `GITHUB_REPOSITORY` or, for the live environment, the origin remote. The result names
  names only: `synced`, `skipped` (no value here), `failed` (name plus a redacted one-line
  reason). `python -m orchestrator.connect sync-github [--group aws]` is the shell form.
  `ORCHESTRATOR_UI_TOKEN` is `github_secret: true` so a vault token reaches the push workflow.

## API (`app/orchestrator_api.py`)

| Route | Returns |
| --- | --- |
| `GET /connections` | `{generated_at, token_required, groups[]}`; each group: key, label, cli, mode, runners, present, missing, `authenticated`, `checked_at`, `env[]` (`name, kind, required, purpose, set, source: vault/env/unset`), `vault[]` (names), `login{available, reason, note, input}`, `verify`, docs |
| `POST /connections/{group}/secrets` `{values}` | write-only; 400 for a name outside the group or an invalid value |
| `DELETE /connections/{group}/secrets/{name}` | forgets the vault copy |
| `POST /connections/{group}/login` → `GET …/login/{flow_id}` → `POST …/login/{flow_id}/input` / `…/cancel` | the flow as `as_dict()` |
| `POST /connections/{group}/verify` | `{verified, connection}` |
| `POST /connections/github/sync` `{groups?, names?}` | `{sync: {repository, synced, skipped, failed, at}, connection}`; 409 for another group. The Connections card shows "Sync secrets to GitHub". |

`require_token`: when `ORCHESTRATOR_UI_TOKEN` is set, every route on the `mutating` router
(session start, answer, viewed, public-url, all connection writes) needs
`X-Orchestrator-Token` (constant-time compare). The push workflow passes the
`ORCHESTRATOR_UI_TOKEN` repository secret through to Compose and says in its log whether one
is set; it never prints the value.

## UI

`frontend/src/api.js` (`apiFetch`, token in `sessionStorage`, 401 → `orchestrator:token-required`
event), `components/TokenPrompt.jsx`, `components/Connections.jsx` (cards, Connect: device-code
flow polled every 1.5 s with URL + code + pasted-input form, or the vault form with password
inputs for `kind: secret`; Verify; forget). The idle screen shows Connections and Episodes; a
header button opens Connections in any other phase.

## Image

`Dockerfile` installs `aws`, `kubectl`, `az`, and `gcloud` behind `ARG WITH_CLOUD_CLIS=1`, so
the service, not the person's laptop, carries the platform tools.

## Tests

`tests/test_vault.py` (round trip, names only, 0600, key handling, env export and override,
redaction of a config value, readiness from a verified login, login flow with
`tests/fakes/fake_login.py`, pasted input, failure, `can_login` reasons, verify refusing unset
names) and `tests/test_connections_api.py` (names-only listing, write-only store, wrong
group, token 401/200, login flow over HTTP, verify). `conftest.py` points the vault at a temp
folder and clears cached checks for every test.

## Invariant wording

"Secrets by reference only" now reads: values live only in the encrypted vault file and in the
service process environment, entered through the Connections panel or the environment; still
never in state, reports, prompts, artifacts, logs, or any HTTP response.
