import { createContext, useContext } from 'react';
import type { Channel, DemoData, Graph } from './model';

export type Route = { module: string; id: string; tab: string; params: URLSearchParams };
export type DemoContextValue = {
  data: DemoData;
  update: (updater: (data: DemoData) => DemoData, message?: string) => void;
  route: Route;
  navigate: (path: string) => void;
  notify: (message: string) => void;
  graphDrafts: Record<string, Graph>;
  setGraphDraft: (id: string, graph: Graph | null) => void;
  channelDrafts: Record<string, Channel>;
  setChannelDraft: (id: string, channel: Channel | null) => void;
};
export const DemoContext = createContext<DemoContextValue | null>(null);
export function useDemo() { const value = useContext(DemoContext); if (!value) throw new Error('Demo workspace context missing'); return value; }
export function parseRoute(hash: string): Route {
  const [path, query] = hash.replace(/^#/, '').split('?');
  const parts = path.split('/').filter(Boolean);
  const params = new URLSearchParams(query);
  return { module: parts[0] || 'channels', id: parts[1] || '', tab: params.get('tab') || '', params };
}
