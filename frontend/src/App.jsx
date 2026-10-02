import { useEffect, useState } from "react"

const EXAMPLES = [
  "Where is the order for alice@example.com?",
  "Is the keyboard in stock?",
  "What is your return policy?",
  "Estimate a refund for 40 delivered 45 days ago",
]

function errorMessage(data) {
  if (!data) {
    return "Request failed."
  }
  if (typeof data.detail === "string") {
    return data.detail
  }
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => item.msg || JSON.stringify(item)).join(" ")
  }
  return "Request failed."
}

export default function App() {
  const [question, setQuestion] = useState(EXAMPLES[0])
  const [statusLine, setStatusLine] = useState("Checking the API...")
  const [answer, setAnswer] = useState("")
  const [route, setRoute] = useState("")
  const [trace, setTrace] = useState(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch("/status")
      .then((response) => response.json())
      .then((data) => {
        const counts = data.counts || {}
        setStatusLine(
          `${data.model} · ${counts.orders ?? 0} orders · ${counts.products ?? 0} products`
        )
      })
      .catch(() => {
        setStatusLine("API is not reachable. Start uvicorn on port 8000.")
      })
  }, [])

  async function onSubmit(event) {
    event.preventDefault()
    setLoading(true)
    setError("")
    setAnswer("")
    setRoute("")
    setTrace(null)
    try {
      const response = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: question }),
      })
      const data = await response.json()
      if (!response.ok) {
        setError(errorMessage(data))
        return
      }
      setAnswer(data.answer)
      setRoute(data.route)
      setTrace(data.tool_trace)
    } catch {
      setError("Could not reach the API.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <main>
      <header>
        <h1>Support agent</h1>
        <p id="status-line">{statusLine}</p>
        <a href="/docs">API docs</a>
      </header>

      <form onSubmit={onSubmit}>
        <label htmlFor="question">Question</label>
        <textarea
          id="question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          rows={4}
        />
        <div className="examples">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setQuestion(example)}
            >
              {example}
            </button>
          ))}
        </div>
        <button id="send" type="submit" disabled={loading || !question.trim()}>
          {loading ? "Looking it up..." : "Send"}
        </button>
      </form>

      {error ? <p className="error">{error}</p> : null}

      {answer ? (
        <section>
          <h2>Answer</h2>
          {route ? <p className="route">Route: {route}</p> : null}
          <p id="answer">{answer}</p>
          {trace ? (
            <details>
              <summary>Tool trace</summary>
              <pre>{JSON.stringify(trace, null, 2)}</pre>
            </details>
          ) : null}
        </section>
      ) : null}
    </main>
  )
}
