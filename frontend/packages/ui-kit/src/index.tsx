import React, { type PropsWithChildren, type ReactNode } from 'react';

export function Panel(props: PropsWithChildren<{ title: string; action?: ReactNode }>) {
  return (
    <section className="panel">
      <div className="panel__header">
        <div>
          <h2>{props.title}</h2>
        </div>
        {props.action ? <div>{props.action}</div> : null}
      </div>
      <div className="panel__body">{props.children}</div>
    </section>
  );
}

export function MetricPill(props: {
  label: string;
  value: string;
  tone?: 'neutral' | 'success' | 'warn' | 'danger' | 'info';
}) {
  return (
    <div className={`metric-pill metric-pill--${props.tone ?? 'neutral'}`}>
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

export function EmptyState(props: PropsWithChildren<{ title: string; body: string }>) {
  return (
    <div className="empty-state">
      <h3>{props.title}</h3>
      <p>{props.body}</p>
      {props.children}
    </div>
  );
}
