import { useCallback, useEffect, useState } from "react"
import { apiFetch, errorDetail } from "../api.js"
import { NameChips, RunModeBadge } from "./Chips.jsx"
import { Mark } from "./Mark.jsx"

// Platform connections (Sprint 4). One card per provider group. Values are write-only:
// the API never returns one, and this panel never keeps one in state after sending it.

const POLL_MS = 1500

function sourceLabel(row) {
  if (row.source === "vault") {
    return "set in the vault"
  }
  if (row.source === "env") {
    return "set in the environment"
  }
  return row.required ? "required" : "optional"
}

export default function Connections({ compact = false }) {
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState("")
  const [active, setActive] = useState("")
  const [notice, setNotice] = useState("")

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/orchestrator/connections")
      if (!response.ok) {
        setFailed(`The connections endpoint returned ${response.status}.`)
        return
      }
      setData(await response.json())
      setFailed("")
    } catch {
      setFailed("The connections endpoint is not reachable.")
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (failed && !data) {
    return (
      <section className="panel panel-compact" aria-label="Connections">
        <h2>Connections</h2>
        <p className="empty">{failed}</p>
      </section>
    )
  }
  if (!data) {
    return (
      <section className="panel panel-compact" aria-label="Connections">
        <h2>Connections</h2>
        <p className="empty">Checking which platforms are connected…</p>
      </section>
    )
  }

  const groups = data.groups || []
  const connected = groups.filter((group) => group.mode === "apply").length

  return (
    <section className="panel" aria-label="Connections">
      <div className="readiness-head">
        <h2>Connections</h2>
        <p className="card-note">
          {connected} of {groups.length} platforms can apply now. Connect a platform with its device-code login, or
          store its names in the encrypted vault. Values are never shown back; the run only ever sees names.
          {data.token_required ? " This orchestrator requires the dashboard token for every change." : ""}
        </p>
      </div>
      {notice ? <p className="card-note notice">{notice}</p> : null}
      <div className="readiness-grid">
        {groups.map((group) => (
          <ConnectionCard
            key={group.key}
            group={group}
            open={active === group.key}
            onOpen={() => setActive(active === group.key ? "" : group.key)}
            onChanged={(message) => {
              setNotice(message || "")
              refresh()
            }}
            compact={compact}
          />
        ))}
      </div>
    </section>
  )
}

function ConnectionCard({ group, open, onOpen, onChanged, compact }) {
  const tone = group.mode === "apply" ? "ok" : group.authenticated === false ? "bad" : "warn"
  const authLabel =
    group.authenticated === true ? "verified" : group.authenticated === false ? "check failed" : "not checked"
  return (
    <article className={`readiness-card tone-${tone}`}>
      <header className="candidate-head">
        <h3>{group.key}</h3>
        <RunModeBadge mode={group.mode} />
        <Mark value={authLabel} />
      </header>
      <p className="card-note">{group.label}</p>
      <ul className="fact-list">
        <li>
          <span>CLI</span>
          <strong>
            {group.cli === null || group.cli === undefined
              ? "not needed"
              : group.cli
                ? `${group.cli_name} present`
                : `${group.cli_name} absent`}
          </strong>
        </li>
        <li>
          <span>Names</span>
          <strong>
            {group.present.length + group.vault.filter((name) => !group.present.includes(name)).length} set
            {group.missing.length ? `, ${group.missing.length} missing` : ""}
          </strong>
        </li>
        {group.checked_at ? (
          <li>
            <span>Checked</span>
            <strong>{group.checked_at}</strong>
          </li>
        ) : null}
      </ul>
      {group.missing.length ? (
        <p className="readiness-missing">
          Missing <NameChips names={group.missing} />
        </p>
      ) : null}
      {!compact ? (
        <div className="decision-actions">
          <button type="button" className="tab" onClick={onOpen} aria-expanded={open}>
            {open ? "Close" : "Connect"}
          </button>
          {group.verify ? <VerifyButton group={group} onChanged={onChanged} /> : null}
        </div>
      ) : null}
      {open ? <ConnectForm group={group} onChanged={onChanged} /> : null}
    </article>
  )
}

function VerifyButton({ group, onChanged }) {
  const [busy, setBusy] = useState(false)
  return (
    <button
      type="button"
      className="tab"
      disabled={busy}
      onClick={async () => {
        setBusy(true)
        try {
          const response = await apiFetch(`/api/orchestrator/connections/${group.key}/verify`, { method: "POST" })
          if (!response.ok) {
            onChanged(await errorDetail(response))
            return
          }
          const body = await response.json()
          onChanged(`${group.key}: ${body.verified.ok ? "verified" : "not verified"}${body.verified.detail ? ` (${body.verified.detail})` : ""}`)
        } catch {
          onChanged("The verify call could not be sent.")
        } finally {
          setBusy(false)
        }
      }}
    >
      {busy ? "Checking…" : "Verify"}
    </button>
  )
}

function ConnectForm({ group, onChanged }) {
  const [values, setValues] = useState({})
  const [sending, setSending] = useState(false)
  const [error, setError] = useState("")
  const [flow, setFlow] = useState(null)
  const [code, setCode] = useState("")
  const ready = Object.values(values).some((value) => String(value || "").trim())

  useEffect(() => {
    if (!flow || ["done", "failed", "expired"].includes(flow.status)) {
      return undefined
    }
    const id = setInterval(async () => {
      try {
        const response = await fetch(`/api/orchestrator/connections/${group.key}/login/${flow.id}`)
        if (response.ok) {
          const body = await response.json()
          setFlow(body.flow)
          if (["done", "failed", "expired"].includes(body.flow.status)) {
            onChanged(`${group.key}: login ${body.flow.status}`)
          }
        }
      } catch {
        // keep polling
      }
    }, POLL_MS)
    return () => clearInterval(id)
  }, [flow, group.key, onChanged])

  async function startLogin() {
    setError("")
    try {
      const response = await apiFetch(`/api/orchestrator/connections/${group.key}/login`, { method: "POST" })
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      setFlow((await response.json()).flow)
    } catch {
      setError("The login could not be started.")
    }
  }

  async function sendCode(event) {
    event.preventDefault()
    if (!flow || !code.trim()) {
      return
    }
    try {
      const response = await apiFetch(`/api/orchestrator/connections/${group.key}/login/${flow.id}/input`, {
        method: "POST",
        body: JSON.stringify({ text: code.trim() }),
      })
      setCode("")
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      setFlow((await response.json()).flow)
    } catch {
      setError("The code could not be sent.")
    }
  }

  async function submitValues(event) {
    event.preventDefault()
    if (!ready || sending) {
      return
    }
    setSending(true)
    setError("")
    const payload = {}
    for (const [name, value] of Object.entries(values)) {
      if (String(value || "").trim()) {
        payload[name] = value
      }
    }
    try {
      const response = await apiFetch(`/api/orchestrator/connections/${group.key}/secrets`, {
        method: "POST",
        body: JSON.stringify({ values: payload }),
      })
      setValues({})
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      const body = await response.json()
      onChanged(`${group.key}: stored ${body.stored.join(", ")}`)
    } catch {
      setError("The values could not be sent.")
    } finally {
      setSending(false)
    }
  }

  async function forget(name) {
    try {
      const response = await apiFetch(`/api/orchestrator/connections/${group.key}/secrets/${name}`, { method: "DELETE" })
      if (!response.ok) {
        setError(await errorDetail(response))
        return
      }
      onChanged(`${group.key}: forgot ${name}`)
    } catch {
      setError("The value could not be removed.")
    }
  }

  return (
    <div className="connect-form">
      {group.login && (group.login.available || group.login.reason) ? (
        <div className="connect-login">
          <h4>Sign in with the {group.cli_name} CLI</h4>
          <p className="card-note">{group.login.note || "A device-code login on the orchestrator host."}</p>
          {group.login.available ? (
            <button type="button" className="primary" onClick={startLogin} disabled={flow && flow.status === "waiting"}>
              {flow && flow.status === "waiting" ? "Waiting for the browser…" : "Start device-code login"}
            </button>
          ) : (
            <p className="card-note">Not available: {group.login.reason}</p>
          )}
          {flow ? (
            <div className={`login-flow tone-${flow.status === "done" ? "ok" : flow.status === "failed" || flow.status === "expired" ? "bad" : "warn"}`}>
              <p>
                <Mark value={flow.status} />{" "}
                {flow.url ? (
                  <>
                    Open <a href={flow.url} target="_blank" rel="noreferrer">{flow.url}</a>
                  </>
                ) : (
                  "Starting the CLI…"
                )}
                {flow.code ? (
                  <>
                    {" "}
                    and enter the code <code className="chip chip-ok">{flow.code}</code>
                  </>
                ) : null}
              </p>
              {flow.needs_input ? (
                <form onSubmit={sendCode} className="inline-form">
                  <input
                    type="text"
                    autoComplete="off"
                    placeholder={`paste the ${flow.input_label || "code"}`}
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    aria-label={flow.input_label || "code"}
                  />
                  <button type="submit" className="tab" disabled={!code.trim()}>
                    Send
                  </button>
                </form>
              ) : null}
              {flow.message ? (
                <details>
                  <summary>CLI output</summary>
                  <pre>{flow.message}</pre>
                </details>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
      <form onSubmit={submitValues} className="vault-form">
        <h4>Store names in the vault</h4>
        <p className="card-note">
          Write-only: a value is encrypted on the orchestrator host and exported to the run. It is never displayed
          again. Leave a field empty to keep what is set.
        </p>
        {group.env.map((row) => (
          <label key={row.name} htmlFor={`cred-${group.key}-${row.name}`}>
            <span className="cred-name">
              <code>{row.name}</code>
              <span className={`cred-state ${row.set ? "cred-set" : ""}`}>{sourceLabel(row)}</span>
              {row.source === "vault" ? (
                <button type="button" className="link" onClick={() => forget(row.name)}>
                  forget
                </button>
              ) : null}
            </span>
            <input
              id={`cred-${group.key}-${row.name}`}
              type={row.kind === "secret" ? "password" : "text"}
              autoComplete="off"
              placeholder={row.purpose}
              value={values[row.name] || ""}
              onChange={(event) => setValues((current) => ({ ...current, [row.name]: event.target.value }))}
            />
          </label>
        ))}
        {error ? <p className="error">{error}</p> : null}
        <div className="decision-actions">
          <button className="primary" type="submit" disabled={!ready || sending}>
            {sending ? "Storing…" : "Store in the vault"}
          </button>
          {group.docs ? (
            <a className="tab" href={group.docs} target="_blank" rel="noreferrer">
              Docs
            </a>
          ) : null}
        </div>
      </form>
    </div>
  )
}
