import { PlatformApiError } from '@cv-platform/api-client';

type ErrorDescriptor = {
  title: string;
  description?: string;
};

export function describeError(error: unknown, fallbackTitle: string): ErrorDescriptor {
  if (error instanceof PlatformApiError) {
    const detailParts = [
      error.payload.detail,
      error.payload.correlation_id ? `Correlation ID: ${error.payload.correlation_id}` : undefined
    ].filter(Boolean);
    return {
      title: error.payload.message || fallbackTitle,
      description: detailParts.join(' ')
    };
  }

  if (error instanceof Error) {
    return {
      title: error.message || fallbackTitle
    };
  }

  return { title: fallbackTitle };
}

export function confirmConsequentialAction(input: {
  title: string;
  consequence: string;
  secondary?: string;
}) {
  return window.confirm(
    [input.title, '', input.consequence, input.secondary].filter(Boolean).join('\n')
  );
}
