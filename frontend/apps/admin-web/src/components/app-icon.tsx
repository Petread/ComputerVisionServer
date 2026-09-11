import type { ReactNode, SVGProps } from 'react';

export type AppIconName =
  | 'activity'
  | 'alert'
  | 'batch'
  | 'check'
  | 'chevron'
  | 'close'
  | 'dashboard'
  | 'endpoint'
  | 'environment'
  | 'help'
  | 'home'
  | 'model'
  | 'pipeline'
  | 'realtime'
  | 'settings'
  | 'sparkle'
  | 'user';

const iconPaths: Record<AppIconName, ReactNode> = {
  activity: (
    <>
      <path d="M3 12h3l2.2-5.5 4 11 2.6-5.5H21" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3.6 21 19H3l9-15.4Z" />
      <path d="M12 8v5" />
      <path d="M12 16.5h.01" />
    </>
  ),
  batch: (
    <>
      <rect x="4" y="5" width="16" height="5" rx="2" />
      <rect x="4" y="14" width="16" height="5" rx="2" />
      <path d="M8 7.5h.01M8 16.5h.01" />
    </>
  ),
  check: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m8.5 12.3 2.2 2.2 4.8-5" />
    </>
  ),
  chevron: (
    <>
      <path d="m9 6 6 6-6 6" />
    </>
  ),
  close: (
    <>
      <path d="M6 6l12 12" />
      <path d="M18 6 6 18" />
    </>
  ),
  dashboard: (
    <>
      <rect x="4" y="4" width="6" height="6" rx="1.5" />
      <rect x="14" y="4" width="6" height="6" rx="1.5" />
      <rect x="4" y="14" width="6" height="6" rx="1.5" />
      <rect x="14" y="14" width="6" height="6" rx="1.5" />
    </>
  ),
  endpoint: (
    <>
      <path d="M6 8a6 6 0 0 1 12 0" />
      <path d="M6 16a6 6 0 0 0 12 0" />
      <path d="M4 12h16" />
      <path d="M12 4v16" />
    </>
  ),
  environment: (
    <>
      <path d="M12 3v18" />
      <path d="M6.2 6.2c3.8-.5 7.6-.5 11.6 0" />
      <path d="M6.2 17.8c3.8.5 7.6.5 11.6 0" />
      <circle cx="12" cy="12" r="8.5" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.8 9.4a2.4 2.4 0 0 1 4.5 1.2c0 1.7-2.3 2-2.3 3.5" />
      <path d="M12 17h.01" />
    </>
  ),
  home: (
    <>
      <path d="m4 11 8-7 8 7" />
      <path d="M6.5 10.5V20h11v-9.5" />
      <path d="M10 20v-5h4v5" />
    </>
  ),
  model: (
    <>
      <path d="M12 3.5 19 7.5v8.8l-7 4.2-7-4.2V7.5l7-4Z" />
      <path d="M5.5 8 12 12l6.5-4" />
      <path d="M12 12v8" />
    </>
  ),
  pipeline: (
    <>
      <circle cx="6" cy="7" r="2.5" />
      <circle cx="18" cy="17" r="2.5" />
      <path d="M8.5 7h4.2A3.3 3.3 0 0 1 16 10.3V14" />
      <path d="M12 4.8 14.2 7 12 9.2" />
    </>
  ),
  realtime: (
    <>
      <path d="M5 12a7 7 0 0 1 14 0" />
      <path d="M8 12a4 4 0 0 1 8 0" />
      <circle cx="12" cy="13" r="1.5" />
      <path d="M12 14.5v5" />
    </>
  ),
  settings: (
    <>
      <path d="M12 8.5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Z" />
      <path d="M19 12a7 7 0 0 0-.1-1.1l2-1.5-2-3.4-2.4 1a7.4 7.4 0 0 0-1.9-1.1L14.3 3h-4.6l-.4 2.9A7.4 7.4 0 0 0 7.5 7L5 6 3 9.4l2 1.5a7 7 0 0 0 0 2.2l-2 1.5L5 18l2.5-1a7.4 7.4 0 0 0 1.8 1.1l.4 2.9h4.6l.4-2.9a7.4 7.4 0 0 0 1.8-1.1l2.5 1 2-3.4-2-1.5c.1-.4.1-.7.1-1.1Z" />
    </>
  ),
  sparkle: (
    <>
      <path d="M12 3.5 13.9 9l5.6 2-5.6 2L12 18.5 10.1 13l-5.6-2 5.6-2L12 3.5Z" />
      <path d="M19 4v4M17 6h4" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M4.8 19.5a7.2 7.2 0 0 1 14.4 0" />
    </>
  )
};

export function AppIcon({ name, className, ...props }: SVGProps<SVGSVGElement> & { name: AppIconName }) {
  return (
    <svg
      aria-hidden="true"
      className={['app-icon', className].filter(Boolean).join(' ')}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      {...props}
    >
      {iconPaths[name]}
    </svg>
  );
}
