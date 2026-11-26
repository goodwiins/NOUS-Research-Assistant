/**
 * How an analytics card tells "never measured" apart from "failed to load"
 * (GOO-768). Both show "—", never a made-up 0, but only a real load error
 * reads like an outage. For example, the backend sends a `null` average
 * search time when no search in the window was timed (GOO-745).
 */
export const NOT_MEASURED = 'not-measured' as const;
export type NotMeasured = typeof NOT_MEASURED;

/** Note under a value that the backend reported as not measured. */
export const NOT_MEASURED_NOTE = 'Not measured yet';
/** Note under a value whose source failed to load. */
export const UNAVAILABLE_NOTE = 'Unavailable right now';
