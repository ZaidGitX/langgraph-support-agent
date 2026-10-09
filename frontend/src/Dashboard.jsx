import { useEffect, useState } from "react"

const SAMPLE_URL = "/samples/live-run.json"
const LOCAL_URL = "/local-report.json"

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

export default function Dashboard() {
  const [report, setReport] = useState(null)
  const [source, setSource] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    loadUrl(SAMPLE_URL, "checked-in live run")
  }, [])

  async function loadUrl(url, label) {
    setError("")
    try {
      const response = await fetch(url)
      if (!response.ok) {
        setError(`${label} was not found (${response.status}).`)
        return
      }
      const data = await response.json()
      setReport(data)
      setSource(label)
    } catch {
      setError(`Could not load ${label}.`)
    }
  }

  function onFile(event) {
    const file = event.target.files && event.target.files[0]
    if (!file) {
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      try {
        setReport(JSON.parse(String(reader.result)))
        setSource(file.name)
        setError("")
      } catch {
        setError("That file is not JSON.")
      }
    }
    reader.readAsText(file)
  }

  const pipeline = report?.chosen_pipeline || {}
  const stages = pipeline.stages || []
  const gate = report?.quality_gate || {}
  const approvals = report?.approvals || {}
  const evidence = report?.stage_evidence || []
  const decisions = report?.jev_decisions || []
  const trace = report?.trace || []
  const langsmith = report?.langsmith || {}
  const cloudwatch = report?.cloudwatch || {}

  return (
    <main className="dashboard">
      <header>
        <h1>CI/CD orchestrator</h1>
        <p id="orch-source">
          {report
            ? `${source} · run ${report.run_id} · ${report.status}`
            : "Loading the live run report…"}
        </p>
        <div className="examples">
          <button type="button" onClick={() => loadUrl(SAMPLE_URL, "checked-in live run")}>
            Load checked-in run
          </button>
          <button type="button" onClick={() => loadUrl(LOCAL_URL, "var/report.json")}>
            Load var/report.json
          </button>
          <label className="file-label">
            Load a report file
            <input type="file" accept="application/json,.json" onChange={onFile} />
          </label>
        </div>
      </header>

      {error ? <p className="error">{error}</p> : null}

      {report ? (
        <>
          <section>
            <h2>Run history</h2>
            <p>
              One run is loaded. Status <strong id="orch-status">{report.status}</strong>. Past
              UAT <strong id="orch-past-uat">{String(report.past_uat)}</strong>. AWS called{" "}
              <strong>{String(report.aws_called)}</strong>.
            </p>
            <p id="orch-run-id">Run id: {report.run_id}</p>
          </section>

          <section>
            <h2>Chosen pipeline</h2>
            <p id="orch-pipeline">
              {pipeline.id || "—"} — {pipeline.summary || "No summary"}
            </p>
            <ol>
              {stages.map((stage) => (
                <li key={stage.name}>
                  {stage.name} after [{(stage.depends_on || []).join(", ") || "none"}]
                </li>
              ))}
            </ol>
          </section>

          <section>
            <h2>Node trace</h2>
            <p id="orch-trace">{trace.join(" → ") || "No trace"}</p>
          </section>

          <section>
            <h2>Jev decisions</h2>
            <table>
              <thead>
                <tr>
                  <th>Edge</th>
                  <th>Client</th>
                  <th>Model</th>
                  <th>Schema</th>
                  <th>Primitives</th>
                  <th>Choice</th>
                  <th>Noul</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((item, index) => (
                  <tr key={`${item.kind}-${index}`}>
                    <td>{item.kind}</td>
                    <td>{item.client}</td>
                    <td>{item.model}</td>
                    <td>{item.schema}</td>
                    <td>{(item.primitives || []).join(", ") || "—"}</td>
                    <td>{choiceLabel(item.choice)}</td>
                    <td>{noulLabel(item.noul)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section>
            <h2>Stage evidence</h2>
            <table>
              <thead>
                <tr>
                  <th>Stage</th>
                  <th>Status</th>
                  <th>Exit</th>
                  <th>Attempt</th>
                </tr>
              </thead>
              <tbody>
                {evidence.map((row) => (
                  <tr key={`${row.stage}-${row.attempt}`}>
                    <td>{row.stage}</td>
                    <td>{row.status}</td>
                    <td>{row.exit_code}</td>
                    <td>{row.attempt}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section>
            <h2>Quality gate</h2>
            <p id="orch-gate">
              Route <strong>{gate.route || "—"}</strong>. Retry {String(gate.retry)}.
            </p>
            <p>{gate.rationale}</p>
          </section>

          <section>
            <h2>Approvals</h2>
            <ul>
              <li>Pipeline: {approvals.pipeline || "—"}</li>
              <li>Deploy: {approvals.deploy || "—"}</li>
              <li>UAT: {approvals.uat || "—"}</li>
            </ul>
          </section>

          <section>
            <h2>LangSmith traces</h2>
            <p>{langsmith.note}</p>
            <p>
              Tracing configured: <strong>{String(langsmith.tracing_configured)}</strong>
            </p>
            <p>Request ids: {(langsmith.request_ids || []).join(", ") || "none recorded"}</p>
            <p>{(langsmith.trace || []).join(" → ")}</p>
          </section>

          <section>
            <h2>CloudWatch</h2>
            <p>{cloudwatch.note}</p>
            <p>
              CloudWatch called: <strong id="orch-cloudwatch">{String(cloudwatch.called)}</strong>
            </p>
            <ul>
              {(cloudwatch.stage_evidence || []).map((row) => (
                <li key={`cw-${row.stage}`}>
                  {row.stage}: {row.status} (exit {row.exit_code})
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2>A→Z report</h2>
            <pre id="orch-report">{report.report_markdown || "No markdown report"}</pre>
          </section>
        </>
      ) : null}
    </main>
  )
}
