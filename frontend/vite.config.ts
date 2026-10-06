import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // библиотеки меняются реже кода приложения: отдельные чанки дольше живут в кеше браузера
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          mantine: [
            '@mantine/core',
            '@mantine/hooks',
            '@mantine/dates',
            '@mantine/modals',
            '@mantine/notifications',
          ],
          data: ['@tanstack/react-query', 'axios', 'react-hook-form', 'zod'],
        },
      },
    },
  },
  server: {
    port: 5173,
    // в dev-режиме API проксируется на backend из docker compose (через nginx на 8080)
    proxy: { '/api': 'http://localhost:8080' },
  },
});
