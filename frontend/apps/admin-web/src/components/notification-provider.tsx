import { createContext, useContext, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

type Toast = {
  id: string;
  title: string;
  description?: string;
  tone: 'info' | 'success' | 'error';
};

type NotificationContextValue = {
  notify: (toast: Omit<Toast, 'id'>) => void;
};

const NotificationContext = createContext<NotificationContextValue | null>(null);

export function NotificationProvider({ children }: PropsWithChildren) {
  const [items, setItems] = useState<Toast[]>([]);

  const value = useMemo<NotificationContextValue>(
    () => ({
      notify: (toast) => {
        const next = { id: crypto.randomUUID(), ...toast };
        setItems((current) => [...current, next]);
        window.setTimeout(() => {
          setItems((current) => current.filter((item) => item.id !== next.id));
        }, 4000);
      }
    }),
    []
  );

  return (
    <NotificationContext.Provider value={value}>
      {children}
      <aside className="toast-stack">
        {items.map((item) => (
          <div key={item.id} className={`toast toast--${item.tone}`}>
            <strong>{item.title}</strong>
            {item.description ? <p>{item.description}</p> : null}
          </div>
        ))}
      </aside>
    </NotificationContext.Provider>
  );
}

export function useNotifications() {
  const context = useContext(NotificationContext);
  if (!context) {
    throw new Error('NotificationProvider is missing');
  }
  return context;
}
