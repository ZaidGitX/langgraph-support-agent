import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
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
