import { describe, expect, it } from 'vitest';
import { normalizeProjectId, sameProjectId } from '../projectId';

const LOWER = '0f8e2a1c-3b4d-4e5f-8a9b-0c1d2e3f4a5b';

describe('normalizeProjectId (GOO-778)', () => {
  it('lowercases a UUID in any case', () => {
    expect(normalizeProjectId(LOWER.toUpperCase())).toBe(LOWER);
    expect(normalizeProjectId('0F8e2A1c-3b4d-4e5f-8a9b-0c1d2e3f4a5b')).toBe(
      LOWER
    );
    expect(normalizeProjectId(LOWER)).toBe(LOWER);
  });

  it('leaves anything that is not a UUID unchanged', () => {
    expect(normalizeProjectId('Proj-A')).toBe('Proj-A');
    expect(normalizeProjectId(`${LOWER.toUpperCase()}X`)).toBe(
      `${LOWER.toUpperCase()}X`
    );
    expect(normalizeProjectId(`X${LOWER.toUpperCase()}`)).toBe(
      `X${LOWER.toUpperCase()}`
    );
  });
});

describe('sameProjectId', () => {
  it('matches the same UUID across case and rejects different ids', () => {
    expect(sameProjectId(LOWER.toUpperCase(), LOWER)).toBe(true);
    expect(sameProjectId('Proj-A', 'proj-a')).toBe(false);
    expect(sameProjectId(LOWER, '11111111-2222-4333-8444-555555555555')).toBe(
      false
    );
  });
});
