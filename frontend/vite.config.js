import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

const rootDir = path.dirname(fileURLToPath(import.meta.url))

function serveLocalReport() {
  return {
    name: "serve-local-orchestrator-report",
    configureServer(server) {
      server.middlewares.use("/local-report.json", (req, res) => {
        const file = path.resolve(rootDir, "../var/report.json")
        if (!fs.existsSync(file)) {
          res.statusCode = 404
          res.setHeader("Content-Type", "application/json")
          res.end(JSON.stringify({ error: "var/report.json was not found" }))
          return
        }
        res.setHeader("Content-Type", "application/json")
        fs.createReadStream(file).pipe(res)
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), serveLocalReport()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ask": "http://127.0.0.1:8000",
      "/status": "http://127.0.0.1:8000",
      "/orders": "http://127.0.0.1:8000",
      "/products": "http://127.0.0.1:8000",
      "/knowledge-base": "http://127.0.0.1:8000",
      "/docs": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
    },
  },
})
