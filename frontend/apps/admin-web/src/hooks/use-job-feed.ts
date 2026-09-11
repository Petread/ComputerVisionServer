import { useEffect, useState } from 'react';
import type { SystemStatusResponse } from '@cv-platform/schemas';

function resolveWebSocketBaseUrl() {
  const configuredWsUrl = import.meta.env.VITE_ADMIN_WS_URL;
  if (configuredWsUrl) {
    return configuredWsUrl.replace(/^http/, 'ws');
  }

  const configuredApiUrl = import.meta.env.VITE_ADMIN_API_URL;
  if (configuredApiUrl) {
    return configuredApiUrl.replace(/^http/, 'ws');
  }

  if (configuredWsUrl === '' || configuredApiUrl === '') {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}`;
  }

  if (import.meta.env.DEV) {
    return 'ws://localhost:8080';
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}`;
}

export function useJobFeed(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<SystemStatusResponse | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const baseUrl = resolveWebSocketBaseUrl();
    const socket = new WebSocket(`${baseUrl}/api/v1/jobs/events/ws`);

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onerror = () => setConnected(false);
    socket.onmessage = (event) => {
      setSnapshot(JSON.parse(event.data) as SystemStatusResponse);
    };

    return () => {
      socket.close();
    };
  }, [enabled]);

  return { connected, snapshot };
}
