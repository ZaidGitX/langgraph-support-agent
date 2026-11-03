import { useEffect, useState } from "react"
import { KindChip, NameChips, ProviderChip, RunnerChip } from "./Chips.jsx"
import { Mark } from "./Mark.jsx"

// The approved DAG as live tiles. Rows are dispatch waves (stages that ran together); a tile
// flips to running when the session dispatches it and to its result when the stage returns.

function wavesFor(stages, events) {
  const waveOf = {}
  let count = 0
  for (const item of events || []) {
    if (item.kind === "wave" && Array.isArray(item.stages)) {
      count += 1
      for (const name of item.stages) {
        if (!(name in waveOf)) {
          waveOf[name] = count
        }
      }
    }
  }
  // Stages that have not been dispatched yet sit in later rows by dependency depth.
  const depth = {}
  const byName = Object.fromEntries((stages || []).map((stage) => [stage.name, stage]))
  const depthOf = (name, seen = new Set()) => {
    if (name in depth) {
      return depth[name]
    }
    if (seen.has(name)) {
      return 0
    }
    seen.add(name)
    const deps = (byName[name]?.depends_on || []).filter((dep) => dep in byName)
    depth[name] = deps.length ? 1 + Math.max(...deps.map((dep) => depthOf(dep, seen))) : 0
    return depth[name]
  }
  const rows = {}
  for (const stage of stages || []) {
    const key = stage.name in waveOf ? `wave-${waveOf[stage.name]}` : `later-${depthOf(stage.name)}`
    if (!rows[key]) {
      rows[key] = []
    }
    rows[key].push(stage)
  }
  const order = Object.keys(rows).sort((a, b) => {
    const [ka, na] = a.split("-")
    const [kb, nb] = b.split("-")
    if (ka !== kb) {
      return ka === "wave" ? -1 : 1
    }
    return Number(na) - Number(nb)
  })
  return order.map((key) => ({ key, stages: rows[key], label: key.startsWith("wave") ? `Wave ${key.split("-")[1]}` : "Waiting" }))
}

function startedAt(name, events) {
  let started = null
  for (const item of events || []) {
    if (item.kind === "wave" && Array.isArray(item.stages) && item.stages.includes(name)) {
      started = item.at
    }
  }
  return started
}

function finishedAt(name, events) {
  let finished = null
  for (const item of events || []) {
    if (item.kind === "stage" && item.name === name) {
      finished = item.at
    }
  }
  return finished
}

function elapsed(from, to) {
  if (!from) {
    return ""
  }
  const start = Date.parse(from)
  const end = to ? Date.parse(to) : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return ""
  }
  const total = Math.max(0, Math.round((end - start) / 1000))
  return total >= 60 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${total}s`
}

export default function RunBoard({ stages, running, events, runnerPlan, title = "Run board" }) {
  const [, setTick] = useState(0)
  const [selected, setSelected] = useState("")
  const active = (running || []).length > 0
  useEffect(() => {
    if (!active) {
      return undefined
    }
    const id = setInterval(() => setTick((value) => value + 1), 1000)
    return () => clearInterval(id)
  }, [active])
  if (!stages || !stages.length) {
    return null
  }
  const runningSet = new Set(running || [])
  const rows = wavesFor(stages, events)
  const chosen = stages.find((stage) => stage.name === selected)
  return (
    <section className="panel" aria-label={title}>
      <div className="stage-head">
        <h2>{title}</h2>
        <p className="legend">
          <span className="legend-label">Key</span>
          <Mark value="running" />
          <Mark value="pass" />
          <Mark value="planned" />
          <Mark value="fail" />
        </p>
      </div>
      <div className="run-board">
        {rows.map((row) => (
          <div className="run-wave" key={row.key}>
            <span className="run-wave-label">{row.label}</span>
            <div className="run-tiles">
              {row.stages.map((stage) => {
                const isRunning = runningSet.has(stage.name) || stage.status === "running"
                const status = isRunning ? "running" : stage.status || "pending"
                const plan = (runnerPlan || {})[stage.name] || {}
                const missing = Array.isArray(stage.plan?.missing) ? stage.plan.missing : []
                return (
                  <button
                    type="button"
                    key={stage.name}
                    className={`run-tile tone-${status === "running" ? "warn" : status === "pass" ? "ok" : status === "fail" || status === "blocked" ? "bad" : status === "planned" ? "warn" : "neutral"}${selected === stage.name ? " selected" : ""}`}
                    onClick={() => setSelected(selected === stage.name ? "" : stage.name)}
                    aria-pressed={selected === stage.name}
                  >
                    <span className="run-tile-head">
                      <strong>{stage.name}</strong>
                      {isRunning ? <span className="spinner" aria-label="running" /> : <Mark value={status} />}
                    </span>
                    <span className="chip-row">
                      <RunnerChip runner={plan.runner || stage.runner} mode={plan.mode || (status === "planned" ? "plan" : undefined)} />
                      <ProviderChip provider={stage.provider} />
                      {stage.kind && stage.kind !== "fixed" ? <KindChip kind={stage.kind} /> : null}
                    </span>
                    <span className="run-tile-meta">
                      {status === "running" ? `running ${elapsed(startedAt(stage.name, events), null)}` : null}
                      {status !== "running" && finishedAt(stage.name, events) ? `took ${elapsed(startedAt(stage.name, events), finishedAt(stage.name, events))}` : null}
                      {stage.attempt && stage.attempt > 1 ? ` · attempt ${stage.attempt}` : ""}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
      {chosen ? (
        <div className="run-drawer">
          <div className="candidate-head">
            <h3>{chosen.name}</h3>
            <Mark value={runningSet.has(chosen.name) ? "running" : chosen.status || "pending"} />
            <button type="button" className="link" onClick={() => setSelected("")}>
              close
            </button>
          </div>
          {chosen.purpose ? <p className="card-note">{chosen.purpose}</p> : null}
          {chosen.brief ? <p className="card-note">{chosen.brief}</p> : null}
          <p className="stage-dep">after [{(chosen.depends_on || []).join(", ") || "none"}]</p>
          {chosen.status === "planned" ? (
            <p className="stage-planned">
              {Array.isArray(chosen.plan?.missing) && chosen.plan.missing.length ? (
                <>
                  Planned: set <NameChips names={chosen.plan.missing} /> and run again.
                </>
              ) : (
                "Planned: set ORCHESTRATOR_CLOUD_APPLY=1 and run again."
              )}
            </p>
          ) : null}
          {chosen.evidence ? <pre>{chosen.evidence}</pre> : <p className="empty">No evidence yet.</p>}
        </div>
      ) : null}
    </section>
  )
}
