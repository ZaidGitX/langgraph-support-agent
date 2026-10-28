"""Sprint 3 phase 1: stage registry, StageSpec v2, new state fields, open stage names."""

from __future__ import annotations

import json
import operator
import sys
import typing

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from orchestrator import registry
from orchestrator.catalog import commands_for
from orchestrator.demo import drive
from orchestrator.graph import (
    TOOL_BINDINGS,
    _deps_met,
    build_graph,
    normalize_stages,
    ready_stage_names,
)
from orchestrator.schemas import (
    ArtifactKind,
    ArtifactSet,
    CandidatePipeline,
    ClarifyDecision,
    Diagnosis,
    EnvironmentSpec,
    FollowUpSet,
    GateDecision,
    GeneratedArtifact,
    PlanProposal,
    Platform,
    PolicyResult,
    QuestionSet,
    RankDecision,
    Runner,
    RunnerDecision,
    StageBrief,
    StageFamily,
    StageSpec,
    Tier,
)
from orchestrator.state import (
    FORBIDDEN_TARGETS,
    MAX_ARTIFACT_REVISIONS,
    MAX_CLARIFY_ROUNDS,
    SATISFIED_STATUSES,
    STAGE_NAMES,
    STATUSES,
    PipelineState,
    initial_state,
    merge_dict,
    pending_stages,
    planned_stages,
    platform_of,
    runner_default_for,
)

TEMPLATE_NAMES = {
    "build", "lint", "compile", "frontend_build", "build_image", "dependency_install",
    "test", "unit_test", "integration_test", "e2e_test", "contract_test", "perf_test", "smoke_test",
    "scanners", "dast", "sast", "sca", "secrets_scan", "container_scan", "iac_scan", "license_scan",
    "sbom", "dast_scan", "dockerfile_lint", "workflow_lint",
    "package", "package_artifact", "publish_artifact",
    "container", "push_image",
    "release", "release_notes", "tag_release", "sign_artifact",
    "deploy", "infra_plan", "infra_apply", "deploy_dev", "deploy_staging", "deploy_uat",
    "migrate_db", "rollback_plan",
    "post_deploy_smoke", "health_check", "observability_check",
    "uat",
}  # fmt: skip


def _stage(name: str, deps: list[str] | None = None, **extra) -> StageSpec:
    return StageSpec(name=name, depends_on=deps or [], purpose=f"{name} purpose", **extra)


# ---------------------------------------------------------------- registry data


def test_fixed_stages_are_the_nine_sprint2_names():
    assert tuple(registry.FIXED_STAGES) == STAGE_NAMES
    assert set(registry.FIXED_STAGES) <= set(registry.STAGE_TEMPLATES)
    flagged = {name for name in registry.STAGE_TEMPLATES if registry.is_fixed(name)}
    assert flagged == set(STAGE_NAMES)
    assert registry.is_fixed("my_custom_check") is False
    assert registry.is_fixed("Build ") is True


def test_every_named_template_is_registered_and_well_formed():
    assert TEMPLATE_NAMES <= set(registry.STAGE_TEMPLATES)
    assert len(registry.STAGE_TEMPLATES) - len(registry.FIXED_STAGES) >= 30
    families = {item.family for item in registry.STAGE_TEMPLATES.values()}
    assert families == set(registry.STAGE_FAMILIES) - {"custom"}
    for name, item in registry.STAGE_TEMPLATES.items():
        assert item.name == name
        assert 0 < len(item.summary) < 100, name
        assert item.default_runners, name
        assert set(item.default_runners) <= set(registry.RUNNERS), name
        assert set(item.produces) <= set(registry.ARTIFACT_KINDS), name
        assert registry.CUSTOM_NAME_RE.match(name), name
        assert not set(name.split("_")) & set(registry.FORBIDDEN_NAME_TOKENS), name
    assert registry.STAGE_TEMPLATES["uat"].default_runners == ("local",)
    assert "local" in registry.STAGE_TEMPLATES["deploy"].default_runners


def test_no_template_or_default_creates_a_production_target():
    assert set(registry.FORBIDDEN_NAME_TOKENS) == set(FORBIDDEN_TARGETS)
    assert typing.get_args(Tier) == ("local", "dev", "staging", "uat")
    for schema in (StageSpec, PlanProposal, EnvironmentSpec):
        blob = json.dumps(schema.model_json_schema())
        assert '"production"' not in blob
        assert '"prod"' not in blob


def test_validate_stage_name_accepts_registry_names_and_custom_slugs():
    assert registry.validate_stage_name("Container_Scan ") == "container_scan"
    assert registry.validate_stage_name("build") == "build"
    assert registry.validate_stage_name(" License_Audit") == "license_audit"
    assert registry.validate_stage_name("product_catalog_check") == "product_catalog_check"
    assert registry.validate_stage_name("abc") == "abc"


@pytest.mark.parametrize(
    "bad",
    [
        "production", "prod", "Prod", "promote", "main", "a", "ab", "deploy-prod", "deploy_prod",
        "release_to_production", "prod_deploy", "1stage", "has space", "", "x" * 42, None, 42,
    ],
)  # fmt: skip
def test_validate_stage_name_refuses_everything_else(bad):
    with pytest.raises(ValueError, match="not allowed"):
        registry.validate_stage_name(bad)


def test_template_family_and_menu_helpers():
    assert registry.template_for("sast").family == "scan"
    assert registry.template_for("not_a_template") is None
    assert registry.family_of("deploy_dev") == "deploy"
    assert registry.family_of("license_audit") == "custom"
    menu = registry.stage_menu()
    assert len(menu) == len(registry.STAGE_TEMPLATES)
    assert set(menu[0]) == {"name", "family", "summary", "default_runners", "providers"}
    json.dumps(menu)


def test_platforms_map_to_runners():
    assert registry.runners_for("aws") == ["aws_codebuild"]
    assert registry.runners_for("local")[0] == "local"
    assert registry.runners_for("nowhere") == []
    assert set(registry.PLATFORM_RUNNERS) == set(registry.PLATFORMS)
    for platform in registry.PLATFORMS:
        assert set(registry.runners_for(platform)) <= set(registry.RUNNERS)


def test_schema_literals_match_the_registry():
    assert typing.get_args(Runner) == registry.RUNNERS
    assert typing.get_args(Platform) == registry.PLATFORMS
    assert typing.get_args(StageFamily) == registry.STAGE_FAMILIES
    assert typing.get_args(ArtifactKind) == registry.ARTIFACT_KINDS
    assert STATUSES == registry.STATUSES
    assert "planned" in STATUSES


# ---------------------------------------------------------------- StageSpec v2


def test_sprint2_stage_spec_call_still_works():
    spec = StageSpec(name="build", depends_on=[], purpose="compile")
    assert spec.kind == "fixed"
    assert spec.family == "build"
    assert spec.runner == "local"
    assert spec.environment == EnvironmentSpec()
    assert spec.artifacts == []
    assert spec.stochastic is False


def test_template_and_custom_names_become_generated_stages():
    scan = StageSpec(name="container_scan", purpose="x")
    assert (scan.kind, scan.family) == ("generated", "scan")
    custom = StageSpec(name=" License_Audit ", depends_on=["Build "], purpose="x", family="deploy", kind="fixed")
    assert custom.name == "license_audit"
    assert custom.depends_on == ["build"]
    assert (custom.kind, custom.family) == ("generated", "custom")
    conflicting = StageSpec(name="build", purpose="x", kind="generated", family="deploy")
    assert (conflicting.kind, conflicting.family) == ("fixed", "build")


def test_uat_is_fixed_and_local():
    spec = StageSpec(name="uat", runner="docker", purpose="x")
    assert spec.runner == "local"
    assert spec.kind == "fixed"


def test_stage_spec_carries_runner_environment_and_artifact_intent():
    spec = StageSpec(
        name="deploy_dev",
        runner="aws_codebuild",
        provider="aws_ecs",
        purpose="ship to dev",
        environment={"tier": "dev", "platform": "aws", "region": "us-east-1"},
        artifacts=[{"kind": "buildspec", "path": "ci/buildspec-dev.yml", "purpose": "build and deploy"}],
        stochastic=True,
    )
    assert spec.environment.platform == "aws"
    assert spec.artifacts[0].kind == "buildspec"
    assert spec.model_dump()["environment"]["region"] == "us-east-1"


@pytest.mark.parametrize("name", ["production", "deploy-prod", "deploy_prod", "x"])
def test_stage_spec_refuses_bad_names(name):
    with pytest.raises(ValidationError):
        StageSpec(name=name, purpose="x")


def test_environment_and_runner_have_no_production_or_unknown_values():
    with pytest.raises(ValidationError):
        EnvironmentSpec(tier="production")
    with pytest.raises(ValidationError):
        EnvironmentSpec(platform="mainframe")
    with pytest.raises(ValidationError):
        StageSpec(name="build", purpose="x", runner="mainframe")
    with pytest.raises(ValidationError):
        StageSpec(name="build", purpose="x", environment={"tier": "prod"})


def test_stage_name_is_open_in_the_json_schema():
    name = StageSpec.model_json_schema()["properties"]["name"]
    assert name["type"] == "string"
    assert "enum" not in name


# ---------------------------------------------------------------- CandidatePipeline / PlanProposal


def _candidate(stages: list[StageSpec], ident: str = "c1", **extra) -> CandidatePipeline:
    return CandidatePipeline(id=ident, summary="s", stages=stages, **extra)


def test_candidate_accepts_a_well_formed_uat():
    candidate = _candidate([_stage("build"), _stage("test", ["build"]), _stage("uat", ["test"])])
    assert candidate.mode == "deterministic"
    assert candidate.environment == EnvironmentSpec()


def test_candidate_uat_must_be_last_single_and_depend_on_an_earlier_stage():
    with pytest.raises(ValidationError, match="last"):
        _candidate([_stage("uat", ["build"]), _stage("build")])
    with pytest.raises(ValidationError, match="only one"):
        _candidate([_stage("build"), _stage("uat", ["build"]), _stage("uat", ["build"])])
    with pytest.raises(ValidationError, match="depend on at least one"):
        _candidate([_stage("build"), _stage("uat")])
    with pytest.raises(ValidationError, match="depend on at least one"):
        _candidate([_stage("build"), _stage("uat", ["ghost_stage"])])
    with pytest.raises(ValidationError, match="depend on at least one"):
        _candidate([_stage("uat")])
    with pytest.raises(ValidationError, match="nothing runs after"):
        _candidate([_stage("build"), _stage("lint", ["uat"]), _stage("uat", ["build"])])


def test_candidate_without_uat_is_accepted_but_can_be_required():
    stages = [_stage("build"), _stage("test", ["build"])]
    assert _candidate(stages).stages[-1].name == "test"
    payload = _candidate(stages).model_dump()
    with pytest.raises(ValidationError, match="must end with the uat stage"):
        CandidatePipeline.model_validate(payload, context={"require_uat": True})
    with pytest.raises(ValidationError, match="must end with the uat stage"):
        PlanProposal.model_validate({"candidates": [payload, {**payload, "id": "c2"}]}, context={"require_uat": True})


def _plan(*candidates: CandidatePipeline) -> PlanProposal:
    return PlanProposal(candidates=list(candidates))


def test_plan_proposal_bounds_ids_and_modes():
    one = _candidate([_stage("build")], "a")
    two = _candidate([_stage("build")], "b")
    stochastic = _candidate([_stage("build"), _stage("sast", ["build"], stochastic=True)], "s", mode="stochastic")
    with pytest.raises(ValidationError):
        _plan(one)
    with pytest.raises(ValidationError):
        PlanProposal(candidates=[_candidate([_stage("build")], str(n)) for n in range(5)])
    assert len(PlanProposal(candidates=[_candidate([_stage("build")], str(n)) for n in range(4)]).candidates) == 4
    with pytest.raises(ValidationError, match="unique"):
        _plan(one, _candidate([_stage("build")], "a"))
    with pytest.raises(ValidationError, match="deterministic"):
        _plan(stochastic, _candidate([_stage("build")], "t", mode="stochastic"))
    assert _plan(one, two).candidates[0].id == "a"  # no stochastic candidate: accepted, logged later
    assert _plan(one, stochastic).candidates[1].mode == "stochastic"


def test_stochastic_candidate_round_trips_generated_stages_with_runner_and_environment():
    generated = _stage(
        "container_scan",
        ["build_image"],
        runner="docker",
        provider="trivy",
        environment={"tier": "dev", "platform": "docker"},
        artifacts=[{"kind": "shell", "path": "ci/scan.sh", "purpose": "scan the image"}],
        stochastic=True,
    )
    proposal = _plan(
        _candidate([_stage("build"), _stage("uat", ["build"])], "fixed"),
        _candidate(
            [_stage("build_image"), generated, _stage("uat", ["container_scan"])],
            "gen",
            mode="stochastic",
            rationale="adds an image scan",
            environment={"tier": "staging", "platform": "aws", "region": "us-east-1"},
        ),
    )
    again = PlanProposal.model_validate(proposal.model_dump())
    stage = again.candidates[1].stages[1]
    assert (stage.kind, stage.family, stage.runner, stage.provider) == ("generated", "scan", "docker", "trivy")
    assert stage.environment.platform == "docker"
    assert again.candidates[1].environment.tier == "staging"


def test_other_v2_models_have_the_documented_bounds():
    artifact = GeneratedArtifact(kind="shell", path="ci/run.sh", purpose="p", content="echo ok")
    assert artifact.secrets_referenced == []
    assert set(artifact.model_dump()) >= {"kind", "path", "purpose", "content"}
    with pytest.raises(ValidationError):
        ArtifactSet(stage="lint", artifacts=[])
    with pytest.raises(ValidationError):
        ArtifactSet(stage="lint", artifacts=[artifact] * 7)
    assert ArtifactSet(stage="lint", artifacts=[artifact] * 6).notes == ""
    with pytest.raises(ValidationError):
        FollowUpSet(questions=[{"id": str(n), "prompt": "p", "reason": "r"} for n in range(4)])
    with pytest.raises(ValidationError):
        RunnerDecision(runners={"lint": "mainframe"}, rationale="x")
    assert PolicyResult(ok=True).findings == []


# ---------------------------------------------------------------- state


def test_state_constants_and_fields():
    assert MAX_CLARIFY_ROUNDS == 2
    assert MAX_ARTIFACT_REVISIONS == 1
    state = initial_state("/tmp/x")
    assert state["retrieval"] == {}
    assert state["episodes"] == []
    assert state["readiness"] == {}
    assert state["runner_plan"] == {}
    assert state["generated_artifacts"] == {}
    assert state["policy"] == []
    assert state["clarify_rounds"] == 0
    assert state["follow_up_decision"] == {}
    assert state["artifact_review"] == {}
    assert state["env_state"]["platform"] == "local"
    assert state["env_state"]["cloud_apply"] is False
    assert state["env_state"]["past_uat"] is False
    hints = typing.get_type_hints(PipelineState, include_extras=True)
    for key in ("runner_plan", "generated_artifacts", "artifact_review"):
        assert hints[key].__metadata__ == (merge_dict,)
    for key in ("policy", "clarify_rounds"):
        assert hints[key].__metadata__ == (operator.add,)
    for key in ("retrieval", "episodes", "readiness", "follow_up_decision"):
        assert key in hints


def _results(*rows: tuple[str, str]) -> dict:
    return {
        "pipeline": {"id": "p", "stages": [{"name": "build"}, {"name": "scan_images"}, {"name": "uat"}]},
        "stage_results": [{"stage": name, "plan_id": "p", "status": status, "blocked": False} for name, status in rows],
    }


def test_planned_counts_as_done_for_pending_and_dependencies():
    assert pending_stages(_results(("build", "pass"), ("scan_images", "planned"), ("uat", "pass"))) == []
    assert pending_stages(_results(("build", "pass"), ("scan_images", "fail"))) == ["scan_images", "uat"]
    assert pending_stages(_results(("build", "pass"))) == ["scan_images", "uat"]
    assert planned_stages(_results(("build", "pass"), ("scan_images", "planned"))) == ["scan_images"]
    assert set(SATISFIED_STATUSES) == {"pass", "skipped", "planned"}
    assert _deps_met({"depends_on": ["scan_images"]}, {"scan_images": {"status": "planned"}}) is True
    assert _deps_met({"depends_on": ["scan_images"]}, {"scan_images": {"status": "fail"}}) is False


def test_a_planned_stage_is_not_dispatched_again():
    state = _results(("build", "pass"), ("scan_images", "planned"))
    state["pipeline"]["stages"] = [
        {"name": "build", "depends_on": []},
        {"name": "scan_images", "depends_on": ["build"]},
        {"name": "uat", "depends_on": ["scan_images"]},
    ]
    state.update({"gate": {}, "attempt": {}})
    assert ready_stage_names(state) == ["uat"]


def test_platform_and_runner_helpers():
    assert platform_of({"env_platform": " AWS "}) == "aws"
    assert platform_of({"env_platform": "mainframe"}) == "local"
    assert platform_of(None) == "local"
    assert runner_default_for("gcp") == "gcp_cloudbuild"
    assert runner_default_for("github") == "github_actions"
    assert runner_default_for("remote") == "ssh_remote"
    assert runner_default_for("nowhere") == "local"


# ---------------------------------------------------------------- normalize_stages / catalog


def _raw(name: str, deps: list[str] | None = None, **extra) -> dict:
    return {"name": name, "depends_on": deps or [], "purpose": f"{name} purpose", **extra}


def test_normalize_keeps_registry_and_custom_stages_and_their_fields():
    ordered, removed = normalize_stages(
        [
            _raw("build"),
            _raw(
                "container_scan",
                ["build"],
                runner="docker",
                provider="trivy",
                environment={"tier": "dev", "platform": "docker"},
                artifacts=[{"kind": "shell", "path": "ci/scan.sh", "purpose": "scan"}],
                stochastic=True,
            ),
            _raw("License_Audit", ["container_scan"]),
        ]
    )
    assert [stage["name"] for stage in ordered] == ["build", "container_scan", "license_audit"]
    assert removed == []
    build, scan, audit = ordered
    assert (build["kind"], build["family"], build["runner"]) == ("fixed", "build", "local")
    assert build["tool"] == TOOL_BINDINGS["build"]
    assert (scan["kind"], scan["family"], scan["runner"], scan["provider"]) == ("generated", "scan", "docker", "trivy")
    assert scan["environment"]["platform"] == "docker"
    assert scan["artifacts"][0]["path"] == "ci/scan.sh"
    assert scan["stochastic"] is True
    assert scan["tool"] == f"{registry.STAGE_TEMPLATES['container_scan'].summary} via docker"
    assert (audit["kind"], audit["family"], audit["tool"]) == ("generated", "custom", "custom stage via local")
    assert audit["environment"] == EnvironmentSpec().model_dump()


def test_normalize_still_breaks_cycles_and_ignores_unknown_or_production_names():
    ordered, removed = normalize_stages(
        [
            _raw("lint", ["license_audit"]),
            _raw("license_audit", ["lint"]),
            _raw("not-a-stage"),
            _raw("deploy_prod", ["lint"]),
            _raw("production"),
            _raw("Lint"),
            _raw("test", ["production", "ghost_stage"]),
        ]
    )
    names = [stage["name"] for stage in ordered]
    assert names == ["test", "license_audit", "lint"]
    assert {"license_audit->lint", "test->production", "test->ghost_stage"} <= set(removed)
    index = {name: position for position, name in enumerate(names)}
    for stage in ordered:
        assert all(index[dep] < index[stage["name"]] for dep in stage["depends_on"])
        assert not set(stage["name"].split("_")) & set(registry.FORBIDDEN_NAME_TOKENS)


def test_normalize_keeps_uat_last_and_nothing_depends_on_it():
    ordered, removed = normalize_stages(
        [_raw("uat"), _raw("build"), _raw("lint", ["uat"]), _raw("zeta_check", ["build"])]
    )
    assert [stage["name"] for stage in ordered][-1] == "uat"
    assert "lint->uat" in removed
    assert "uat->moved_last" in removed
    assert all("uat" not in stage["depends_on"] for stage in ordered)
    in_place, none_removed = normalize_stages([_raw("build"), _raw("uat", ["build"])])
    assert [stage["name"] for stage in in_place] == ["build", "uat"]
    assert "uat->moved_last" not in none_removed
    docker_uat, _ = normalize_stages([_raw("build"), _raw("uat", ["build"], runner="docker")])
    assert docker_uat[-1]["runner"] == "local"


def test_normalize_resets_unknown_runners_and_production_environments():
    ordered, removed = normalize_stages(
        [_raw("lint", runner="mainframe", environment={"tier": "production"})]
    )
    assert ordered[0]["runner"] == "local"
    assert ordered[0]["environment"]["tier"] == "local"
    assert {"lint->runner_reset", "lint->environment_reset"} <= set(removed)


def test_normalize_can_limit_a_plan_to_fixed_stages():
    ordered, removed = normalize_stages(
        [_raw("build"), _raw("lint", ["build"]), _raw("test", ["lint"]), _raw("uat", ["test"])],
        allow_generated=False,
    )
    assert [stage["name"] for stage in ordered] == ["build", "test", "uat"]
    assert "lint->generated_stage_unavailable" in removed
    assert "test->lint" in removed


def test_catalog_has_no_commands_for_generated_stages():
    assert commands_for("lint", {}, None) == []
    assert commands_for("license_audit", {}, None) == []
    assert commands_for("test", {}, None)[0]["label"] == "pytest"
    for bad in ("production", "deploy-prod", "x"):
        with pytest.raises(ValueError, match="unknown stage"):
            commands_for(bad, {}, None)


# ---------------------------------------------------------------- the live graph


class _Astra:
    def write_questions(self, payload: dict) -> QuestionSet:
        return QuestionSet(questions=[])

    def brief(self, payload: dict) -> StageBrief:
        return StageBrief(summary=f"Brief for {payload.get('stage')}.", watch_for="exit code")

    def propose(self, payload: dict) -> PlanProposal:
        chain = [
            _stage("build"),
            _stage("lint", ["build"], stochastic=True),
            _stage("test", ["lint"]),
            _stage("uat", ["test"]),
        ]
        plain = [_stage("build"), _stage("test", ["build"]), _stage("uat", ["test"])]
        return _plan(
            _candidate(chain, "gen", mode="stochastic"),
            _candidate(plain, "fixed"),
        )

    def diagnose(self, payload: dict) -> Diagnosis:
        return Diagnosis(summary="none", retry_advice="none")


class _Jev:
    def clarify(self, payload: dict) -> ClarifyDecision:
        return ClarifyDecision(action="skip", rationale="no gaps")

    def rank(self, payload: dict) -> RankDecision:
        ids = [item["id"] for item in payload["candidates"]]
        return RankDecision(chosen_id=ids[0], ordered_ids=ids, drop_stages=[], rationale="first")

    def gate(self, payload: dict) -> GateDecision:
        return GateDecision(route="pass", retry=False, retry_stages=[], replan=False, rationale="ok")


def _approve(payload: dict) -> dict:
    assert payload["gate"] in {"pipeline_approval", "uat_signoff"}
    return {"decision": "approved"}


def test_graph_runs_only_fixed_stages_when_astra_proposes_a_generated_one(tmp_path):
    echo = [sys.executable, "-c", "print('ok')"]
    graph = build_graph(
        MemorySaver(), jev=_Jev(), astra=_Astra(), commands={name: [echo] for name in STAGE_NAMES}
    )
    values = drive(graph, initial_state(str(tmp_path)), _approve, echo=False)
    pipeline = values["pipeline"]
    assert pipeline["id"] == "gen-1"
    assert [stage["name"] for stage in pipeline["stages"]] == ["build", "test", "uat"]
    assert "lint->generated_stage_unavailable" in pipeline["dropped_edges"]
    assert all(stage["kind"] == "fixed" for stage in pipeline["stages"])
    assert pipeline["stages"][-1]["runner"] == "local"
    seen = {stage["name"] for item in values["candidates"] for stage in item["stages"]}
    assert "lint" in seen  # Jev still ranked the candidate that carried it
    assert values["status"] == "passed"
    assert {row["stage"] for row in values["stage_results"]} == {"build", "test", "uat"}
    assert values["approvals"]["uat"] == "approved"
