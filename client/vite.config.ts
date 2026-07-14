import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const OUT_DIR = process.env.VITE_OUT_DIR
  ? path.resolve(process.env.VITE_OUT_DIR)
  : path.resolve(__dirname, '../server/app/static')

// Ports come from scripts/dev.sh's free-port walk (falls back to the
// classic defaults when run directly). strictPort keeps port ownership in
// dev.sh: if the chosen port is somehow taken, fail loudly instead of Vite
// silently hopping to a port nothing else knows about.
const VITE_PORT = Number(process.env.VITE_PORT ?? 5173)
const API_PORT = Number(process.env.API_PORT ?? 8000)

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
  },
  server: {
    port: VITE_PORT,
    strictPort: true,
    proxy: {
      '/auth': `http://localhost:${API_PORT}`,
      '/api': `http://localhost:${API_PORT}`,
    },
  },
})
