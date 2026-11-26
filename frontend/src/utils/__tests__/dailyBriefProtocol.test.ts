import { describe, expect, it } from 'vitest';

import { dailyBriefProtocolSnapshot } from '@/utils/dailyBriefProtocol';

const SCOPE = {
  contract_version: 1,
  research_question: 'What changed?',
  inclusion_criteria: ['Peer reviewed'],
  exclusion_criteria: ['Editorials'],
  providers: ['openalex', 'crossref'],
  limit_per_provider: 25,
  notes: 'Focus on benchmarks',
};

describe('dailyBriefProtocolSnapshot', () => {
  it('maps the brief scope to all seven protocol sections', () => {
    const snapshot = dailyBriefProtocolSnapshot(SCOPE);

    expect(snapshot).toEqual({
      eligibility: {
        inclusion_criteria: ['Peer reviewed'],
        exclusion_criteria: ['Editorials'],
      },
      sources_search: {
        providers: ['openalex', 'crossref'],
        limit_per_provider: 25,
      },
      selection: { method: 'daily_research_brief', exhaustive: false },
      extraction: { method: 'daily_research_brief' },
      appraisal_synthesis: { method: 'daily_research_brief' },
      outcomes: { notes: 'Focus on benchmarks' },
      reviewer_mode: { mode: 'single' },
    });
    for (const section of Object.values(snapshot)) {
      expect(typeof section).toBe('object');
      expect(Array.isArray(section)).toBe(false);
      expect(Object.keys(section).length).toBeGreaterThan(0);
    }
  });

  it('uses the method marker for outcomes when the notes are empty', () => {
    const snapshot = dailyBriefProtocolSnapshot({ ...SCOPE, notes: '  ' });

    expect(snapshot.outcomes).toEqual({ method: 'daily_research_brief' });
    expect(Object.keys(snapshot)).toHaveLength(7);
    for (const section of Object.values(snapshot)) {
      expect(Object.keys(section).length).toBeGreaterThan(0);
    }
  });
});
