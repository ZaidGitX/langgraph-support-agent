// The single status renderer. Every panel maps a status word to one of five tones here.

export function toneOf(value) {
  const text = String(value ?? "")
    .trim()
    .toLowerCase()
  if (["pass", "passed", "ok", "approved", "success", "accept", "apply"].includes(text)) {
    return "ok"
  }
  if (
    ["fail", "failed", "failure", "error", "blocked", "escalate", "escalated", "rejected", "reject"].includes(text)
  ) {
    return "bad"
  }
  if (text === "skip" || text === "skipped") {
    return "skip"
  }
  if (
    ["pending", "warn", "warning", "remediate", "running", "starting", "planned", "plan", "revise"].includes(text)
  ) {
    return "warn"
  }
  return "neutral"
}

export function Mark({ value, large = false }) {
  const label = value === undefined || value === null || value === "" ? "—" : String(value)
  const tone = label === "—" ? "neutral" : toneOf(label)
  return <span className={`mark mark-${tone}${large ? " mark-lg" : ""}`}>{label}</span>
}
