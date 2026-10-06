import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // в dev-режиме API проксируется на backend из docker compose (через nginx на 8080)
    proxy: { '/api': 'http://localhost:8080' },
  },
});
