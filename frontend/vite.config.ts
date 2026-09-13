import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { configDefaults, defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'
  return {
    plugins: [vue()],
    server: {
      allowedHosts: ['frontend', 'localhost', '127.0.0.1'],
      proxy: {
        '/api': proxyTarget,
        '/health': proxyTarget,
      },
    },
    test: {
      environment: 'jsdom',
      exclude: [...configDefaults.exclude, 'e2e/**'],
    },
  }
})
