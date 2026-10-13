import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

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
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/metrics': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: false,
    // 2C2G 上不做 sourcemap；chunk 警告阈值调高一点，避免噪音
    chunkSizeWarningLimit: 800,
  },
})
