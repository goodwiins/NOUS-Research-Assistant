/**
 * A type list loaded from the knowledge graph (GOO-756). There is no
 * built-in list to fall back to: while it loads or after it fails, the
 * controls that need it say so and stay disabled.
 */
export type TypeList =
  | { status: 'loading' }
  | { status: 'failed' }
  | { status: 'loaded'; types: string[] };

export const typeListFrom = (
  result: PromiseSettledResult<string[]>
): TypeList =>
  result.status === 'fulfilled'
    ? { status: 'loaded', types: result.value }
    : { status: 'failed' };
