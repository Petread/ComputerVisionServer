import React from 'react';
import ReactDOM from 'react-dom/client';
import { RouterProvider } from '@tanstack/react-router';
import { AppProviders } from './providers';
import { router } from './router';
import '../styles/global.css';
import '../styles/workspace.css';
import '../styles/production-workspace.css';

document.documentElement.dataset.workspace = 'cv-production-workspace-v1';
document.documentElement.lang = 'de';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppProviders><RouterProvider router={router} /></AppProviders>
  </React.StrictMode>
);
