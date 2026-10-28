// "Astra read these excerpts": the retrieval evidence grouped by topic plus the index stats.

function countOrLength(value) {
  if (Array.isArray(value)) {
    return value.length
  }
  return typeof value === "number" ? value : null
}

export default function RetrievalPanel({ retrieval, evidence }) {
  const stats = retrieval || {}
  const rows =
    Array.isArray(evidence) && evidence.length
      ? evidence
      : Array.isArray(stats.evidence)
        ? stats.evidence
        : []
  if (!rows.length && !stats.index_id) {
    return null
  }
  const groups = {}
  for (const row of rows) {
    const topic = row.topic || "other"
    if (!groups[topic]) {
      groups[topic] = []
    }
    groups[topic].push(row)
  }
  const files = countOrLength(stats.files)
  const chunks = countOrLength(stats.chunks)
  const statLine = stats.index_id
    ? `Index ${stats.index_id} · ${files ?? "?"} files · ${chunks ?? "?"} chunks · ${stats.retriever || "bm25"}`
    : "Retrieval over the checkout"

  return (
    <section className="panel" aria-label="Retrieval evidence">
      <h2>Astra read these excerpts</h2>
      <p className="card-note">
        {statLine}
        {stats.summary ? ` · ${stats.summary}` : ""}
      </p>
      {rows.length ? (
        <div className="retrieval-groups">
          {Object.entries(groups).map(([topic, hits]) => (
            <article className="retrieval-group" key={topic}>
              <h3>{topic}</h3>
              <ul className="retrieval-list">
                {hits.map((hit) => (
                  <li key={`${hit.path}:${hit.lines}`}>
                    <code>{hit.path}</code>
                    <span className="retrieval-lines">{hit.lines}</span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      ) : (
        <p className="empty">No excerpts were retrieved for the prompts.</p>
      )}
    </section>
  )
}
