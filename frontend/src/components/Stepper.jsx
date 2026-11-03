// The run's stations, left to right. Done, current, or pending; the error phase marks the
// station the run was at.

const STEPS = [
  { key: "connect", label: "Connect" },
  { key: "questions", label: "Questions" },
  { key: "plan", label: "Plan" },
  { key: "approve", label: "Approve" },
  { key: "run", label: "Run" },
  { key: "uat", label: "UAT" },
  { key: "results", label: "Results" },
]

export function stationFor(session) {
  const phase = session?.phase || "starting"
  const trace = session?.trace || []
  if (phase === "idle" || phase === "starting") {
    return 0
  }
  if (phase === "questions" || phase === "follow_up") {
    return 1
  }
  if (phase === "approval") {
    return 3
  }
  if (phase === "uat") {
    return 5
  }
  if (phase === "results") {
    return 6
  }
  if (phase === "error") {
    return trace.includes("approve_pipeline") ? 4 : trace.includes("plan_pipeline") ? 3 : trace.includes("clarify_with_user") ? 2 : 1
  }
  // running: before the approval it is planning, after it the stages run
  if (trace.includes("approve_pipeline")) {
    return 4
  }
  if (trace.includes("clarify_with_user") || session?.has_answers) {
    return 2
  }
  return 1
}

export default function Stepper({ session }) {
  const current = stationFor(session)
  const failed = session?.phase === "error"
  return (
    <ol className="stepper" aria-label="Run progress">
      {STEPS.map((step, index) => {
        const state = index < current ? "done" : index === current ? (failed ? "failed" : "current") : "pending"
        return (
          <li key={step.key} className={`step step-${state}`} aria-current={index === current ? "step" : undefined}>
            <span className="step-dot" aria-hidden="true">
              {state === "done" ? "✓" : index + 1}
            </span>
            <span className="step-label">{step.label}</span>
          </li>
        )
      })}
    </ol>
  )
}
