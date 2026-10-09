import { useEffect, useState } from "react"
import Dashboard from "./Dashboard.jsx"
import SupportApp from "./SupportApp.jsx"

function routeFromHash() {
  return window.location.hash.startsWith("#/orchestrator") ? "orchestrator" : "support"
}

export default function App() {
  const [route, setRoute] = useState(routeFromHash)

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  return (
    <>
      <nav className="app-nav">
        <a href="#/support" aria-current={route === "support" ? "page" : undefined}>
          Support
        </a>
        <a
          href="#/orchestrator"
          aria-current={route === "orchestrator" ? "page" : undefined}
        >
          Orchestrator
        </a>
      </nav>
      {route === "orchestrator" ? <Dashboard /> : <SupportApp />}
    </>
  )
}
