import { useEffect, useState } from "react"
import { NameChips, RunModeBadge } from "./Chips.jsx"

// Credential NAMES and CLIs only. The API never returns a value, so neither does this panel.

const TOGGLE_LABELS = {
  ORCHESTRATOR_DISPATCH: "dispatch",
  ORCHESTRATOR_CLOUD_APPLY: "cloud apply",
  ORCHESTRATOR_WRITE_ARTIFACTS: "write artifacts",
  ORCHESTRATOR_EMBEDDINGS: "embeddings",
  ORCHESTRATOR_MCP: "mcp",
  ORCHESTRATOR_ASTRA_TEMPERATURE: "astra temperature",
  cloud_apply: "cloud apply effective",
}

function hasProviders(readiness) {
  return Boolean(readiness && readiness.providers && Object.keys(readiness.providers).length)
}

function cliLabel(item) {
  if (item.cli === null || item.cli === undefined) {
    return "not needed"
  }
  if (item.cli) {
    return `${item.cli_name || "cli"} present`
  }
  return `${item.cli_name || "cli"} absent`
}

export default function Readiness({ readiness, compact = false }) {
  const [fetched, setFetched] = useState(null)
  const [failed, setFailed] = useState(false)
  const provided = hasProviders(readiness)
  const live = provided ? readiness : fetched

  useEffect(() => {
    if (provided) {
      return undefined
    }
    let stop = false
    fetch("/api/orchestrator/readiness")
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then((data) => {
        if (!stop) {
          setFetched(data)
        }
      })
      .catch(() => {
        if (!stop) {
          setFailed(true)
        }
      })
    return () => {
      stop = true
    }
  }, [provided])

  if (!live) {
    return (
      <section className="panel panel-compact" aria-label="Readiness">
        <h2>Readiness</h2>
        <p className="empty">
          {failed
            ? "The readiness endpoint is not reachable."
            : "Checking which CLIs and credential names are present on this host…"}
        </p>
      </section>
    )
  }

  const providers = Object.entries(live.providers || {})
  const app = live.app || {}
  const toggles = Object.entries(live.toggles || {})
  const runners = Object.entries(live.runners || {})

  return (
    <section className="panel" aria-label="Readiness">
      <div className="readiness-head">
        <h2>Readiness</h2>
        <p className="card-note">
          Names only, never a value. A provider in <strong>plan</strong> mode still gets its stage: the stage
          records <em>planned</em> and names what is missing.
        </p>
      </div>
      {app.missing?.length ? (
        <p className="readiness-missing">
          App names missing <NameChips names={app.missing} />
        </p>
      ) : null}
      <div className="readiness-grid">
        {providers.map(([key, item]) => (
          <article className={`readiness-card tone-${item.mode === "apply" ? "ok" : "warn"}`} key={key}>
            <header className="candidate-head">
              <h3>{key}</h3>
              <RunModeBadge mode={item.mode} />
            </header>
            {item.label ? <p className="card-note">{item.label}</p> : null}
            <ul className="fact-list">
              <li>
                <span>CLI</span>
                <strong>{cliLabel(item)}</strong>
              </li>
              <li>
                <span>Names present</span>
                <strong>{(item.present || []).length}</strong>
              </li>
              <li>
                <span>Runners</span>
                <strong>{(item.runners || []).join(", ") || "—"}</strong>
              </li>
            </ul>
            {item.missing?.length ? (
              <p className="readiness-missing">
                Missing <NameChips names={item.missing} />
              </p>
            ) : (
              <p className="card-note">Every name is set.</p>
            )}
          </article>
        ))}
      </div>
      {!compact && runners.length ? (
        <p className="chip-row toggles-row" aria-label="Runners">
          {runners.map(([name, item]) => (
            <span className={`chip chip-runner ${item.mode === "apply" ? "chip-ok" : "chip-warn"}`} key={name}>
              {name}
              <small>{item.mode || "plan"}</small>
            </span>
          ))}
        </p>
      ) : null}
      {!compact && toggles.length ? (
        <p className="chip-row toggles-row" aria-label="Toggles">
          {toggles.map(([name, value]) => (
            <span className="chip" key={name} title={name}>
              {TOGGLE_LABELS[name] || name} <strong>{String(value)}</strong>
            </span>
          ))}
        </p>
      ) : null}
    </section>
  )
}
