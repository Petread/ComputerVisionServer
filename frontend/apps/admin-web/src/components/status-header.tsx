import { useLocation } from '@tanstack/react-router';
import { shouldHideStatusHeader } from './status-header-visibility';

export function StatusHeader() {
  const location = useLocation();
  if (shouldHideStatusHeader(location.pathname)) {
    return null;
  }

  return (
    <header className="status-header">
      <section className="status-header__hero" aria-label="Inferenz Server">
        <div className="status-header__hero-inner">
          <p>Standort Ybbs</p>
          <h1>Inferenz Server</h1>
        </div>
      </section>
      
    </header>
  );
}
