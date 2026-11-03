import { useEffect, useState } from "react"
import { readToken, saveToken } from "../api.js"

// Shown when the server requires X-Orchestrator-Token (the push workflow prints one next
// to the tunnel URL). The token is kept in sessionStorage for this tab only.
export default function TokenPrompt({ required }) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState("")

  useEffect(() => {
    const show = () => setOpen(true)
    window.addEventListener("orchestrator:token-required", show)
    return () => window.removeEventListener("orchestrator:token-required", show)
  }, [])

  useEffect(() => {
    if (required && !readToken()) {
      setOpen(true)
    }
  }, [required])

  if (!open) {
    return null
  }
  return (
    <section className="panel token-prompt" aria-label="Dashboard token">
      <h2>Dashboard token</h2>
      <p className="card-note">
        This orchestrator requires a token for every action. It is printed once, next to the public URL, in the
        Actions log or the start-up output. It is kept in this tab only.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          saveToken(value.trim())
          setValue("")
          setOpen(false)
        }}
      >
        <input
          type="password"
          autoComplete="off"
          placeholder="paste the token"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-label="Dashboard token"
        />
        <div className="decision-actions">
          <button className="primary" type="submit" disabled={!value.trim()}>
            Use this token
          </button>
          <button type="button" onClick={() => setOpen(false)}>
            Later
          </button>
        </div>
      </form>
    </section>
  )
}
