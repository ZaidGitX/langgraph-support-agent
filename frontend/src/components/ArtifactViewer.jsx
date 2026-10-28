import { useEffect, useState } from "react"
import { KindChip, NameChips, PolicyBadge } from "./Chips.jsx"

// Generated files per stage. The session view carries a bounded, redacted copy of each file;
// the full text is fetched lazily from /session/artifacts/{stage}/{index} (also redacted).

function countOf(records, key) {
  return (records || []).reduce((total, record) => total + Number((record.policy || {})[key] || 0), 0)
}

export default function ArtifactViewer({ artifacts, review, readOnly = false }) {
  const stages = Object.keys(artifacts || {}).filter(
    (stage) => Array.isArray(artifacts[stage]) && artifacts[stage].length,
  )
  const stageKey = stages.join("|")
  const [active, setActive] = useState(stages[0] || "")
  const [full, setFull] = useState({})
  const [loading, setLoading] = useState("")

  useEffect(() => {
    if (!stages.includes(active)) {
      setActive(stages[0] || "")
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stageKey])

  if (!stages.length) {
    return null
  }
  const records = artifacts[active] || []
  const verdict = (review || {})[active] || {}

  async function loadFull(stage, index) {
    const key = `${stage}/${index}`
    if (full[key] || loading) {
      return
    }
    setLoading(key)
    try {
      const response = await fetch(
        `/api/orchestrator/session/artifacts/${encodeURIComponent(stage)}/${index}`,
      )
      if (response.ok) {
        const text = await response.text()
        setFull((current) => ({ ...current, [key]: text }))
      }
    } catch {
      // Keep the bounded copy.
    } finally {
      setLoading("")
    }
  }

  return (
    <section className="panel" aria-label="Generated artifacts">
      <h2>Generated artifacts</h2>
      <p className="card-note">
        Astra wrote these for the generated stages. The policy checked every file; secrets appear by name
        only. {readOnly ? "This is the record of what ran." : "Approve only when every badge is ok."}
      </p>
      <div className="artifact-tabs" role="tablist">
        {stages.map((stage) => {
          const rows = artifacts[stage]
          const errors = countOf(rows, "errors")
          return (
            <button
              key={stage}
              type="button"
              role="tab"
              aria-selected={stage === active}
              className={`tab${stage === active ? " active" : ""}${errors ? " tab-error" : ""}`}
              onClick={() => setActive(stage)}
            >
              {stage}
              <span className="tab-count">{rows.length}</span>
            </button>
          )
        })}
      </div>
      {verdict.verdict ? (
        <p className="card-note">
          Jev review <strong>{verdict.verdict}</strong>
          {typeof verdict.revisions === "number"
            ? ` after ${verdict.revisions} revision${verdict.revisions === 1 ? "" : "s"}`
            : ""}
          {verdict.rationale ? ` · ${verdict.rationale}` : ""}
        </p>
      ) : null}
      {records.map((record, index) => {
        const key = `${active}/${index}`
        const content = full[key] || record.content || ""
        const truncated = !full[key] && /\[truncated\]\s*$/.test(content)
        return (
          <article className="artifact" key={key}>
            <header className="artifact-head">
              <code className="artifact-path">{record.path}</code>
              <KindChip kind={record.kind} />
              <PolicyBadge policy={record.policy} />
              {record.bytes ? <span className="chip">{record.bytes} bytes</span> : null}
            </header>
            {record.purpose ? <p className="card-note">{record.purpose}</p> : null}
            {record.secrets_referenced?.length ? (
              <p className="card-note">
                Secrets referenced <NameChips names={record.secrets_referenced} tone="warn" />
              </p>
            ) : null}
            {record.findings?.length ? (
              <ul className="finding-list">
                {record.findings.map((finding, position) => (
                  <li
                    className={`finding tone-${finding.severity === "error" ? "bad" : "warn"}`}
                    key={`${finding.rule}-${finding.line}-${position}`}
                  >
                    <span className="finding-rule">
                      {finding.severity} · {finding.rule}
                    </span>
                    {finding.line ? <span className="finding-line">line {finding.line}</span> : null}
                    <span>{finding.message}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {content ? (
              <details>
                <summary>Content{truncated ? " (bounded copy)" : ""}</summary>
                <pre>{content}</pre>
                {truncated ? (
                  <button
                    type="button"
                    className="tab"
                    disabled={loading === key}
                    onClick={() => loadFull(active, index)}
                  >
                    {loading === key ? "Loading…" : "Load the full text"}
                  </button>
                ) : null}
              </details>
            ) : (
              <p className="empty">No content was stored for this artifact.</p>
            )}
          </article>
        )
      })}
    </section>
  )
}
