import type { ProtocolSnapshot } from '@/services/researchProtocolService';

export const DAILY_BRIEF_TEMPLATE_SOURCE = 'daily_research_brief';

/** Fixed record name: protocol names are unique per project. */
export const DAILY_BRIEF_PROTOCOL_NAME = 'Daily Research Brief protocol';

export const DAILY_BRIEF_AMENDMENT_REASON =
  'Daily Research Brief setup changed and was saved again.';

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

/**
 * Map confirmed Daily Research Brief parameters to the protocol snapshot the
 * supervisor approves. Every section is a non-empty object, as the backend
 * `ProtocolSnapshot` contract requires.
 */
export function dailyBriefProtocolSnapshot(
  parameters: Record<string, unknown>
): ProtocolSnapshot {
  const notes =
    typeof parameters.notes === 'string' ? parameters.notes.trim() : '';
  return {
    eligibility: {
      inclusion_criteria: stringList(parameters.inclusion_criteria),
      exclusion_criteria: stringList(parameters.exclusion_criteria),
    },
    sources_search: {
      providers: stringList(parameters.providers),
      limit_per_provider: parameters.limit_per_provider,
    },
    selection: { method: DAILY_BRIEF_TEMPLATE_SOURCE, exhaustive: false },
    extraction: { method: DAILY_BRIEF_TEMPLATE_SOURCE },
    appraisal_synthesis: { method: DAILY_BRIEF_TEMPLATE_SOURCE },
    outcomes: notes ? { notes } : { method: DAILY_BRIEF_TEMPLATE_SOURCE },
    reviewer_mode: { mode: 'single' },
  };
}
