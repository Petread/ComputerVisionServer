export function labelStudioProjectUrl(projectId: number | null | undefined): string | undefined {
  return Number.isSafeInteger(projectId) && (projectId ?? 0) > 0
    ? `/label-studio/projects/${projectId}/data/`
    : undefined;
}

export function LabelStudioProjectLink({ projectId }: { projectId: number | null | undefined }) {
  const href = labelStudioProjectUrl(projectId);
  return href ? <a className="button-link" href={href} target="_blank" rel="noopener noreferrer">Label Studio ↗</a>
    : <span className="subtle-text">Kein Label-Studio-Projekt zugeordnet</span>;
}
