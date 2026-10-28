// Small inline labels shared by the Sprint 3 panels. Props only; no fetches.

export function RunnerChip({ runner, mode }) {
  if (!runner) {
    return null
  }
  const tone = mode === "plan" ? " chip-warn" : mode === "apply" ? " chip-ok" : ""
  return (
    <span className={`chip chip-runner${tone}`} title={mode ? `${runner} runs in ${mode} mode` : runner}>
      {runner}
      {mode ? <small>{mode}</small> : null}
    </span>
  )
}

export function ProviderChip({ provider }) {
  if (!provider) {
    return null
  }
  return <span className="chip chip-provider">{provider}</span>
}

export function EnvChip({ environment }) {
  const tier = environment?.tier
  const platform = environment?.platform
  if (!tier && !platform) {
    return null
  }
  return <span className="chip chip-env">{[platform, tier].filter(Boolean).join(" · ")}</span>
}

export function KindChip({ kind }) {
  if (!kind) {
    return null
  }
  return <span className="chip chip-kind">{kind}</span>
}

export function ModeBadge({ mode }) {
  const value = mode === "stochastic" ? "stochastic" : "deterministic"
  return <span className={`badge-mode badge-mode-${value}`}>{mode || value}</span>
}

export function RunModeBadge({ mode }) {
  const value = mode === "apply" ? "apply" : "plan"
  return <span className={`policy-badge ${value === "apply" ? "ok" : "warn"}`}>{value}</span>
}

export function PolicyBadge({ policy }) {
  if (!policy || typeof policy !== "object") {
    return <span className="policy-badge">unchecked</span>
  }
  const errors = Number(policy.errors || 0)
  const warns = Number(policy.warns || 0)
  if (policy.ok === false || errors > 0) {
    const count = errors || 1
    return <span className="policy-badge error">{`${count} error${count === 1 ? "" : "s"}`}</span>
  }
  if (warns > 0) {
    return <span className="policy-badge warn">{`${warns} warning${warns === 1 ? "" : "s"}`}</span>
  }
  return <span className="policy-badge ok">ok</span>
}

export function NameChips({ names, tone = "missing" }) {
  const list = Array.isArray(names) ? names.filter(Boolean) : []
  if (!list.length) {
    return null
  }
  return (
    <span className="chip-row">
      {list.map((name) => (
        <code className={`chip chip-${tone}`} key={name}>
          {name}
        </code>
      ))}
    </span>
  )
}
