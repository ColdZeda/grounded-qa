import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// API 目标。
// 默认指向 `docker compose` 发布到宿主机的端口（58000，**不是 8000** ——
// 本机还有项目 #1，端口要错开，见 .env.example 的「本机端口规划」）。
// 若你用 `uv run uvicorn app.main:app --reload`（宿主机 8000）调试：
//     $env:VITE_API_TARGET = "http://127.0.0.1:8000"; pnpm dev
const apiTarget = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:58000'

export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // 本地开发直连后端。服务器上这一步由 nginx 反代完成，不走这个 proxy。
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      '/healthz': { target: apiTarget, changeOrigin: true },
      '/metrics': { target: apiTarget, changeOrigin: true },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: false,
    // 2C2G 上不做 sourcemap；chunk 警告阈值调高一点，避免噪音
    chunkSizeWarningLimit: 800,
  },
})
