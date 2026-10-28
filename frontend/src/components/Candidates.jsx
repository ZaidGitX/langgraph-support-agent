import { EnvChip, ModeBadge, RunnerChip } from "./Chips.jsx"

// Every route Astra proposed. Jev ranked one; the bar is Jev's probability for each candidate.

function percent(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null
  }
  return Math.max(0, Math.min(100, Math.round(value * 100)))
}

export default function Candidates({ candidates, runnerPlan }) {
  const list = Array.isArray(candidates) ? candidates : []
  if (!list.length) {
    return null
  }
  return (
    <section className="panel" aria-label="Candidates">
      <h2>Candidates</h2>
      <p className="card-note">
        Astra proposed every route below. Jev ranked exactly one. A stochastic route carries stages Astra
        generated; a deterministic route uses only the fixed catalog.
      </p>
      <div className="candidate-grid">
        {list.map((item) => {
          const probability = percent(item.probability)
          return (
            <article className={`candidate-card${item.chosen ? " chosen" : ""}`} key={item.id}>
              <header className="candidate-head">
                <h3>{item.id}</h3>
                <ModeBadge mode={item.mode} />
                {item.chosen ? <span className="chip chip-ok">chosen</span> : null}
              </header>
              {item.summary ? <p className="card-note">{item.summary}</p> : null}
              <p className="chip-row">
                <EnvChip environment={item.environment} />
              </p>
              <ol className="candidate-stages">
                {(item.stages || []).map((name) => {
                  const planned = item.chosen ? (runnerPlan || {})[name] : null
                  const runner = planned?.runner || (item.runners || {})[name]
                  return (
                    <li key={name}>
                      <span>{name}</span>
                      <RunnerChip runner={runner} mode={planned?.mode} />
                    </li>
                  )
                })}
              </ol>
              {probability !== null ? (
                <>
                  <div
                    className="prob-bar"
                    role="meter"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={probability}
                    aria-label={`Jev probability for ${item.id}`}
                  >
                    <span style={{ width: `${probability}%` }} />
                  </div>
                  <p className="card-note">
                    Jev {probability}%{item.readiness_note ? ` · ${item.readiness_note}` : ""}
                  </p>
                </>
              ) : item.readiness_note ? (
                <p className="card-note">{item.readiness_note}</p>
              ) : null}
              {item.rationale ? <p className="rationale">{item.rationale}</p> : null}
            </article>
          )
        })}
      </div>
    </section>
  )
}
