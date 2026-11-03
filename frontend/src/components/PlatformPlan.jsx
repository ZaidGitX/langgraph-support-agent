import { NameChips, RunnerChip } from "./Chips.jsx"
import { Mark } from "./Mark.jsx"

// Sprint 4: what the platform run needs around the stages. Provisioning creates the platform
// objects the generated suite relies on (a CodeBuild project, a Jenkins job); injection copies
// vault values into the platform's own secret store, by name only.

function commandText(argv) {
  return (argv || []).map((part) => (/\s/.test(part) ? `"${part}"` : part)).join(" ")
}

function Block({ title, item, emptyText }) {
  if (!item || !item.status) {
    return (
      <div className="platform-block">
        <h4>{title}</h4>
        <p className="card-note">{emptyText}</p>
      </div>
    )
  }
  return (
    <div className="platform-block">
      <div className="candidate-head">
        <h4>{title}</h4>
        <Mark value={item.status} />
        <RunnerChip runner={item.runner} mode={item.mode} />
      </div>
      {item.target ? <p className="card-note">Target: {item.target}</p> : null}
      {item.names?.length ? (
        <p className="card-note">
          Names <NameChips names={item.names} tone="warn" />
        </p>
      ) : null}
      {item.missing?.length ? (
        <p className="readiness-missing">
          Missing <NameChips names={item.missing} />
        </p>
      ) : null}
      {item.commands?.length ? (
        <ul className="finding-list">
          {item.commands.map((argv, index) => (
            <li className="finding" key={`${index}-${argv[0]}`}>
              <code>{commandText(argv)}</code>
            </li>
          ))}
        </ul>
      ) : null}
      {item.evidence ? (
        <details>
          <summary>Evidence</summary>
          <pre>{item.evidence}</pre>
        </details>
      ) : null}
    </div>
  )
}

export default function PlatformPlan({ suite, provision, injection }) {
  const hasSuite = suite && suite.platform
  if (!hasSuite && !provision?.status && !injection?.status) {
    return null
  }
  const files = Object.entries(suite?.stage_files || {})
  return (
    <section className="panel" aria-label="Platform plan">
      <h2>Platform</h2>
      {hasSuite ? (
        <p className="card-note">
          The fixed stages run on <strong>{suite.platform}</strong> through the <code>{suite.runner}</code> runner with the
          generated suite{suite.status && suite.status !== "ok" ? ` (${suite.status})` : ""}.
        </p>
      ) : null}
      {files.length ? (
        <ul className="finding-list">
          {files.map(([stage, path]) => (
            <li className="finding" key={stage}>
              <span className="finding-rule">{stage}</span>
              <code>{path || "no file"}</code>
            </li>
          ))}
        </ul>
      ) : null}
      <Block title="Provisioning" item={provision} emptyText="Nothing to provision for this runner." />
      <Block title="Secret injection" item={injection} emptyText="No secret names to inject." />
    </section>
  )
}
