/**
 * GOO-778: the pipeline's error text is one of these fixed, safe messages.
 * Raw error detail is logged, never shown.
 */
export const PIPELINE_ERRORS = {
  load: "Couldn't load the research pipeline.",
  reset: "Couldn't reset the research pipeline.",
  advance: "Couldn't move to the next step. Try again.",
  skip: "Couldn't skip this step. Try again.",
  goTo: "Couldn't open that step. Try again.",
  updateData: "Couldn't save this step. Try again.",
  refused:
    "That change wasn't applied: the pipeline isn't loaded for this project yet.",
  generic: 'Something went wrong with the research pipeline.',
} as const;

export type PipelineErrorMessage =
  (typeof PIPELINE_ERRORS)[keyof typeof PIPELINE_ERRORS];

const KNOWN_MESSAGES: ReadonlySet<string> = new Set(
  Object.values(PIPELINE_ERRORS)
);

function isPipelineErrorMessage(error: string): error is PipelineErrorMessage {
  return KNOWN_MESSAGES.has(error);
}

/** The text to show for a stored error: a known message, else the generic one. */
export function pipelineErrorText(error: string): PipelineErrorMessage {
  return isPipelineErrorMessage(error) ? error : PIPELINE_ERRORS.generic;
}
