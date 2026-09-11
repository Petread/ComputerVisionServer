import { Link } from '@tanstack/react-router';

export const operationCards = [
  {title: 'Systemzustand', body: 'Dienste, Warteschlangen und Ressourcen.', to: '/operations/health'},
  {title: 'Bereiche und Anlagen', body: 'Technische Plätze für die Kanalstruktur.', to: '/operations/places'},
  {title: 'Annotationsstatistik', body: 'Label-Studio-Datenqualität und Fortschritt.', to: '/operations/annotation-statistics'},
  {title: 'Webhooks', body: 'Eingänge und Verarbeitung aus Label Studio.', to: '/operations/webhooks'},
  {
    title: 'Access & Security',
    body: 'Users, passwords, API keys, and revocation.',
    to: '/operations/access'
  },
  {
    title: 'Manual Project Mapping',
    body: 'Manual Label Studio mapping and migration fallback.',
    to: '/operations/project-mapping'
  },
  {
    title: 'Snapshot Operations',
    body: 'Snapshot browsing, validation, and upload sessions.',
    to: '/operations/snapshots'
  },
  {
    title: 'Training Jobs',
    body: 'Job controls, checkpoints, logs, and comparisons.',
    to: '/operations/training'
  },
  {
    title: 'Model Registry',
    body: 'Registry approvals, lineage, and promotions.',
    to: '/operations/registry'
  },
  {
    title: 'Model Templates',
    body: 'Operator presets and training defaults.',
    to: '/operations/model-templates'
  },
  {
    title: 'Deployment Control',
    body: 'Package, stage, promote, rollback, and reports.',
    to: '/operations/deployments'
  },
  {
    title: 'Realtime Test Console',
    body: 'Release comparison and realtime diagnostics.',
    to: '/operations/inference'
  },
  {
    title: 'Batch Operations',
    body: 'Batch chunks, artifacts, bundles, and recovery.',
    to: '/operations/batch'
  },
  {
    title: 'Audit Trail',
    body: 'Actions, transitions, webhooks, and audit review.',
    to: '/operations/audit'
  },
  {
    title: 'Settings & Health',
    body: 'Integrations, health, queues, GPU, and config.',
    to: '/operations/settings'
  },
  {
    title: 'Workers & Updates',
    body: 'Worker liveness, offline update planning, and targeted restarts.',
    to: '/operations/workers-updates'
  }
];

export function OperationsPage() {
  return <div className="workspace-page"><header className="workspace-toolbar"><strong>System</strong></header><div className="workspace-tab-body"><table className="workspace-table"><thead><tr><th>Arbeitsbereich</th><th>Funktionen</th></tr></thead><tbody>{operationCards.map(card => <tr key={card.to}><td><Link to={card.to}>{card.title}</Link></td><td>{card.body}</td></tr>)}</tbody></table></div></div>;
}
export function SystemNavigation() {
  return <nav className="system-navigation" aria-label="Systembereiche">{operationCards.map(card => <Link key={card.to} to={card.to} activeProps={{className: 'is-active'}}>{card.title}</Link>)}</nav>;
}
