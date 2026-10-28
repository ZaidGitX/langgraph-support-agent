# Artifact skeleton templates

Rendered by `orchestrator/generator.render_template(kind, stage, environment, runner=...)`
with Python's `string.Template.safe_substitute`. Placeholders are `$NAME`; a literal `$`
is written `$$` (so `$${{ secrets.X }}` renders to `${{ secrets.X }}` and `$$RUN_ID`
renders to `$RUN_ID` for bash). Every template keeps the trigger, permissions, checkout,
and env placeholder block fixed and leaves a `# --- ASTRA: steps ---` marker where Astra
writes the stage's steps. Astra returns the whole file; the policy checks the whole file.

| Template | Artifact kind | Used by runner |
| --- | --- | --- |
| `github-workflow.generated.yml.tpl` | `github_workflow` | github_actions |
| `Jenkinsfile.tpl` | `jenkinsfile` | jenkins |
| `buildspec.yml.tpl` | `buildspec` | aws_codebuild |
| `cloudbuild.yaml.tpl` | `cloudbuild` | gcp_cloudbuild |
| `azure-pipelines.yml.tpl` | `azure_pipeline` | azure_pipelines |
| `stage.sh.tpl` | `shell` | local, docker, ssh_remote |
| `Dockerfile.generated.tpl` | `dockerfile` | docker, build_image stages |
| `docker-compose.generated.yml.tpl` | `compose` | ssh_compose deploys, integration tests |
| `kubernetes-deployment.yaml.tpl` | `kubernetes_manifest` | kubernetes |
| `env.target.example.tpl` | `env_example` | every runner (written by the generator, not Astra) |
| `deploy-doc.md.tpl` | `doc` | report / `write_docs` |

Variables available to every template: `STAGE`, `FAMILY`, `PURPOSE`, `RUN_ID`, `TIER`,
`PLATFORM`, `REGION`, `RUNNER`, `PROVIDER`, `APP_NAME` (`langgraph-support-app`), `PORT`
(`8000`), `PYTHON_VERSION` (`3.12`), `NODE_VERSION` (`22`), `IMAGE` (`$APP_NAME:$RUN_ID`),
`GENERATED_AT`, `ENV_LINES` (pre-rendered placeholder lines for the format),
`SECRETS_LINES` (GitHub `NAME: ${{ secrets.NAME }}` lines), `PLACEHOLDER_NAMES` (comma list).
