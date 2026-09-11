import type { UserRole } from '@cv-platform/schemas';

import type { AppIconName } from '../components/app-icon';

export type NavItem = {
  key: string;
  label: string;
  to: string;
  description: string;
  icon: AppIconName;
  allowedRoles: UserRole[];
};

const allOperatorRoles: UserRole[] = [
  'platform_admin',
  'ml_engineer',
  'operator',
  'label_manager',
  'viewer'
];

export const navItems: NavItem[] = [
  {
    key: 'pipelines',
    label: 'Projekte',
    to: '/pipelines',
    description: 'Label-Studio-Projekte, Datensätze, Modelle, Training und Deployment in einem Workflow.',
    icon: 'pipeline',
    allowedRoles: allOperatorRoles
  },
  {
    key: 'channels',
    label: 'Kanäle',
    to: '/channels',
    description: 'Manage cameras and source connections by place, source, health, and live operation.',
    icon: 'realtime',
    allowedRoles: ['platform_admin', 'ml_engineer', 'operator', 'viewer']
  },
  {
    key: 'edge-devices',
    label: 'Edge Devices',
    to: '/edge-devices',
    description: 'Register fixed-IP Edge devices and inspect connectivity and capabilities.',
    icon: 'environment',
    allowedRoles: ['platform_admin', 'ml_engineer', 'operator', 'viewer']
  },
  {
    key: 'runtime-pipelines',
    label: 'Runtime Pipelines',
    to: '/runtime-pipelines',
    description: 'Build live and triggerable graph versions with source, trigger, and response nodes.',
    icon: 'pipeline',
    allowedRoles: ['platform_admin', 'ml_engineer', 'operator', 'viewer']
  },
  {
    key: 'results',
    label: 'Ergebnisse',
    to: '/results',
    description: 'Browse recent and historical runtime results with final fields and calculated values.',
    icon: 'activity',
    allowedRoles: ['platform_admin', 'ml_engineer', 'operator', 'viewer']
  },
  {
    key: 'operations',
    label: 'System',
    to: '/operations',
    description: 'Snapshots, deployment control, audit, settings, and platform health.',
    icon: 'settings',
    allowedRoles: ['platform_admin', 'ml_engineer', 'operator', 'viewer']
  }
];

const legacyRouteMap: Array<[string, string]> = [
  ['/datasets/projects', '/operations/project-mapping'],
  ['/datasets/stats', '/operations/annotation-statistics'],
  ['/datasets/webhooks', '/operations/webhooks'],
  ['/datasets/snapshots', '/operations/snapshots'],
  ['/datasets', '/operations/snapshots'],
  ['/projects', '/pipelines'],
  ['/labeling', '/pipelines'],
  ['/snapshots', '/operations/snapshots'],
  ['/training', '/operations/training'],
  ['/deployments', '/operations/deployments'],
  ['/inference', '/operations/inference'],
  ['/batch', '/operations/batch'],
  ['/audit', '/operations/audit'],
  ['/settings', '/operations/settings'],
  ['/health', '/operations/health']
];

function canonicalPath(pathname: string) {
  const legacy = legacyRouteMap.find(([legacyPrefix]) => pathname.startsWith(legacyPrefix));
  return legacy ? legacy[1] : pathname;
}

export function findNavItem(pathname: string) {
  const currentPath = canonicalPath(pathname);
  return (
    [...navItems]
      .sort((left, right) => right.to.length - left.to.length)
      .find((item) => currentPath === item.to || currentPath.startsWith(`${item.to}/`)) ?? navItems[0]
  );
}

export function canAccessPath(pathname: string, role: UserRole | null | undefined) {
  void pathname;
  return Boolean(role);
}
