/**
 * Object-shaped cell values reach the grid and the CSV export as JSON text,
 * and a formula-shaped value is neutralised in the CSV (GOO-779).
 *
 * Mutation check (docs/engineering/testing.md): in ExtractionMatrix.tsx,
 * swap `extractionCellText(cell?.value)` for `String(cell?.value ?? '')` in
 * `handleExportCsv` and the CSV test fails with `[object Object]`; do the
 * same in the grid cell and the grid test fails.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/test-utils';
import { ExtractionMatrix } from '../ExtractionMatrix';
import { getMatrix, listMatrices } from '@/services/scispaceService';
import type { ExtractionMatrix as ExtractionMatrixType } from '@/types/scispace';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn().mockResolvedValue([]),
}));
vi.mock('@/services/scispaceService', () => ({
  createMatrix: vi.fn(),
  deleteMatrix: vi.fn(),
  getExtractionTaskStatus: vi.fn(),
  getMatrix: vi.fn(),
  listMatrices: vi.fn(),
  triggerExtraction: vi.fn(),
  updateMatrix: vi.fn(),
  getCellObservations: vi.fn(),
  listFormVersions: vi.fn(),
}));

const OBJECT_VALUE = { arms: 2, labels: ['drug', 'placebo'] };
const FORMULA_VALUE = '=HYPERLINK("http://x")';

const matrix: ExtractionMatrixType = {
  id: 'm-1',
  project_id: 'p-1',
  name: 'Outcomes',
  columns: [{ name: 'Design' }, { name: 'Note' }],
  cells: [
    {
      document_id: 'doc-1',
      column_name: 'Design',
      value: OBJECT_VALUE,
      citation_snippet: null,
      confidence: null,
    },
    {
      document_id: 'doc-1',
      column_name: 'Note',
      value: FORMULA_VALUE,
      citation_snippet: null,
      confidence: null,
    },
  ],
  created_at: null,
  updated_at: null,
};

async function renderLoaded(): Promise<ReturnType<typeof render>> {
  vi.mocked(listMatrices).mockResolvedValue({
    matrices: [{ id: 'm-1' }],
  } as unknown as Awaited<ReturnType<typeof listMatrices>>);
  vi.mocked(getMatrix).mockResolvedValue(matrix);
  const view = render(
    <ExtractionMatrix
      projectId="p-1"
      documents={[{ id: 'doc-1', title: 'Paper A' }]}
    />
  );
  await screen.findByRole('button', { name: /Export CSV/ });
  return view;
}

// jsdom's Blob has no text(); FileReader reads it.
function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

const { createObjectURL, revokeObjectURL } = URL;
afterEach(() => {
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  vi.restoreAllMocks();
});

describe('ExtractionMatrix cell text', () => {
  it('shows an object cell as JSON in the grid', async () => {
    await renderLoaded();
    const row = screen.getByRole('row', { name: /Paper A/ });
    expect(
      within(row).getByText(JSON.stringify(OBJECT_VALUE))
    ).toBeInTheDocument();
    expect(within(row).getByText(FORMULA_VALUE)).toBeInTheDocument();
    expect(row).not.toHaveTextContent('[object Object]');
  });

  it('exports an object cell as JSON and neutralises a formula', async () => {
    const blobs: Blob[] = [];
    // jsdom has no object URLs; capture the blob instead.
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return 'blob:test';
    });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(
      () => undefined
    );
    const { user } = await renderLoaded();

    await user.click(screen.getByRole('button', { name: /Export CSV/ }));

    expect(blobs).toHaveLength(1);
    const csv = await readBlob(blobs[0]);
    const [header, row] = csv.split('\n');
    expect(header).toBe('Document,Design,Note');
    const json = JSON.stringify(OBJECT_VALUE).replace(/"/g, '""');
    expect(row).toBe(`Paper A,"${json}","'${FORMULA_VALUE.replace(/"/g, '""')}"`);
    expect(csv).not.toContain('[object Object]');
  });
});
