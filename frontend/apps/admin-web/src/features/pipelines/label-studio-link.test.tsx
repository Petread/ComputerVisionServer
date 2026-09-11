import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { LabelStudioProjectLink, labelStudioProjectUrl } from './label-studio-link';

describe('Label Studio project navigation', () => {
  it('links to the actual Label Studio data manager through the existing proxy', () => {
    expect(labelStudioProjectUrl(42)).toBe('/label-studio/projects/42/data/');
    expect(renderToStaticMarkup(<LabelStudioProjectLink projectId={42}/>)).toContain('href="/label-studio/projects/42/data/"');
  });
  it.each([null, undefined, 0, -1, 1.5, NaN, Infinity])('never falls back to the listing for invalid ID %s', id => {
    expect(labelStudioProjectUrl(id)).toBeUndefined();
    expect(renderToStaticMarkup(<LabelStudioProjectLink projectId={id}/>)).not.toContain('href=');
  });
});
