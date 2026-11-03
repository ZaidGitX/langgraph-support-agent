import { useState } from "react"
import { Mark } from "./Mark.jsx"

// Every event the session published, oldest first: nodes, dispatch waves, stage results,
// Jev edges, gates, autopilot decisions, and the commands the runner printed.

const FILTERS = [
  { key: "all", label: "All" },
  { key: "stage", label: "Stages", kinds: ["wave", "stage"] },
  { key: "jev", label: "Jev", kinds: ["jev", "plan"] },
  { key: "gate", label: "Gates", kinds: ["gate", "autopilot", "done"] },
  { key: "note", label: "Commands", kinds: ["note"] },
]

function seconds(value) {
  if (typeof value !== "number") {
    return ""
  }
  return value >= 60 ? `${Math.floor(value / 60)}m ${Math.round(value % 60)}s` : `${value.toFixed(1)}s`
}

export default function Timeline({ events, limit = 300 }) {
  const [filter, setFilter] = useState("all")
  const chosen = FILTERS.find((item) => item.key === filter) || FILTERS[0]
  const rows = (events || []).filter((item) => !chosen.kinds || chosen.kinds.includes(item.kind)).slice(-limit)
  return (
    <section className="panel" aria-label="Timeline">
      <div className="stage-head">
        <h2>Timeline</h2>
        <p className="artifact-tabs" role="tablist">
          {FILTERS.map((item) => (
            <button key={item.key} type="button" role="tab" aria-selected={filter === item.key} className={`tab${filter === item.key ? " active" : ""}`} onClick={() => setFilter(item.key)}>
              {item.label}
            </button>
          ))}
        </p>
      </div>
      {rows.length ? (
        <ol className="timeline">
          {rows.map((item) => (
            <li key={item.seq} className={`timeline-row kind-${item.kind}`}>
              <span className="timeline-t">{seconds(item.t)}</span>
              <span className={`chip chip-kind kind-${item.kind}`}>{item.kind}</span>
              <span className="timeline-name">{item.name}</span>
              {item.status ? <Mark value={item.status} /> : null}
              {item.action ? <Mark value={item.action === "human" ? "warn" : item.action} /> : null}
              <span className="timeline-detail">{item.kind === "note" ? <code>{item.detail}</code> : item.detail}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="empty">Nothing has happened yet.</p>
      )}
    </section>
  )
}
