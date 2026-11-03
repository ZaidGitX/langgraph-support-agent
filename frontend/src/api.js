// One fetch wrapper for the orchestrator API. It adds the dashboard token (when the server
// requires one) to every mutating call and lets the shell ask for the token on a 401.

const TOKEN_KEY = "orchestrator-ui-token"

export function readToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || ""
  } catch {
    return ""
  }
}

export function saveToken(value) {
  try {
    if (value) {
      window.sessionStorage.setItem(TOKEN_KEY, value)
    } else {
      window.sessionStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    // Storage can be unavailable; the token then lives for this page load only.
  }
  memoryToken = value || ""
}

let memoryToken = ""

export async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) }
  const method = (options.method || "GET").toUpperCase()
  if (options.body !== undefined && !headers["content-type"]) {
    headers["content-type"] = "application/json"
  }
  if (method !== "GET") {
    const token = readToken() || memoryToken
    if (token) {
      headers["X-Orchestrator-Token"] = token
    }
  }
  const response = await fetch(path, { ...options, headers })
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("orchestrator:token-required"))
  }
  return response
}

export async function errorDetail(response) {
  try {
    const body = await response.json()
    if (typeof body.detail === "string") {
      return body.detail
    }
    if (Array.isArray(body.detail) && body.detail.length) {
      return body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
    }
  } catch {
    // The body was not JSON.
  }
  return `The orchestrator returned ${response.status}.`
}
