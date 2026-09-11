import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from 'react';

type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info';

export function PageHero(props: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
  metrics?: ReactNode;
}) {
  return (
    <section className="page-hero">
      <div>
        <p className="training-hero__eyebrow">{props.eyebrow}</p>
        <h2>{props.title}</h2>
        {props.children ? <p className="subtle-text">{props.children}</p> : null}
      </div>
      {props.metrics ? <div className="training-summary-pills">{props.metrics}</div> : null}
    </section>
  );
}

export function ActionToolbar(props: PropsWithChildren) {
  return <div className="action-toolbar">{props.children}</div>;
}

export function AdvancedSection(props: PropsWithChildren<{ title?: string; open?: boolean }>) {
  return (
    <details className="advanced-section" open={props.open}>
      <summary>{props.title ?? 'Advanced details'}</summary>
      <div className="panel-stack">{props.children}</div>
    </details>
  );
}

export function DetailsCard(props: PropsWithChildren<{ title: string; aside?: ReactNode }>) {
  return (
    <article className="details-card">
      <div className="status-card__header">
        <h3>{props.title}</h3>
        {props.aside}
      </div>
      {props.children}
    </article>
  );
}

export function StatusBadge(props: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-badge badge--${props.tone ?? 'neutral'}`}>{props.children}</span>;
}

export function PrimaryActionButton(props: PropsWithChildren<ButtonHTMLAttributes<HTMLButtonElement>>) {
  const { className, ...buttonProps } = props;
  return <button {...buttonProps} className={['button--primary', className].filter(Boolean).join(' ')} />;
}

export function DangerActionButton(props: PropsWithChildren<ButtonHTMLAttributes<HTMLButtonElement>>) {
  const { className, ...buttonProps } = props;
  return <button {...buttonProps} className={['button--danger', className].filter(Boolean).join(' ')} />;
}

export function EmptyStateCard(props: PropsWithChildren<{ title: string; body: string }>) {
  return (
    <div className="empty-state-card empty-state">
      <h3>{props.title}</h3>
      <p>{props.body}</p>
      {props.children}
    </div>
  );
}
