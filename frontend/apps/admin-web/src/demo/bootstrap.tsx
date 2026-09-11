import React from 'react';
import ReactDOM from 'react-dom/client';
import { DemoApp } from './demo-app';
import './workspace.css';

document.title = 'CV Ybbs · Lokale Vorschau';
document.documentElement.lang = 'de';
// Only self-hosted assets and Vite HMR are permitted, including media and frames.
const policy = document.createElement('meta');
policy.httpEquiv = 'Content-Security-Policy';
policy.content = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob: data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'none'";
document.head.append(policy);
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><DemoApp /></React.StrictMode>);
