/**
 * GOO-770: a project create/update/delete started while workspace A's list
 * was on screen must not change workspace B's list, total, error or spinner
 * when it settles after a switch. The server change has happened; the next
 * fetch of A shows it.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import { useProjectStore } from '../projectStore';
import { projectService } from '@/services/projectService';
import type { Project } from '@/services/projectService';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listProjects: vi.fn(),
    createProject: vi.fn(),
    updateProject: vi.fn(),
    deleteProject: vi.fn(),
  },
}));

const svc = projectService as Mocked<typeof projectService>;
const project = (id: string, name = `Project ${id}`): Project =>
  ({ id, name }) as Project;
const state = (): ReturnType<typeof useProjectStore.getState> =>
  useProjectStore.getState();

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Load a workspace's list. B's list holds a project with A's shared id. */
async function showWorkspace(ws: 'A' | 'B'): Promise<void> {
  const projects =
    ws === 'A'
      ? [project('shared', 'A shared'), project('a1')]
      : [project('shared', 'B shared'), project('b1'), project('b2')];
  svc.listProjects.mockResolvedValueOnce({
    projects,
    total: projects.length,
  } as never);
  await state().fetchProjects({ workspace_id: `ws-${ws}` });
}

function expectBUntouched(): void {
  expect(state().projects.map((p) => [p.id, p.name])).toEqual([
    ['shared', 'B shared'],
    ['b1', 'Project b1'],
    ['b2', 'Project b2'],
  ]);
  expect(state().total).toBe(3);
  expect(state().error).toBeNull();
  expect(state().mutating).toBe(false);
}

describe('projectStore project mutations across a workspace switch (GOO-770)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    state().reset();
  });

  const cases = [
    {
      name: 'createProject',
      mock: (): { mockReturnValueOnce(v: unknown): void } =>
        svc.createProject as never,
      start: (): Promise<unknown> =>
        state().createProject({ name: 'new', workspace_id: 'ws-A' } as never),
      value: project('a-new'),
    },
    {
      name: 'updateProject',
      mock: (): { mockReturnValueOnce(v: unknown): void } =>
        svc.updateProject as never,
      start: (): Promise<unknown> =>
        state().updateProject('shared', { name: 'A renamed' } as never),
      value: project('shared', 'A renamed'),
    },
    {
      name: 'deleteProject',
      mock: (): { mockReturnValueOnce(v: unknown): void } =>
        svc.deleteProject as never,
      start: (): Promise<unknown> => state().deleteProject('shared'),
      value: undefined,
    },
  ] as const;

  it.each(cases)(
    "$name from A resolving after the switch leaves B's list and total alone",
    async (c) => {
      await showWorkspace('A');
      const call = deferred<unknown>();
      c.mock().mockReturnValueOnce(call.promise);
      const pending = c.start();
      expect(state().mutating).toBe(true);

      await showWorkspace('B');
      expect(state().mutating).toBe(false);
      call.resolve(c.value);
      await pending;

      expectBUntouched();
    }
  );

  it.each(cases)(
    '$name from A failing after the switch sets no error on B, and still rejects',
    async (c) => {
      await showWorkspace('A');
      const call = deferred<unknown>();
      c.mock().mockReturnValueOnce(call.promise);
      const pending = c.start();

      await showWorkspace('B');
      call.reject(new Error(`${c.name} failed`));

      await expect(pending).rejects.toThrow(`${c.name} failed`);
      expectBUntouched();
    }
  );

  it.each(cases)(
    "$name from A settling does not clear B's own in-flight mutation spinner",
    async (c) => {
      await showWorkspace('A');
      const call = deferred<unknown>();
      c.mock().mockReturnValueOnce(call.promise);
      const pending = c.start();

      await showWorkspace('B');
      const bCreate = deferred<Project>();
      svc.createProject.mockReturnValueOnce(bCreate.promise);
      const bPending = state().createProject({
        name: 'b-new',
        workspace_id: 'ws-B',
      } as never);

      call.resolve(c.value);
      await pending;
      expect(state().mutating).toBe(true);

      bCreate.resolve(project('b-new'));
      await bPending;
      expect(state().mutating).toBe(false);
      expect(state().projects[0].id).toBe('b-new');
    }
  );

  it('createProject from A still returns the created project after the switch', async () => {
    await showWorkspace('A');
    const call = deferred<Project>();
    svc.createProject.mockReturnValueOnce(call.promise);
    const pending = state().createProject({
      name: 'new',
      workspace_id: 'ws-A',
    } as never);

    await showWorkspace('B');
    call.resolve(project('a-new'));

    await expect(pending).resolves.toEqual(project('a-new'));
  });

  it("an update or delete of the open project still applies to it after a switch", async () => {
    await showWorkspace('A');
    state().setCurrentProject(project('a1'));
    const update = deferred<Project>();
    svc.updateProject.mockReturnValueOnce(update.promise);
    const updating = state().updateProject('a1', { name: 'renamed' } as never);
    await showWorkspace('B');
    update.resolve(project('a1', 'renamed'));
    await updating;
    expect(state().currentProject?.name).toBe('renamed');

    const del = deferred<void>();
    svc.deleteProject.mockReturnValueOnce(del.promise);
    const deleting = state().deleteProject('a1');
    await showWorkspace('A');
    del.resolve(undefined);
    await deleting;
    expect(state().currentProject).toBeNull();
  });

  it('a refetch of the same workspace (new filters) is not a switch', async () => {
    await showWorkspace('B');
    const call = deferred<Project>();
    svc.createProject.mockReturnValueOnce(call.promise);
    const pending = state().createProject({
      name: 'new',
      workspace_id: 'ws-B',
    } as never);

    svc.listProjects.mockResolvedValueOnce({
      projects: [project('b1')],
      total: 1,
    } as never);
    await state().fetchProjects({ workspace_id: 'ws-B', search: 'b' });
    expect(state().mutating).toBe(true);
    call.resolve(project('b-new'));
    await pending;

    expect(state().projects.map((p) => p.id)).toEqual(['b-new', 'b1']);
    expect(state().total).toBe(2);
    expect(state().mutating).toBe(false);
  });

  it('mutations in the workspace on screen still commit and report errors', async () => {
    await showWorkspace('B');

    svc.createProject.mockResolvedValueOnce(project('b-new'));
    await state().createProject({ name: 'n', workspace_id: 'ws-B' } as never);
    expect(state().projects.map((p) => p.id)).toEqual([
      'b-new',
      'shared',
      'b1',
      'b2',
    ]);
    expect(state().total).toBe(4);

    svc.updateProject.mockResolvedValueOnce(project('b1', 'renamed'));
    await state().updateProject('b1', { name: 'renamed' } as never);
    expect(state().projects[2].name).toBe('renamed');

    svc.deleteProject.mockResolvedValueOnce(undefined);
    await state().deleteProject('b2');
    expect(state().projects.map((p) => p.id)).toEqual(['b-new', 'shared', 'b1']);
    expect(state().total).toBe(3);
    expect(state().mutating).toBe(false);

    svc.createProject.mockRejectedValueOnce(new Error('create failed'));
    await expect(
      state().createProject({ name: 'n' } as never)
    ).rejects.toThrow();
    expect(state().error).toBe('create failed');
    expect(state().mutating).toBe(false);

    svc.updateProject.mockRejectedValueOnce(new Error('update failed'));
    await expect(state().updateProject('b1', {} as never)).rejects.toThrow();
    expect(state().error).toBe('update failed');

    svc.deleteProject.mockRejectedValueOnce(new Error('delete failed'));
    await expect(state().deleteProject('b1')).rejects.toThrow();
    expect(state().error).toBe('delete failed');
    expect(state().mutating).toBe(false);
  });

  it('reset drops the scope: a mutation from before it does not land after', async () => {
    await showWorkspace('A');
    const call = deferred<Project>();
    svc.createProject.mockReturnValueOnce(call.promise);
    const pending = state().createProject({
      name: 'new',
      workspace_id: 'ws-A',
    } as never);

    state().reset();
    await showWorkspace('A');
    call.resolve(project('a-new'));
    await pending;

    expect(state().projects.map((p) => p.id)).toEqual(['shared', 'a1']);
    expect(state().total).toBe(2);
  });
});
