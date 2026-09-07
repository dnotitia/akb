import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  cacheDir: process.env.VITE_CACHE_DIR ?? '.vite',
  server: {
    host: '127.0.0.1',
  },
})
