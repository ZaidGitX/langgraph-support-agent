import { useEffect, useState } from "react"
import { ModeBadge } from "./Chips.jsx"
import { Mark } from "./Mark.jsx"

// Earlier runs on this checkout (episodic memory). With no prop the panel asks the API itself,
// so the idle screen can show what Astra will recall before the session starts.

function names(list) {
  return (Array.isArray(list) ? list : [])
    .map((item) => (typeof item === "string" ? item : item?.stage || item?.name || ""))
    .filter(Boolean)
}

export default function Episodes({ episodes, title = "Earlier runs on this checkout" }) {
  const provided = Array.isArray(episodes)
  const [fetched, setFetched] = useState(null)

  useEffect(() => {
    if (provided) {
      return undefined
    }
    let stop = false
    fetch("/api/orchestrator/episodes?limit=5")
      .then((response) => (response.ok ? response.json() : []))
      .then((data) => {
        if (!stop) {
          setFetched(Array.isArray(data) ? data : data?.episodes || [])
        }
      })
      .catch(() => {
        if (!stop) {
          setFetched([])
        }
      })
    return () => {
      stop = true
    }
  }, [provided])

  const list = provided ? episodes : fetched || []
  if (!list.length && !provided) {
    return null
  }
  return (
    <section className="panel" aria-label="Episodes">
      <h2>{title}</h2>
      <p className="card-note">
        Each run is recorded as an episode. Astra reads the newest ones before it writes the questions and
        the candidates.
      </p>
      {list.length ? (
        <ul className="episode-list">
          {list.map((episode) => {
            const chosen = episode.chosen || {}
            const planned = names(episode.planned)
            const failures = names(episode.failures)
            const where = [
              episode.branch,
              episode.sha,
              episode.platform && episode.tier ? `${episode.platform}/${episode.tier}` : episode.platform || episode.tier,
            ].filter(Boolean)
            return (
              <li key={episode.run_id}>
                <div className="episode-head">
                  <Mark value={episode.status} />
                  <strong>{chosen.id || "—"}</strong>
                  {chosen.mode ? <ModeBadge mode={chosen.mode} /> : null}
                  <span className="episode-time">{episode.finished_at || ""}</span>
                </div>
                <p className="episode-stages">{(chosen.stages || []).join(" → ") || "no stages ran"}</p>
                {planned.length ? <p className="card-note">Planned: {planned.join(", ")}</p> : null}
                {failures.length ? <p className="card-note">Failed: {failures.join(", ")}</p> : null}
                {episode.policy_errors ? (
                  <p className="card-note">Policy errors: {episode.policy_errors}</p>
                ) : null}
                {episode.lesson ? <p className="rationale">{episode.lesson}</p> : null}
                {where.length ? <p className="card-note">{where.join(" · ")}</p> : null}
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="empty">No earlier run is recorded for this checkout.</p>
      )}
    </section>
  )
}
