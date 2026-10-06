import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // `@/` → src（管理端组件按 shadcn 惯例用这个前缀；生产构建也要认）
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5273,
    // 开发期把 /api 代理到后端，避免 CORS —— 生产由同一域名提供
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', changeOrigin: true },
    },
  },
})
