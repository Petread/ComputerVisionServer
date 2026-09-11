import { useEffect, useRef, useState } from 'react';
import { api } from '../../app/api';

interface EdgeViewerLeaseOptions {
  maxFps: number;
  maxWidth: number;
  includeOverlay?: boolean;
}

export function useEdgeViewerLease(
  channelId: string,
  enabled: boolean,
  options: EdgeViewerLeaseOptions
) {
  const leaseIdRef = useRef<string | null>(null);
  const [active, setActive] = useState(false);
  const { maxFps, maxWidth, includeOverlay = true } = options;

  useEffect(() => {
    if (!enabled) {
      setActive(false);
      return;
    }
    let disposed = false;
    const renew = async () => {
      try {
        const lease = await api.channels.acquireEdgeViewerLease(channelId, {
          lease_id: leaseIdRef.current,
          ttl_seconds: 30,
          max_fps: maxFps,
          max_width: maxWidth,
          include_overlay: includeOverlay
        });
        if (!disposed) {
          leaseIdRef.current = lease.lease_id;
          setActive(lease.status === 'active');
        }
      } catch {
        if (!disposed) {
          setActive(false);
        }
      }
    };
    void renew();
    const timer = window.setInterval(() => void renew(), 10_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      const leaseId = leaseIdRef.current;
      leaseIdRef.current = null;
      setActive(false);
      if (leaseId) {
        void api.channels.releaseEdgeViewerLease(channelId, leaseId).catch(() => undefined);
      }
    };
  }, [channelId, enabled, includeOverlay, maxFps, maxWidth]);

  return active;
}
