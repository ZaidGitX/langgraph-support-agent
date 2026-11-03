import { useCallback, useEffect, useRef, useState } from "react"

// The dashboard's one data source. Server-sent events announce every change (seq); the hook
// then refetches the view and the new timeline events. Polling stays as the fallback, so a
// proxy that drops the stream only costs latency, never correctness.

const POLL_FALLBACK_MS = 2000
const POLL_SAFETY_MS = 10000

export function useSession(initialSession = null) {
  const [session, setSession] = useState(initialSession)
  const [events, setEvents] = useState(() => (initialSession && initialSession.events) || [])
  const [error, setError] = useState("")
  const [live, setLive] = useState("connecting")
  const seqRef = useRef(0)
  const runRef = useRef(initialSession ? initialSession.run_id : null)
  const busyRef = useRef(false)

  const refresh = useCallback(async () => {
    if (busyRef.current) {
      return
    }
    busyRef.current = true
    try {
      const response = await fetch("/api/orchestrator/session")
      if (!response.ok) {
        return
      }
      const data = await response.json()
      if (data.run_id !== runRef.current) {
        runRef.current = data.run_id
        seqRef.current = 0
        setEvents([])
      }
      setSession(data)
      setError("")
      const feed = await fetch(`/api/orchestrator/session/events?since=${seqRef.current}&limit=500`)
      if (feed.ok) {
        const page = await feed.json()
        if (page.events.length) {
          seqRef.current = page.events[page.events.length - 1].seq
          setEvents((current) => [...current, ...page.events].slice(-500))
        }
      }
    } catch {
      setError("The orchestrator API is not reachable.")
    } finally {
      busyRef.current = false
    }
  }, [])

  useEffect(() => {
    let stop = false
    let source = null
    refresh()
    if (typeof window !== "undefined" && "EventSource" in window) {
      try {
        source = new EventSource("/api/orchestrator/session/stream")
        source.addEventListener("change", () => {
          if (!stop) {
            setLive("sse")
            refresh()
          }
        })
        source.onerror = () => {
          if (!stop) {
            setLive("polling")
          }
        }
      } catch {
        setLive("polling")
      }
    } else {
      setLive("polling")
    }
    const id = setInterval(() => {
      if (!stop) {
        refresh()
      }
    }, live === "sse" ? POLL_SAFETY_MS : POLL_FALLBACK_MS)
    return () => {
      stop = true
      clearInterval(id)
      if (source) {
        source.close()
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh, live === "sse"])

  return { session, setSession, events, error, setError, live, refresh }
}
