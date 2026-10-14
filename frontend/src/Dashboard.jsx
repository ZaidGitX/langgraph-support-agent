import { useEffect, useState } from "react"

function choiceLabel(choice) {
  if (!choice || typeof choice !== "object") {
    return "—"
  }
  return Object.entries(choice)
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

function noulLabel(noul) {
  if (!noul || typeof noul !== "object" || Object.keys(noul).length === 0) {
    return "—"
  }
  return Object.entries(noul)
    .map(([name, value]) => `${name}=${typeof value === "number" ? value.toFixed(2) : value}`)
    .join(", ")
}

function toneOf(value) {
  const text = String(value ?? "")
    .trim()
    .toLowerCase()
  if (["pass", "passed", "ok", "approved", "success"].includes(text)) {
    return "ok"
  }
  if (["fail", "failed", "failure", "error", "blocked", "escalate", "escalated", "rejected"].includes(text)) {
    return "bad"
  }
  if (text === "skip" || text === "skipped") {
    return "skip"
  }
  if (["pending", "warn", "warning", "remediate", "running", "starting"].includes(text)) {
    return "warn"
  }
  return "neutral"
}

function Mark({ value, large = false }) {
  const label = value === undefined || value === null || value === "" ? "—" : String(value)
  const tone = label === "—" ? "neutral" : toneOf(label)
  return <span className={`mark mark-${tone}${large ? " mark-lg" : ""}`}>{label}</span>
}

function latestEvidence(evidence, name) {
  const rows = (evidence || []).filter((row) => row.stage === name)
  return rows.length ? rows[rows.length - 1] : null
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
        body: JSON.stringify({ gate: "ecosystem_qa", answers }),
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

  return (
    <main className="dashboard">
      <header>
        <p className="eyebrow">Session</p>
        <div className="title-row">
          <h1>Agentic CI/CD Orchestrator</h1>
          {session?.status ? <Mark value={session.status} large /> : null}
        </div>
        <p className="lede">
          Astra writes the questions and the candidate pipelines. Jev chooses one pipeline.
          You answer here. Only the stages in the approved pipeline run.
        </p>
        <div className="hero-meta">
          <p>{session?.run_id ? `Run id: ${session.run_id}` : "Opening a session…"}</p>
          <p>Support stays on #/support</p>
        </div>
      </header>

      {error ? <p className="error">{error}</p> : null}
      {session?.error ? <p className="error">{session.error}</p> : null}

      {phase === "questions" ? (
        <form className="question-form" onSubmit={submitAnswers}>
          <h2>Ecosystem questions</h2>
          <p className="card-note">
            Astra wrote these from the checkout. The run stays on this screen until you submit
            an answer for every question. Nothing is filled in for you.
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
        <section className="panel">
          <h2>Approve the pipeline Jev chose</h2>
          <p className="card-note">
            {session?.pipeline?.summary || "Jev selected one candidate."} Approving runs only
            the stages below. Rejecting runs none of them.
          </p>
          <StageList stages={liveStages} evidence={[]} />
          <div className="decision-actions">
            <button
              className="primary"
              type="button"
              disabled={sending}
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
      ) : null}

      {phase === "uat" ? (
        <section className="panel">
          <h2>UAT sign-off</h2>
          <p className="card-note">
            There is no path past UAT. Sign off only if this run should stop here as accepted.
          </p>
          <StageList stages={liveStages} evidence={[]} />
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
        <section className="panel">
          <h2>{session?.has_answers ? "Pipeline in progress" : "Waiting for Astra"}</h2>
          <p className="card-note">
            {session?.has_answers
              ? "Astra proposes the DAGs. Jev chooses one. If a pipeline is already approved, only those stages run."
              : "Astra is reading the checkout and writing the ecosystem questions."}
          </p>
          {session?.has_answers && liveStages.length ? (
            <StageList stages={liveStages} evidence={[]} />
          ) : null}
        </section>
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
            </article>
          </section>

          <section className="panel">
            <h2>Stages</h2>
            {ran && resultStages.length ? (
              <StageList stages={resultStages} evidence={evidence} />
            ) : (
              <p className="empty">
                {proposedNames.length
                  ? `Jev proposed ${proposedNames.join(", ")}. That pipeline was not approved, so those stages did not run.`
                  : "No approved stages ran."}
              </p>
            )}
          </section>

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
                        <dd>{choiceLabel(item.choice)}</dd>
                      </div>
                      <div className="span-2">
                        <dt>Noul</dt>
                        <dd>{noulLabel(item.noul)}</dd>
                      </div>
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

function StageList({ stages, evidence }) {
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
        return (
          <li key={stage.name} className="stage-stop">
            <div className="stage-index">{String(index + 1).padStart(2, "0")}</div>
            <div className="stage-titleline">
              <h3>{stage.name}</h3>
              {status ? <Mark value={status} /> : null}
            </div>
            <p className="stage-dep">after [{(stage.depends_on || []).join(", ") || "none"}]</p>
            {stage.purpose ? <p className="stage-purpose">{stage.purpose}</p> : null}
            {brief ? <p className="stage-purpose">{brief}</p> : null}
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
