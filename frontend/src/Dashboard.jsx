import { useCallback, useEffect, useRef, useState } from "react"
import { apiFetch, errorDetail } from "./api.js"
import ArtifactViewer from "./components/ArtifactViewer.jsx"
import Candidates from "./components/Candidates.jsx"
import { EnvChip, KindChip, ModeBadge, NameChips, PolicyBadge, ProviderChip, RunnerChip } from "./components/Chips.jsx"
import Connections from "./components/Connections.jsx"
import Episodes from "./components/Episodes.jsx"
import { Mark, toneOf } from "./components/Mark.jsx"
import PlatformPlan from "./components/PlatformPlan.jsx"
import RetrievalPanel from "./components/RetrievalPanel.jsx"
import RunBoard from "./components/RunBoard.jsx"
import Stepper from "./components/Stepper.jsx"
import Timeline from "./components/Timeline.jsx"
import Toasts from "./components/Toasts.jsx"
import TokenPrompt from "./components/TokenPrompt.jsx"
import { useSession } from "./useSession.js"

// The shell: a stepper, the one action the person can take right now, and five tabs that
// stay in place for the whole run. The panels live in ./components; the data comes from
// useSession (server-sent events with polling as the fallback).

const CHECK_FAMILIES = new Set(["test", "scan", "verify"])
const CHECK_NAMES = new Set(["test", "scanners", "dast"])
const TABS = [
  { key: "overview", label: "Overview" },
  { key: "plan", label: "Plan" },
  { key: "artifacts", label: "Artifacts" },
  { key: "evidence", label: "Evidence" },
  { key: "history", label: "History" },
]
const PHASE_NOTICES = {
  questions: ["Astra is asking the ecosystem questions.", "warn"],
  follow_up: ["Jev asked for one more round of questions.", "warn"],
  approval: ["A pipeline is ready for your approval.", "warn"],
  uat: ["Every stage finished. UAT sign-off is yours.", "warn"],
  results: ["The run finished.", "ok"],
  error: ["The run stopped with an error.", "bad"],
}

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
        const confidence = typeof value.confidence === "number" ? ` (${value.confidence.toFixed(2)})` : ""
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
        const confidence = typeof value.confidence === "number" ? ` (${value.confidence.toFixed(2)})` : ""
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
      if (record?.policy && record.policy.ok === false) {
        return true
      }
    }
  }
  return false
}

export default function Dashboard({ initialSession = null }) {
  const { session, setSession, events, error, setError, live } = useSession(initialSession)
  const [draft, setDraft] = useState({})
  const [sending, setSending] = useState(false)
  const [showConnections, setShowConnections] = useState(false)
  const [tab, setTab] = useState("overview")
  const [toasts, setToasts] = useState([])
  const previousPhase = useRef(initialSession ? initialSession.phase : null)

  const pushToast = useCallback((text, tone = "neutral") => {
    setToasts((current) => [...current, { id: `${Date.now()}-${Math.random()}`, text, tone }])
  }, [])
  const dismissToast = useCallback((id) => setToasts((current) => current.filter((item) => item.id !== id)), [])

  useEffect(() => {
    const previous = document.title
    document.title = "CI/CD orchestrator"
    return () => {
      document.title = previous
    }
  }, [])

  useEffect(() => {
    if (session?.phase !== "idle") {
      return
    }
    apiFetch("/api/orchestrator/session", { method: "POST" }).catch(() => {})
  }, [session?.phase])

  useEffect(() => {
    if (session?.phase !== "results" || session.results_viewed) {
      return
    }
    apiFetch("/api/orchestrator/session/viewed", { method: "POST" }).catch(() => {})
  }, [session?.phase, session?.results_viewed])

  const phase = session?.phase || "starting"
  useEffect(() => {
    if (previousPhase.current === phase) {
      return
    }
    const notice = PHASE_NOTICES[phase]
    if (notice && previousPhase.current !== null) {
      pushToast(notice[0], notice[1])
    }
    previousPhase.current = phase
  }, [phase, pushToast])

  const questions = session?.questions || []
  const askingPhase = phase === "questions" || phase === "follow_up"
  const gateName = session?.gate || (phase === "follow_up" ? "follow_up_qa" : "ecosystem_qa")
  const clarifyRound = session?.clarify_round || 0

  useEffect(() => {
    setDraft({})
  }, [gateName, clarifyRound])

  const answersReady = questions.length > 0 && questions.every((question) => String(draft[question.id] || "").trim())

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
      const response = await apiFetch("/api/orchestrator/session/answer", {
        method: "POST",
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
      const response = await apiFetch("/api/orchestrator/session/answer", {
        method: "POST",
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

  async function runAgain() {
    if (sending) {
      return
    }
    setSending(true)
    setError("")
    try {
      const response = await apiFetch("/api/orchestrator/session", { method: "POST" })
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      setSession(await response.json())
      setTab("overview")
    } catch {
      setError("A new run could not be started.")
    } finally {
      setSending(false)
    }
  }

  const report = session?.report
  const ran = Boolean(report?.chosen_pipeline_ran)
  const approvedNames = new Set(ran ? report?.approved_stages || [] : [])
  const resultStages = (report?.chosen_pipeline?.stages || []).filter((stage) => approvedNames.has(stage.name))
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
  const retrievalEvidence = session?.retrieval_evidence?.length ? session.retrieval_evidence : report?.retrieval?.evidence || []
  const plannedStages = report?.planned_stages || []
  const pipeline = session?.pipeline || report?.chosen_pipeline || null
  const running = session?.running_stages || []
  const boardStages = liveStages.length ? liveStages : resultStages
  const artifactCount = Object.values(artifacts).reduce((total, records) => total + (records || []).length, 0)
  const candidateCount = (session?.candidates || report?.candidates || []).length
  const policyCount = (session?.policy || report?.policy || []).length
  const lastNode = (session?.timeline || []).length ? session.timeline[session.timeline.length - 1].node : ""
  const badges = {
    overview: running.length ? `${running.length} running` : "",
    plan: candidateCount ? String(candidateCount) : "",
    artifacts: artifactCount ? String(artifactCount) : "",
    evidence: policyCount ? `${policyCount} findings` : "",
    history: events.length ? String(events.length) : "",
  }

  return (
    <main className="dashboard">
      <header>
        <p className="eyebrow">Session</p>
        <div className="title-row">
          <h1>Agentic CI/CD Orchestrator</h1>
          {session?.status ? <Mark value={session.status} large /> : null}
        </div>
        <p className="lede">
          Astra writes the questions, the candidate pipelines, and the files a generated stage needs. Jev chooses one
          pipeline and a runner per stage. You answer here. Only the stages in the approved pipeline run; a stage whose
          credentials are missing is planned, not skipped.
        </p>
        <Stepper session={session} />
        <div className="hero-meta">
          <p>{session?.run_id ? `Run id: ${session.run_id}` : "Opening a session…"}</p>
          {session?.trigger?.source ? (
            <p>
              Triggered by {session.trigger.source}
              {session.trigger.ref ? ` on ${String(session.trigger.ref).replace("refs/heads/", "")}` : ""}
              {session.trigger.sha ? ` at ${String(session.trigger.sha).slice(0, 8)}` : ""}
            </p>
          ) : null}
          {session?.autopilot?.enabled ? (
            <p className="autopilot-note">
              Autopilot on ({(session.autopilot.events || []).filter((item) => item.action !== "human").length} gate
              {(session.autopilot.events || []).filter((item) => item.action !== "human").length === 1 ? "" : "s"} answered, UAT
              stays human)
            </p>
          ) : null}
          {session?.queue?.length ? (
            <p className="autopilot-note">
              {session.queue.length} push{session.queue.length === 1 ? "" : "es"} waiting
            </p>
          ) : null}
          <p className={`live-indicator live-${live}`}>
            <span className="live-dot" aria-hidden="true" />
            {live === "sse" ? "live" : live === "polling" ? "polling" : "connecting"}
          </p>
          <button type="button" className="tab header-action" onClick={() => setShowConnections((current) => !current)} aria-pressed={showConnections}>
            {showConnections ? "Hide connections" : "Connections"}
          </button>
        </div>
      </header>

      <TokenPrompt required={false} />
      <Toasts items={toasts} onDismiss={dismissToast} />
      {showConnections && phase !== "idle" && phase !== "starting" ? <Connections /> : null}

      {error ? <p className="error">{error}</p> : null}
      {session?.error ? <p className="error">{session.error}</p> : null}

      {askingPhase ? (
        <form className="question-form action-card" onSubmit={submitAnswers}>
          <h2>{phase === "follow_up" ? `Follow-up questions (round ${clarifyRound || 2})` : "Ecosystem questions"}</h2>
          <p className="card-note">
            {phase === "follow_up"
              ? session?.follow_up?.rationale || "Jev asked for one more round after reading the retrieved code."
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
                onChange={(event) => setDraft((current) => ({ ...current, [question.id]: event.target.value }))}
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
        <section className="panel action-card">
          <h2>Approve the pipeline Jev chose</h2>
          <p className="card-note">
            {pipeline?.summary || "Jev selected one candidate."} Approving runs only the stages in the Plan tab. Rejecting runs none of
            them.
          </p>
          <p className="chip-row">
            {pipeline?.id ? <span className="chip">{pipeline.id}</span> : null}
            <ModeBadge mode={pipeline?.mode} />
            <EnvChip environment={pipeline?.environment} />
            <PolicyBadge policy={{ ok: !policyBlocked, errors: policyBlocked ? 1 : 0, warns: 0 }} />
          </p>
          {policyBlocked ? (
            <p className="approve-note">
              A generated artifact has a policy error, so this pipeline cannot be approved. Reject it and let Astra plan again.
            </p>
          ) : null}
          <div className="decision-actions">
            <button className="primary" type="button" disabled={sending || policyBlocked} onClick={() => submitDecision("pipeline_approval", "approved")}>
              Approve pipeline
            </button>
            <button className="danger" type="button" disabled={sending} onClick={() => submitDecision("pipeline_approval", "rejected")}>
              Reject pipeline
            </button>
          </div>
        </section>
      ) : null}

      {phase === "uat" ? (
        <section className="panel action-card">
          <h2>UAT sign-off</h2>
          <p className="card-note">There is no path past UAT. Sign off only if this run should stop here as accepted.</p>
          <div className="decision-actions">
            <button className="primary" type="button" disabled={sending} onClick={() => submitDecision("uat_signoff", "approved")}>
              Sign off UAT
            </button>
            <button className="danger" type="button" disabled={sending} onClick={() => submitDecision("uat_signoff", "rejected")}>
              Reject UAT
            </button>
          </div>
        </section>
      ) : null}

      {phase === "running" || phase === "starting" || phase === "idle" ? (
        <section className="panel action-card">
          <h2>{session?.has_answers ? "Pipeline in progress" : "Waiting for Astra"}</h2>
          <p className="card-note">
            {session?.has_answers
              ? "Astra proposes, Jev ranks, Jev picks runners, Astra writes artifacts, the policy checks them. Approved stages run below as they are dispatched."
              : "Astra is reading the checkout and writing the ecosystem questions."}
          </p>
          {lastNode ? (
            <p className="card-note">
              Latest node <code>{lastNode}</code>
              {running.length ? ` · running ${running.join(", ")}` : ""}
            </p>
          ) : null}
        </section>
      ) : null}

      {phase === "results" || phase === "error" ? (
        <section className="panel action-card">
          <h2>{phase === "results" ? "Run finished" : "Run stopped"}</h2>
          <p className="card-note">
            {phase === "results"
              ? `Status ${report?.status || session?.status || "—"}. The results stay on this screen; a new run recalls this one as an episode.`
              : "The error above explains what stopped the run."}
          </p>
          <div className="decision-actions">
            <button className="primary" type="button" disabled={sending} onClick={runAgain}>
              Run again
            </button>
          </div>
        </section>
      ) : null}

      <nav className="tabs" role="tablist" aria-label="Sections">
        {TABS.map((item) => (
          <button key={item.key} type="button" role="tab" aria-selected={tab === item.key} className={`tab${tab === item.key ? " active" : ""}`} onClick={() => setTab(item.key)}>
            {item.label}
            {badges[item.key] ? <span className="tab-badge">{badges[item.key]}</span> : null}
          </button>
        ))}
      </nav>

      {tab === "overview" ? (
        <div className="tab-panel">
          {phase === "idle" || phase === "starting" ? (
            <>
              <Connections />
              <Episodes />
            </>
          ) : null}
          {phase === "approval" ? (
            <>
              <Candidates candidates={session?.candidates} runnerPlan={runnerPlan} />
              <section className="panel">
                <h2>Stages</h2>
                <StageList stages={liveStages} evidence={[]} runnerPlan={runnerPlan} />
              </section>
              <ArtifactViewer artifacts={sessionArtifacts} review={artifactReview} />
              <PlatformPlan suite={session?.suite} provision={session?.provision} injection={session?.injection} />
              <section className="panel">
                <h2>Policy</h2>
                <PolicySummary policy={session?.policy} />
              </section>
            </>
          ) : null}
          {phase === "running" || phase === "uat" ? (
            <>
              <RunBoard stages={liveStages} running={running} events={events} runnerPlan={runnerPlan} />
              {phase === "running" && retrieval?.index_id && !liveStages.length ? <RetrievalPanel retrieval={retrieval} evidence={retrievalEvidence} /> : null}
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
                    <li>
                      <span>Approved by</span>
                      <strong>{report.approvals?.pipeline_by || "human"}</strong>
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
              <RunBoard stages={boardStages} running={[]} events={events} runnerPlan={runnerPlan} title="Stages as they ran" />
              <CheckBoard stages={resultStages} evidence={evidence} />
              {!ran ? (
                <section className="panel">
                  <h2>Stages</h2>
                  <p className="empty">
                    {proposedNames.length
                      ? `Jev proposed ${proposedNames.join(", ")}. That pipeline was not approved, so those stages did not run.`
                      : "No approved stages ran."}
                  </p>
                </section>
              ) : null}
              {report.diagnosis ? (
                <section className="panel">
                  <h2>Astra diagnosis</h2>
                  <p>{report.diagnosis}</p>
                </section>
              ) : null}
            </>
          ) : null}
          {phase === "error" ? <RunBoard stages={liveStages} running={[]} events={events} runnerPlan={runnerPlan} /> : null}
        </div>
      ) : null}

      {tab === "plan" ? (
        <div className="tab-panel">
          <Candidates candidates={session?.candidates || report?.candidates} runnerPlan={runnerPlan} />
          <section className="panel">
            <h2>Stages</h2>
            {boardStages.length ? (
              <StageList stages={boardStages} evidence={evidence} runnerPlan={runnerPlan} />
            ) : (
              <p className="empty">Jev has not chosen a pipeline yet.</p>
            )}
          </section>
          <PlatformPlan suite={session?.suite || report?.suite} provision={session?.provision || report?.provision} injection={session?.injection || report?.injection} />
          {session?.follow_up?.rationale ? (
            <section className="panel">
              <h2>Follow-up decision</h2>
              <p className="card-note">
                Jev chose <strong>{session.follow_up.action || "—"}</strong>: {session.follow_up.rationale}
              </p>
            </section>
          ) : null}
        </div>
      ) : null}

      {tab === "artifacts" ? (
        <div className="tab-panel">
          {artifactCount ? (
            <ArtifactViewer artifacts={artifacts} review={artifactReview} readOnly={phase !== "approval"} />
          ) : (
            <section className="panel">
              <h2>Generated artifacts</h2>
              <p className="empty">Astra has not written any files for this run yet.</p>
            </section>
          )}
          <section className="panel">
            <h2>Policy</h2>
            <PolicySummary policy={session?.policy || report?.policy} />
          </section>
        </div>
      ) : null}

      {tab === "evidence" ? (
        <div className="tab-panel">
          {phase === "results" && report ? <CheckBoard stages={resultStages} evidence={evidence} /> : null}
          <RetrievalPanel retrieval={retrieval} evidence={retrievalEvidence} />
          <Episodes episodes={report?.episodes || session?.episodes || undefined} />
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
              <p className="empty">No Jev decisions yet.</p>
            )}
          </section>
        </div>
      ) : null}

      {tab === "history" ? (
        <div className="tab-panel">
          <Timeline events={events} />
          {session?.autopilot?.events?.length ? (
            <section className="panel">
              <h2>Autopilot</h2>
              <ul className="finding-list">
                {session.autopilot.events.map((item, index) => (
                  <li className={`finding tone-${item.action === "human" ? "warn" : "ok"}`} key={`${item.gate}-${index}`}>
                    <span className="finding-rule">{item.gate}</span>
                    <span>{item.action}</span>
                    <span>{item.detail}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          {(session?.trace || []).length ? (
            <section className="panel">
              <h2>Graph trace</h2>
              <p className="trace-line">{(session.trace || []).join(" → ")}</p>
            </section>
          ) : null}
        </div>
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
        <PolicyBadge policy={{ ok: true, errors: 0, warns: 0 }} /> No findings in the generated files or commands.
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
          <li className={`finding tone-${row.severity === "error" ? "bad" : "warn"}`} key={`${row.stage}-${row.path}-${row.rule}-${index}`}>
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
    .filter(({ stage, row }) => CHECK_NAMES.has(stage.name) || CHECK_FAMILIES.has(stage.family || row?.family) || (row?.status || stage.status) === "planned")
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
            next_step: missing.length ? `Set ${missing.join(", ")} and run again.` : "Set ORCHESTRATOR_CLOUD_APPLY=1 and run again.",
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
        Each tile is one test, scan, or plan. It shows the status, the findings that tool printed, and the next step. An amber planned
        tile names the variables to set before that stage can apply.
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
                  <li className={`check-tile tone-${toneOf(check.status)}`} key={`${group.name}-${check.job || ""}-${check.name}-${index}`}>
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
              {(row?.kind || stage.kind) && (row?.kind || stage.kind) !== "fixed" ? <KindChip kind={row?.kind || stage.kind} /> : null}
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
