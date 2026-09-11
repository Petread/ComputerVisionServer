import { useBlocker } from '@tanstack/react-router';

export function useUnsavedWorkspace(dirty: boolean) {
  useBlocker({
    disabled: !dirty,
    enableBeforeUnload: dirty,
    shouldBlockFn: ({ current, next }) => current.pathname !== next.pathname &&
      !window.confirm('Es gibt ungespeicherte Änderungen. Arbeitsbereich trotzdem verlassen?')
  });
}
