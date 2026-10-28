import { useEffect, useState } from "react"
import ArtifactViewer from "./components/ArtifactViewer.jsx"
import Candidates from "./components/Candidates.jsx"
import { EnvChip, KindChip, ModeBadge, NameChips, PolicyBadge, ProviderChip, RunnerChip } from "./components/Chips.jsx"
import Episodes from "./components/Episodes.jsx"
import { Mark, toneOf } from "./components/Mark.jsx"
import Readiness from "./components/Readiness.jsx"
import RetrievalPanel from "./components/RetrievalPanel.jsx"

// Phase router and data fetcher. The panels live in ./components; this file decides which
// of them a phase shows and posts every human answer to the session API.

const CHECK_FAMILIES = new Set(["test", "scan", "verify"])
const CHECK_NAMES = new Set(["test", "scanners", "dast"])

function choiceLabel(choice) {
  if (!choice || typeof choice !== "object") {
    return "—"
  }
  const entries = Object.entries(choice)
  if (!entries.length) {
    return "—"
  }
  return entries
    .map(([name, value]) => {
      if (value && typeof value === "object" && "choice" in value) {
        const confidence =
          typeof value.confidence === "number" ? ` (${value.confidence.toFixed(2)})` : ""
        return `${name}=${value.choice}${confidence}`
      }
      return `${name}=${JSON.stringify(value)}`
    })
    .join(", ")
}

function runnerLines(item) {
  const entries = Object.entries(item?.choice || {})
  if (entries.length) {
    return entries.map(([name, value]) => {
      const stage = name.replace(/^runner:/, "")
      if (value && typeof value === "object" && "choice" in value) {
        const confidence =
          typeof value.confidence === "number" ? ` (${value.confidence.toFixed(2)})` : ""
        return `${stage} → ${value.choice}${confidence}`
      }
      return `${stage} → ${JSON.stringify(value)}`
    })
  }
  return Object.entries(item?.decision?.runners || {}).map(([stage, runner]) => `${stage} → ${runner}`)
}

function noulLabel(noul) {
  if (!noul || typeof noul !== "object" || Object.keys(noul).length === 0) {
    return "—"
  }
  return Object.entries(noul)
    .map(([name, value]) => `${name}=${typeof value === "number" ? value.toFixed(2) : value}`)
    .join(", ")
}

function latestEvidence(evidence, name) {
  const rows = (evidence || []).filter((row) => row.stage === name)
  return rows.length ? rows[rows.length - 1] : null
}

function missingNames(row, stage) {
  const plan = row?.plan || stage?.plan || {}
  return Array.isArray(plan.missing) ? plan.missing : []
}

function hasPolicyErrors(artifacts) {
  for (const records of Object.values(artifacts || {})) {
    for (const record of records || []) {
      const policy = record?.policy
      if (policy && policy.ok === false) {
        return true
      }
    }
  }
  return false
}

async function errorDetail(response) {
  try {
    const body = await response.json()
    if (typeof body.detail === "string") {
      return body.detail
    }
  } catch {
    // The body was not JSON.
  }
  return `The orchestrator returned ${response.status}.`
}

export default function Dashboard() {
  const [session, setSession] = useState(null)
  const [draft, setDraft] = useState({})
  const [error, setError] = useState("")
  const [sending, setSending] = useState(false)

  useEffect(() => {
    const previous = document.title
    document.title = "CI/CD orchestrator"
    return () => {
      document.title = previous
    }
  }, [])

  useEffect(() => {
    let stop = false

    async function tick() {
      try {
        const response = await fetch("/api/orchestrator/session")
        if (!response.ok) {
          return
        }
        const data = await response.json()
        if (!stop) {
          setSession(data)
        }
      } catch {
        if (!stop) {
          setError("The orchestrator API is not reachable.")
        }
      }
    }

    tick()
    const id = setInterval(tick, 2000)
    return () => {
      stop = true
      clearInterval(id)
    }
  }, [])

  useEffect(() => {
    if (session?.phase !== "idle") {
      return
    }
    fetch("/api/orchestrator/session", { method: "POST" }).catch(() => {})
  }, [session?.phase])

  useEffect(() => {
    if (session?.phase !== "results" || session.results_viewed) {
      return
    }
    fetch("/api/orchestrator/session/viewed", { method: "POST" }).catch(() => {})
  }, [session?.phase, session?.results_viewed])

  const phase = session?.phase || "starting"
  const questions = session?.questions || []
  const askingPhase = phase === "questions" || phase === "follow_up"
  const gateName = session?.gate || (phase === "follow_up" ? "follow_up_qa" : "ecosystem_qa")
  const clarifyRound = session?.clarify_round || 0

  useEffect(() => {
    // A new round brings new question ids; never carry an earlier draft into it.
    setDraft({})
  }, [gateName, clarifyRound])

  const answersReady =
    questions.length > 0 &&
    questions.every((question) => String(draft[question.id] || "").trim())

  async function submitAnswers(event) {
    event.preventDefault()
    if (!answersReady || sending) {
      return
    }
    setSending(true)
    setError("")
    try {
      const answers = {}
      for (const question of questions) {
        answers[question.id] = String(draft[question.id] || "").trim()
      }
      const response = await fetch("/api/orchestrator/session/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ gate: gateName, answers }),
      })
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      setSession(await response.json())
    } catch {
      setError("The answers could not be sent.")
    } finally {
      setSending(false)
    }
  }

  async function submitDecision(gate, decision) {
    if (sending) {
      return
    }
    setSending(true)
    setError("")
    try {
      const response = await fetch("/api/orchestrator/session/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ gate, decision }),
      })
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      setSession(await response.json())
    } catch {
      setError("The decision could not be sent.")
    } finally {
      setSending(false)
    }
  }

  const report = session?.report
  const ran = Boolean(report?.chosen_pipeline_ran)
  const approvedNames = new Set(ran ? report?.approved_stages || [] : [])
  const resultStages = (report?.chosen_pipeline?.stages || []).filter((stage) =>
    approvedNames.has(stage.name),
  )
  const evidence = report?.stage_evidence || []
  const decisions = report?.jev_decisions || session?.jev_decisions || []
  const gate = report?.quality_gate || session?.quality_gate || {}
  const liveStages = (session?.stages || []).filter((stage) => stage.name)
  const rank = [...decisions].reverse().find((item) => item.kind === "rank")
  const proposedNames = (report?.chosen_pipeline?.stages || []).map((stage) => stage.name)
  const runnerPlan = session?.runner_plan || report?.runner_plan || {}
  const sessionArtifacts = session?.artifacts || {}
  const artifacts = Object.keys(sessionArtifacts).length ? sessionArtifacts : report?.generated_artifacts || {}
  const artifactReview = session?.artifact_review || report?.artifact_review || {}
  const policyBlocked = hasPolicyErrors(sessionArtifacts)
  const retrieval = report?.retrieval || session?.retrieval || {}
  const retrievalEvidence = session?.retrieval_evidence?.length
    ? session.retrieval_evidence
    : report?.retrieval?.evidence || []
  const plannedStages = report?.planned_stages || []
  const pipeline = session?.pipeline || report?.chosen_pipeline || null

  return (
    <main className="dashboard">
      <header>
        <p className="eyebrow">Session</p>
        <div className="title-row">
          <h1>Agentic CI/CD Orchestrator</h1>
          {session?.status ? <Mark value={session.status} large /> : null}
        </div>
        <p className="lede">
          Astra writes the questions, the candidate pipelines, and the files a generated stage needs. Jev
          chooses one pipeline and a runner per stage. You answer here. Only the stages in the approved
          pipeline run; a stage whose credentials are missing is planned, not skipped.
        </p>
        <div className="hero-meta">
          <p>{session?.run_id ? `Run id: ${session.run_id}` : "Opening a session…"}</p>
          <p>Support stays on #/support</p>
        </div>
      </header>

      {error ? <p className="error">{error}</p> : null}
      {session?.error ? <p className="error">{session.error}</p> : null}

      {askingPhase ? (
        <form className="question-form" onSubmit={submitAnswers}>
          <h2>
            {phase === "follow_up"
              ? `Follow-up questions (round ${clarifyRound || 2})`
              : "Ecosystem questions"}
          </h2>
          <p className="card-note">
            {phase === "follow_up"
              ? session?.follow_up?.rationale ||
                "Jev asked for one more round after reading the retrieved code."
              : "Astra wrote these from the checkout. The run stays on this screen until you submit an answer for every question. Nothing is filled in for you."}
          </p>
          {questions.map((question) => (
            <label key={question.id} htmlFor={`q-${question.id}`}>
              {question.prompt}
              <textarea
                id={`q-${question.id}`}
                name={question.id}
                value={draft[question.id] || ""}
                autoComplete="off"
                onChange={(event) =>
                  setDraft((current) => ({ ...current, [question.id]: event.target.value }))
                }
              />
              {question.reason ? <span className="card-note">{question.reason}</span> : null}
            </label>
          ))}
          <button className="primary" type="submit" disabled={!answersReady || sending}>
            Submit answers
          </button>
        </form>
      ) : null}

      {phase === "approval" ? (
        <>
          <section className="panel">
            <h2>Approve the pipeline Jev chose</h2>
            <p className="card-note">
              {pipeline?.summary || "Jev selected one candidate."} Approving runs only the stages below.
              Rejecting runs none of them.
            </p>
            <p className="chip-row">
              {pipeline?.id ? <span className="chip">{pipeline.id}</span> : null}
              <ModeBadge mode={pipeline?.mode} />
              <EnvChip environment={pipeline?.environment} />
            </p>
          </section>
          <Candidates candidates={session?.candidates} runnerPlan={runnerPlan} />
          <section className="panel">
            <h2>Stages</h2>
            <StageList stages={liveStages} evidence={[]} runnerPlan={runnerPlan} />
          </section>
          <ArtifactViewer artifacts={sessionArtifacts} review={artifactReview} />
          <section className="panel">
            <h2>Policy</h2>
            <PolicySummary policy={session?.policy} />
            {policyBlocked ? (
              <p className="approve-note">
                A generated artifact has a policy error, so this pipeline cannot be approved. Reject it and
                let Astra plan again.
              </p>
            ) : null}
            <div className="decision-actions">
              <button
                className="primary"
                type="button"
                disabled={sending || policyBlocked}
                onClick={() => submitDecision("pipeline_approval", "approved")}
              >
                Approve pipeline
              </button>
              <button
                className="danger"
                type="button"
                disabled={sending}
                onClick={() => submitDecision("pipeline_approval", "rejected")}
              >
                Reject pipeline
              </button>
            </div>
          </section>
          <RetrievalPanel retrieval={retrieval} evidence={retrievalEvidence} />
        </>
      ) : null}

      {phase === "uat" ? (
        <section className="panel">
          <h2>UAT sign-off</h2>
          <p className="card-note">
            There is no path past UAT. Sign off only if this run should stop here as accepted.
          </p>
          <StageList stages={liveStages} evidence={[]} runnerPlan={runnerPlan} />
          <div className="decision-actions">
            <button
              className="primary"
              type="button"
              disabled={sending}
              onClick={() => submitDecision("uat_signoff", "approved")}
            >
              Sign off UAT
            </button>
            <button
              className="danger"
              type="button"
              disabled={sending}
              onClick={() => submitDecision("uat_signoff", "rejected")}
            >
              Reject UAT
            </button>
          </div>
        </section>
      ) : null}

      {phase === "running" || phase === "starting" || phase === "idle" ? (
        <>
          <section className="panel">
            <h2>{session?.has_answers ? "Pipeline in progress" : "Waiting for Astra"}</h2>
            <p className="card-note">
              {session?.has_answers
                ? "Astra proposes, Jev ranks, Jev picks runners, Astra writes artifacts, the policy checks them. If a pipeline is already approved, only those stages run."
                : "Astra is reading the checkout and writing the ecosystem questions."}
            </p>
            {session?.has_answers && liveStages.length ? (
              <StageList stages={liveStages} evidence={[]} runnerPlan={runnerPlan} />
            ) : null}
          </section>
          {phase === "running" && retrieval?.index_id ? (
            <RetrievalPanel retrieval={retrieval} evidence={retrievalEvidence} />
          ) : null}
          {phase !== "running" ? (
            <>
              <Readiness readiness={session?.readiness} />
              <Episodes />
            </>
          ) : null}
        </>
      ) : null}

      {phase === "results" && report ? (
        <>
          <section className="summary-grid" aria-label="Run summary">
            <article className={`summary-card tone-${toneOf(report.status)}`}>
              <h2>Status</h2>
              <Mark value={report.status} large />
              <ul className="fact-list">
                <li>
                  <span>Past UAT</span>
                  <strong>{String(report.past_uat)}</strong>
                </li>
                <li>
                  <span>AWS called</span>
                  <strong>{String(report.aws_called)}</strong>
                </li>
                <li>
                  <span>Planned stages</span>
                  <strong>{plannedStages.length ? plannedStages.join(", ") : "none"}</strong>
                </li>
              </ul>
            </article>
            <article className="summary-card">
              <h2>Jev route</h2>
              <p className="card-value">{choiceLabel(rank?.choice)}</p>
              <p className="card-note">
                Quality gate <span className={`tone-word tone-${toneOf(gate.route)}`}>{gate.route || "—"}</span>
              </p>
            </article>
            <article className={`summary-card tone-${toneOf(gate.route)}`}>
              <h2>Approved stages</h2>
              <p className="card-value">{resultStages.map((stage) => stage.name).join(" → ") || "none ran"}</p>
              <p className="card-note">{report.chosen_pipeline?.summary || ""}</p>
              <p className="chip-row">
                <ModeBadge mode={report.chosen_pipeline?.mode} />
                <EnvChip environment={report.chosen_pipeline?.environment} />
              </p>
            </article>
          </section>

          <CheckBoard stages={resultStages} evidence={evidence} />

          <section className="panel">
            <h2>Stages</h2>
            {ran && resultStages.length ? (
              <StageList stages={resultStages} evidence={evidence} runnerPlan={runnerPlan} />
            ) : (
              <p className="empty">
                {proposedNames.length
                  ? `Jev proposed ${proposedNames.join(", ")}. That pipeline was not approved, so those stages did not run.`
                  : "No approved stages ran."}
              </p>
            )}
          </section>

          <ArtifactViewer artifacts={artifacts} review={artifactReview} readOnly />
          <RetrievalPanel retrieval={retrieval} evidence={retrievalEvidence} />
          <Episodes episodes={report.episodes || session?.episodes || []} />

          <section className="panel">
            <h2>Jev decisions</h2>
            {decisions.length ? (
              <div className="decision-list">
                {decisions.map((item, index) => (
                  <article className="decision" key={`${item.kind}-${index}`}>
                    <div className="decision-head">
                      <span className="eyebrow">Edge · {item.kind || "—"}</span>
                      <span className="trace-node">{item.schema || "—"}</span>
                    </div>
                    <dl className="kv-grid">
                      <div>
                        <dt>Client</dt>
                        <dd>{item.client || "—"}</dd>
                      </div>
                      <div>
                        <dt>Model</dt>
                        <dd>{item.model || "—"}</dd>
                      </div>
                      <div className="span-2">
                        <dt>Choice</dt>
                        <dd>
                          {item.kind === "runner" ? (
                            <ul className="id-list">
                              {runnerLines(item).map((line) => (
                                <li key={line}>{line}</li>
                              ))}
                            </ul>
                          ) : (
                            choiceLabel(item.choice)
                          )}
                        </dd>
                      </div>
                      <div className="span-2">
                        <dt>Noul</dt>
                        <dd>{noulLabel(item.noul)}</dd>
                      </div>
                      {item.kind === "artifact_review" && item.decision?.verdict ? (
                        <div>
                          <dt>Verdict</dt>
                          <dd>
                            <Mark value={item.decision.verdict} />
                          </dd>
                        </div>
                      ) : null}
                      {item.kind === "follow_up" && item.decision?.action ? (
                        <div>
                          <dt>Action</dt>
                          <dd>{item.decision.action}</dd>
                        </div>
                      ) : null}
                      {item.decision?.rationale ? (
                        <div className="span-2">
                          <dt>Rationale</dt>
                          <dd>{item.decision.rationale}</dd>
                        </div>
                      ) : null}
                    </dl>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty">No Jev decisions.</p>
            )}
          </section>

          {report.diagnosis ? (
            <section className="panel">
              <h2>Astra diagnosis</h2>
              <p>{report.diagnosis}</p>
            </section>
          ) : null}
        </>
      ) : null}
    </main>
  )
}

export function PolicySummary({ policy }) {
  const rows = Array.isArray(policy) ? policy : []
  const errors = rows.filter((row) => row.severity === "error").length
  const warns = rows.filter((row) => row.severity === "warn").length
  if (!rows.length) {
    return (
      <p className="card-note policy-summary">
        <PolicyBadge policy={{ ok: true, errors: 0, warns: 0 }} /> No findings in the generated files or
        commands.
      </p>
    )
  }
  return (
    <div className="policy-summary">
      <p className="card-note">
        <PolicyBadge policy={{ ok: errors === 0, errors, warns }} /> {rows.length} finding
        {rows.length === 1 ? "" : "s"} across the generated files.
      </p>
      <ul className="finding-list">
        {rows.slice(0, 30).map((row, index) => (
          <li
            className={`finding tone-${row.severity === "error" ? "bad" : "warn"}`}
            key={`${row.stage}-${row.path}-${row.rule}-${index}`}
          >
            <span className="finding-rule">
              {row.severity} · {row.rule}
            </span>
            <code>
              {row.stage}:{row.path}
              {row.line ? `:${row.line}` : ""}
            </code>
            <span>{row.message}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function CheckBoard({ stages, evidence }) {
  const groups = (stages || [])
    .map((stage) => ({ stage, row: latestEvidence(evidence, stage.name) }))
    .filter(
      ({ stage, row }) =>
        CHECK_NAMES.has(stage.name) ||
        CHECK_FAMILIES.has(stage.family || row?.family) ||
        (row?.status || stage.status) === "planned",
    )
    .map(({ stage, row }) => {
      const status = row?.status || stage.status || "pending"
      const runner = row?.runner || stage.runner || "local"
      const missing = missingNames(row, stage)
      let checks = Array.isArray(row?.checks) ? row.checks : []
      if (status === "planned" && !checks.length) {
        checks = [
          {
            name: `${runner} plan`,
            status: "planned",
            job: "",
            findings: row?.evidence || "",
            next_step: missing.length
              ? `Set ${missing.join(", ")} and run again.`
              : "Set ORCHESTRATOR_CLOUD_APPLY=1 and run again.",
          },
        ]
      }
      return { name: stage.name, status, runner, missing, checks }
    })
  if (!groups.length) {
    return null
  }
  return (
    <section className="panel" aria-label="Tests and scans">
      <div className="stage-head">
        <h2>Tests, scans, and planned stages</h2>
        <p className="legend">
          <span className="legend-label">Key</span>
          <Mark value="pass" />
          <Mark value="fail" />
          <Mark value="planned" />
          <Mark value="skip" />
        </p>
      </div>
      <p className="card-note">
        Each tile is one test, scan, or plan. It shows the status, the findings that tool printed, and
        the next step. An amber planned tile names the variables to set before that stage can apply.
      </p>
      <div className="check-groups">
        {groups.map((group) => (
          <article className="check-group" key={group.name}>
            <header className="check-group-head">
              <h3>{group.name}</h3>
              <RunnerChip runner={group.runner} mode={group.status === "planned" ? "plan" : undefined} />
              <Mark value={group.status} />
            </header>
            {group.status === "planned" && group.missing.length ? (
              <p className="stage-planned">
                Planned on {group.runner}: set <NameChips names={group.missing} /> and run again.
              </p>
            ) : null}
            {group.checks.length ? (
              <ul className="check-board">
                {group.checks.map((check, index) => (
                  <li
                    className={`check-tile tone-${toneOf(check.status)}`}
                    key={`${group.name}-${check.job || ""}-${check.name}-${index}`}
                  >
                    <span className="check-name">{check.name}</span>
                    {check.job ? <span className="check-job">{check.job}</span> : null}
                    <Mark value={check.status || "pending"} />
                    {check.findings ? (
                      <p className="check-findings">
                        <span>Findings</span>
                        {check.findings}
                      </p>
                    ) : null}
                    {check.next_step ? (
                      <p className="check-next">
                        <span>Next step</span>
                        {check.next_step}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">This stage finished as {group.status}. Step detail was not recorded.</p>
            )}
          </article>
        ))}
      </div>
    </section>
  )
}

export function StageList({ stages, evidence, runnerPlan }) {
  if (!stages.length) {
    return <p className="empty">No stages in this pipeline.</p>
  }
  return (
    <ol className="stage-rail">
      {stages.map((stage, index) => {
        const row = latestEvidence(evidence, stage.name)
        const status = row?.status || stage.status || (evidence.length ? "not run" : "")
        const brief = row?.brief || stage.brief || ""
        const text = row?.evidence || stage.evidence || ""
        const planned = (runnerPlan || {})[stage.name] || {}
        const runner = planned.runner || row?.runner || stage.runner || "local"
        const mode = planned.mode || (status === "planned" ? "plan" : undefined)
        const missing = missingNames(row, stage)
        return (
          <li key={stage.name} className="stage-stop">
            <div className="stage-index">{String(index + 1).padStart(2, "0")}</div>
            <div className="stage-titleline">
              <h3>{stage.name}</h3>
              {status ? <Mark value={status} /> : null}
            </div>
            <p className="chip-row stage-chips">
              <RunnerChip runner={runner} mode={mode} />
              <ProviderChip provider={row?.provider || stage.provider} />
              <EnvChip environment={stage.environment} />
              {(row?.kind || stage.kind) && (row?.kind || stage.kind) !== "fixed" ? (
                <KindChip kind={row?.kind || stage.kind} />
              ) : null}
            </p>
            <p className="stage-dep">after [{(stage.depends_on || []).join(", ") || "none"}]</p>
            {stage.purpose ? <p className="stage-purpose">{stage.purpose}</p> : null}
            {brief ? <p className="stage-purpose">{brief}</p> : null}
            {status === "planned" ? (
              <p className="stage-planned">
                {missing.length ? (
                  <>
                    Planned: set <NameChips names={missing} /> and run again.
                  </>
                ) : (
                  "Planned: set ORCHESTRATOR_CLOUD_APPLY=1 and run again."
                )}
              </p>
            ) : null}
            {text ? (
              <details>
                <summary>Evidence</summary>
                <pre>{text}</pre>
              </details>
            ) : null}
          </li>
        )
      })}
    </ol>
  )
}
