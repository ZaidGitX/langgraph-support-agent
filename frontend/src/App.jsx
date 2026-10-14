import { useEffect, useState } from "react"
import Dashboard from "./Dashboard.jsx"
import SupportApp from "./SupportApp.jsx"

function routeFromHash() {
  return window.location.hash.startsWith("#/support") ? "support" : "orchestrator"
}

export default function App() {
  const [route, setRoute] = useState(routeFromHash)

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const orchestrator = route === "orchestrator"

  return (
    <div className={orchestrator ? "app-shell app-shell--orchestrator" : "app-shell"}>
      <nav className="app-nav">
        <a href="#/support" aria-current={route === "support" ? "page" : undefined}>
          Support
        </a>
        <a href="#/orchestrator" aria-current={orchestrator ? "page" : undefined}>
          Orchestrator
        </a>
      </nav>
      {orchestrator ? <Dashboard /> : <SupportApp />}
    </div>
  )
}
