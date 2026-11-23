/**
 * GOO-756: EntityFilters and RelationshipForm show only the graph's own type
 * lists. While a list loads or after it fails they say so and disable the
 * controls that need it; there is no built-in list to fall back to.
 */
import { beforeAll, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen, within } from '@/test/test-utils';
import { EntityFilters } from '@/components/entities/EntityFilters';
import { RelationshipForm } from '@/components/entities/RelationshipForm';
import type { TypeList } from '@/components/entities/typeList';

vi.mock('@/services/entityService', () => ({
  entityService: { searchEntities: vi.fn().mockResolvedValue([]) },
}));

beforeAll(() => {
  // Radix menus need these in jsdom.
  Element.prototype.hasPointerCapture ??= (): boolean => false;
  Element.prototype.releasePointerCapture ??= (): void => {};
  Element.prototype.scrollIntoView ??= (): void => {};
  globalThis.ResizeObserver ??= class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  } as unknown as typeof ResizeObserver;
});

const LOADING: TypeList = { status: 'loading' };
const FAILED: TypeList = { status: 'failed' };
const loaded = (...types: string[]): TypeList => ({ status: 'loaded', types });

const renderFilters = (
  types: TypeList,
  onTypesChange = vi.fn()
): ReturnType<typeof render> =>
  render(
    <EntityFilters
      searchQuery=""
      onSearchChange={vi.fn()}
      selectedTypes={[]}
      onTypesChange={onTypesChange}
      confidenceRange={[0, 100]}
      onConfidenceChange={vi.fn()}
      sortField="created_at"
      sortOrder="desc"
      onSortChange={vi.fn()}
      onClearFilters={vi.fn()}
      totalCount={0}
      filteredCount={0}
      types={types}
    />
  );

const typeTrigger = (name: RegExp): HTMLElement => screen.getByRole('button', { name });

describe('EntityFilters type filter', () => {
  it('is disabled and says so while the types load', () => {
    renderFilters(LOADING);

    expect(typeTrigger(/loading types/i)).toBeDisabled();
  });

  it('is disabled and says types are unavailable after a failure', () => {
    renderFilters(FAILED);

    expect(typeTrigger(/types unavailable/i)).toBeDisabled();
  });

  it('lists only the loaded types, and Select all picks exactly those', async () => {
    const user = userEvent.setup();
    const onTypesChange = vi.fn();
    renderFilters(loaded('GENE', 'PROTEIN'), onTypesChange);

    const trigger = typeTrigger(/all types/i);
    expect(trigger).toBeEnabled();
    await user.click(trigger);

    const menu = await screen.findByRole('menu');
    const labels = within(menu)
      .getAllByRole('menuitemcheckbox')
      .map((item) => item.textContent?.trim());
    expect(labels).toEqual(['Select all', 'Unknown type', 'Gene', 'Protein']);

    await user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Select all' }));
    expect(onTypesChange).toHaveBeenCalledWith(['GENE', 'PROTEIN']);
  });
});

const renderForm = (types: TypeList): ReturnType<typeof render> =>
  render(
    <RelationshipForm sourceEntityId="s1" onSubmit={vi.fn()} onCancel={vi.fn()} types={types} />
  );

const submit = (): HTMLElement => screen.getByRole('button', { name: /establish_link/i });

describe('RelationshipForm type picker', () => {
  it('is disabled while the types load, and so is submit', () => {
    renderForm(LOADING);

    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByRole('combobox')).toHaveTextContent('Loading relationship types…');
    expect(submit()).toBeDisabled();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says the types are unavailable after a failure and blocks submit', () => {
    renderForm(FAILED);

    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByRole('combobox')).toHaveTextContent('Relationship types unavailable');
    expect(screen.getByRole('alert')).toHaveTextContent("Relationship types couldn't be loaded");
    expect(submit()).toBeDisabled();
  });

  it('offers only the loaded types', async () => {
    const user = userEvent.setup();
    renderForm(loaded('ENCODES', 'BINDS_TO'));

    expect(submit()).toBeEnabled();
    const picker = screen.getByRole('combobox');
    expect(picker).toBeEnabled();
    await user.click(picker);

    const options = (await screen.findAllByRole('option')).map((o) => o.textContent);
    expect(options).toEqual(['ENCODES', 'BINDS TO']);
  });
});
