import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';

declare const process: {
  env: Record<string, string | undefined>;
};

const usePolling = process.env.VITE_USE_POLLING === 'true';
const hmrHost = process.env.VITE_HMR_HOST;
const hmrClientPort = process.env.VITE_HMR_CLIENT_PORT
  ? Number(process.env.VITE_HMR_CLIENT_PORT)
  : undefined;
const basePath = process.env.VITE_FRONTEND_BASE_PATH || '/';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  base: mode === 'demo' ? '/' : basePath,
  build: { manifest: true },
  server: {
    port: 4173,
    watch: usePolling
      ? {
          usePolling: true,
          interval: 250
        }
      : undefined,
    hmr: hmrHost
      ? {
          host: hmrHost,
          clientPort: hmrClientPort,
          protocol: 'ws'
        }
      : undefined
  }
}));
