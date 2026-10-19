import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'KEYSTONE_')
  return {
    plugins: [vue()],
    base: '/admin/',
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': { target: env.KEYSTONE_API_PROXY ?? 'http://127.0.0.1:8000' },
      },
    },
  }
})
