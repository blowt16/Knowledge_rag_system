import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    // 开发期把 /api 代理到后端，避免 CORS —— 生产由同一域名提供
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', changeOrigin: true },
    },
  },
})
