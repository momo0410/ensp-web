import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// base: './' 使构建产物可通过 FastAPI 直接托管在任意路径下
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
