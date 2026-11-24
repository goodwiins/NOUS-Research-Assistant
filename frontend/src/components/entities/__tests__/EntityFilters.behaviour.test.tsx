/**
 * EntityFilters: what each control asks the page to change. This covers the
 * search box, the type menu and its search, the active-filter chips, the
 * confidence presets, the sort picker and Clear all.
 */
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import userEvent from '@testing-library/user-event';
import { fireEvent, render, screen, within } from '@/test/test-utils';
import { EntityFilters } from '@/components/entities/EntityFilters';
import type { EntityType } from '@/types/entity';

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

type Props = ComponentProps<typeof EntityFilters>;

const setup = (overrides: Partial<Props> = {}): Props & { user: ReturnType<typeof userEvent.setup> } => {
  const props: Props = {
    searchQuery: '',
    onSearchChange: vi.fn(),
    selectedTypes: [],
    onTypesChange: vi.fn(),
    confidenceRange: [0, 100],
    onConfidenceChange: vi.fn(),
    sortField: 'created_at',
    sortOrder: 'desc',
    onSortChange: vi.fn(),
    onClearFilters: vi.fn(),
    totalCount: 10,
    filteredCount: 10,
    types: { status: 'loaded', types: ['GENE', 'PROTEIN', 'DISEASE'] },
    ...overrides,
  };
  const user = userEvent.setup();
  render(<EntityFilters {...props} />);
  return { ...props, user };
};

const openTypeMenu = async (user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> => {
  await user.click(screen.getByRole('button', { name: /types/i }));
  return screen.findByRole('menu');
};

describe('EntityFilters controls', () => {
  it('sends what is typed in the search box', () => {
    const props = setup();

    fireEvent.change(screen.getByRole('textbox', { name: 'Search entities by name' }), {
      target: { value: 'brca' },
    });

    expect(props.onSearchChange).toHaveBeenCalledWith('brca');
  });

  it('adds a type that is picked from the menu', async () => {
    const props = setup({ selectedTypes: ['GENE' as EntityType] });

    const menu = await openTypeMenu(props.user);
    await props.user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Protein' }));

    expect(props.onTypesChange).toHaveBeenCalledWith(['GENE', 'PROTEIN']);
  });

  it('removes a type that is picked again', async () => {
    const props = setup({ selectedTypes: ['GENE', 'PROTEIN'] as EntityType[] });

    const menu = await openTypeMenu(props.user);
    await props.user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Gene' }));

    expect(props.onTypesChange).toHaveBeenCalledWith(['PROTEIN']);
  });

  it('Select all clears the selection when every type is already picked', async () => {
    const props = setup({ selectedTypes: ['GENE', 'PROTEIN', 'DISEASE'] as EntityType[] });

    const menu = await openTypeMenu(props.user);
    await props.user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Select all' }));

    expect(props.onTypesChange).toHaveBeenCalledWith([]);
  });

  it('narrows the type menu to the types matching its search', async () => {
    const props = setup();

    const menu = await openTypeMenu(props.user);
    fireEvent.change(within(menu).getByRole('textbox', { name: 'Search entity types' }), {
      target: { value: 'pro' },
    });

    const labels = within(menu)
      .getAllByRole('menuitemcheckbox')
      .map((item) => item.textContent?.trim());
    expect(labels).toEqual(['Select all', 'Protein']);
  });

  it('says so when no type matches the search', async () => {
    const props = setup();

    const menu = await openTypeMenu(props.user);
    fireEvent.change(within(menu).getByRole('textbox', { name: 'Search entity types' }), {
      target: { value: 'zzz' },
    });

    expect(within(menu).getByText('No types found')).toBeInTheDocument();
  });

  it('removes one type from its active-filter chip', () => {
    const props = setup({ selectedTypes: ['GENE', 'PROTEIN'] as EntityType[] });

    fireEvent.click(screen.getByRole('button', { name: 'Remove Gene filter' }));

    expect(props.onTypesChange).toHaveBeenCalledWith(['PROTEIN']);
  });

  it('clears the search from its chip', () => {
    const props = setup({ searchQuery: 'brca' });

    fireEvent.click(screen.getByRole('button', { name: 'Clear search filter' }));

    expect(props.onSearchChange).toHaveBeenCalledWith('');
  });

  it('resets the confidence range from its chip', () => {
    const props = setup({ confidenceRange: [80, 100] });

    fireEvent.click(screen.getByRole('button', { name: 'Clear confidence filter' }));

    expect(props.onConfidenceChange).toHaveBeenCalledWith([0, 100]);
  });

  it('applies the High confidence preset', async () => {
    const props = setup();

    await props.user.click(screen.getByRole('button', { name: /0% - 100%/ }));
    await props.user.click(await screen.findByRole('button', { name: 'High (80%+)' }));

    expect(props.onConfidenceChange).toHaveBeenCalledWith([80, 100]);
  });

  it('splits the picked sort into field and order', async () => {
    const props = setup();

    await props.user.click(screen.getByRole('combobox', { name: 'Sort entities' }));
    await props.user.click(await screen.findByRole('option', { name: 'Name Z-A' }));

    expect(props.onSortChange).toHaveBeenCalledWith('name', 'desc');
  });

  it('Clear all clears every filter', () => {
    const props = setup({ searchQuery: 'brca' });

    fireEvent.click(screen.getByRole('button', { name: /clear all/i }));

    expect(props.onClearFilters).toHaveBeenCalledTimes(1);
  });
});
