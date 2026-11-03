import { useEffect } from "react"

// Short notices for gate changes and errors. Each one dismisses itself after a few seconds.

export default function Toasts({ items, onDismiss }) {
  useEffect(() => {
    if (!items.length) {
      return undefined
    }
    const timers = items.map((item) => setTimeout(() => onDismiss(item.id), item.ttl || 6000))
    return () => timers.forEach((timer) => clearTimeout(timer))
  }, [items, onDismiss])
  if (!items.length) {
    return null
  }
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={`toast tone-${item.tone || "neutral"}`}>
          <span>{item.text}</span>
          <button type="button" className="link" onClick={() => onDismiss(item.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
