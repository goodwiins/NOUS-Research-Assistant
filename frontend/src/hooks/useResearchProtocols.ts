'use client';

import { useCallback, useRef } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  researchProtocolService,
  type ProtocolApprovalRequest,
  type ProtocolApprovalResponse,
  type ProtocolDeviationCreate,
  type ProtocolRegistrationCreate,
  type ProtocolRegistrationResponse,
  type ProtocolDeviationResponse,
  type ResearchProtocolCreate,
  type ResearchProtocolResponse,
  type ResearchProtocolVersionCreate,
  type ResearchQuestionCreate,
  type ResearchQuestionResponse,
  type ResearchQuestionVersionCreate,
} from '@/services/researchProtocolService';
import { getProject } from '@/services/researchEngineService';
import {
  DAILY_BRIEF_AMENDMENT_REASON,
  DAILY_BRIEF_PROTOCOL_NAME,
  dailyBriefProtocolSnapshot,
} from '@/utils/dailyBriefProtocol';

export const researchProtocolKeys = {
  root: (projectId: string) => ['project', projectId, 'protocols'] as const,
  questions: (projectId: string) =>
    [...researchProtocolKeys.root(projectId), 'questions'] as const,
  protocols: (projectId: string) =>
    [...researchProtocolKeys.root(projectId), 'list'] as const,
  registrations: (projectId: string, protocolId: string) =>
    [
      ...researchProtocolKeys.root(projectId),
      protocolId,
      'registrations',
    ] as const,
  deviations: (projectId: string, protocolId: string) =>
    [
      ...researchProtocolKeys.root(projectId),
      protocolId,
      'deviations',
    ] as const,
};

export function useResearchQuestions(
  projectId: string
): UseQueryResult<ResearchQuestionResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.questions(projectId),
    queryFn: () => researchProtocolService.listQuestions(projectId),
    enabled: Boolean(projectId),
  });
}

export function useResearchProtocols(
  projectId: string
): UseQueryResult<ResearchProtocolResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.protocols(projectId),
    queryFn: () => researchProtocolService.listProtocols(projectId),
    enabled: Boolean(projectId),
  });
}

export interface ResearchProtocolActions {
  createQuestion: UseMutationResult<
    ResearchQuestionResponse,
    Error,
    ResearchQuestionCreate
  >;
  createQuestionVersion: UseMutationResult<
    ResearchQuestionResponse,
    Error,
    { questionId: string; body: ResearchQuestionVersionCreate }
  >;
  createProtocol: UseMutationResult<
    ResearchProtocolResponse,
    Error,
    ResearchProtocolCreate
  >;
  createVersion: UseMutationResult<
    ResearchProtocolResponse,
    Error,
    { protocolId: string; body: ResearchProtocolVersionCreate }
  >;
  approve: UseMutationResult<
    ProtocolApprovalResponse,
    Error,
    { protocolId: string; versionId: string; body: ProtocolApprovalRequest }
  >;
  register: UseMutationResult<
    ProtocolRegistrationResponse,
    Error,
    { protocolId: string; body: ProtocolRegistrationCreate }
  >;
  recordDeviation: UseMutationResult<
    ProtocolDeviationResponse,
    Error,
    { protocolId: string; body: ProtocolDeviationCreate }
  >;
}

function useProtocolMutation<TVariables, TResult>(
  projectId: string,
  mutationFn: (variables: TVariables) => Promise<TResult>
): UseMutationResult<TResult, Error, TVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: researchProtocolKeys.root(projectId),
      });
    },
  });
}

export function useProtocolActions(projectId: string): ResearchProtocolActions {
  return {
    createQuestion: useProtocolMutation<
      ResearchQuestionCreate,
      ResearchQuestionResponse
    >(projectId, (body) =>
      researchProtocolService.createQuestion(projectId, body)
    ),
    createQuestionVersion: useProtocolMutation<
      { questionId: string; body: ResearchQuestionVersionCreate },
      ResearchQuestionResponse
    >(projectId, ({ questionId, body }) =>
      researchProtocolService.createQuestionVersion(questionId, body)
    ),
    createProtocol: useProtocolMutation<
      ResearchProtocolCreate,
      ResearchProtocolResponse
    >(projectId, (body) =>
      researchProtocolService.createProtocol(projectId, body)
    ),
    createVersion: useProtocolMutation<
      { protocolId: string; body: ResearchProtocolVersionCreate },
      ResearchProtocolResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.createProtocolVersion(protocolId, body)
    ),
    approve: useProtocolMutation<
      { protocolId: string; versionId: string; body: ProtocolApprovalRequest },
      ProtocolApprovalResponse
    >(projectId, ({ protocolId, versionId, body }) =>
      researchProtocolService.approveProtocolVersion(
        protocolId,
        versionId,
        body
      )
    ),
    register: useProtocolMutation<
      { protocolId: string; body: ProtocolRegistrationCreate },
      ProtocolRegistrationResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.recordRegistration(protocolId, body)
    ),
    recordDeviation: useProtocolMutation<
      { protocolId: string; body: ProtocolDeviationCreate },
      ProtocolDeviationResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.recordDeviation(protocolId, body)
    ),
  };
}

export function useProtocolRegistrations(
  projectId: string,
  protocolId?: string
): UseQueryResult<ProtocolRegistrationResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.registrations(projectId, protocolId ?? ''),
    queryFn: () => researchProtocolService.listRegistrations(protocolId!),
    enabled: Boolean(protocolId),
  });
}

export function useProtocolDeviations(
  projectId: string,
  protocolId?: string
): UseQueryResult<ProtocolDeviationResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.deviations(projectId, protocolId ?? ''),
    queryFn: () => researchProtocolService.listDeviations(protocolId!),
    enabled: Boolean(protocolId),
  });
}

/**
 * `stale`: a newer save (for example in another tab) owns the brief, so the
 * protocol draft was left as it is.
 */
export type DailyBriefProtocolSyncResult = 'synced' | 'stale';

/**
 * Create the Daily Research Brief's question version and protocol draft (or
 * amendment) for a newly saved brief blueprint. Approval still belongs to an
 * independent assigned supervisor in ProtocolPanel.
 */
export function useDailyBriefProtocolSync(
  projectId: string
): (
  blueprintId: string,
  parameters: Record<string, unknown>
) => Promise<DailyBriefProtocolSyncResult> {
  const queryClient = useQueryClient();
  // A question created before a failed protocol call is reused on retry, so
  // pressing Save again does not create a duplicate question record.
  const unboundQuestion = useRef<{ text: string; versionId: string }>();

  return useCallback(
    async (blueprintId, parameters) => {
      const questionText =
        typeof parameters.research_question === 'string'
          ? parameters.research_question.trim()
          : '';
      const snapshot = dailyBriefProtocolSnapshot(parameters);
      try {
        const protocols =
          await researchProtocolService.listProtocols(projectId);
        const brief = protocols.find(
          (candidate) => candidate.name === DAILY_BRIEF_PROTOCOL_NAME
        );

        if (!brief) {
          let questionVersionId =
            unboundQuestion.current?.text === questionText
              ? unboundQuestion.current.versionId
              : undefined;
          if (!questionVersionId) {
            const created = await researchProtocolService.createQuestion(
              projectId,
              { question: questionText, framework: {} }
            );
            questionVersionId = created.current_version_id;
            unboundQuestion.current = {
              text: questionText,
              versionId: questionVersionId,
            };
          }
          await researchProtocolService.createProtocol(projectId, {
            name: DAILY_BRIEF_PROTOCOL_NAME,
            question_version_id: questionVersionId,
            blueprint_id: blueprintId,
            snapshot,
          });
          unboundQuestion.current = undefined;
          return 'synced';
        }

        const draftId = brief.current_draft_version_id;
        const draft = brief.versions?.find((version) => version.id === draftId);
        if (!draftId || !draft) {
          throw new Error('The brief protocol has no draft version.');
        }
        const questions =
          await researchProtocolService.listQuestions(projectId);
        const question = questions.find(
          (candidate) =>
            candidate.current_version_id === draft.question_version_id ||
            (candidate.versions ?? []).some(
              (version) => version.id === draft.question_version_id
            )
        );
        if (!question) {
          throw new Error('The brief protocol question was not found.');
        }
        let questionVersionId = question.current_version_id;
        if (question.current_version.question !== questionText) {
          const updated = await researchProtocolService.createQuestionVersion(
            question.id,
            {
              question: questionText,
              framework: {},
              parent_version_id: question.current_version_id,
            }
          );
          questionVersionId = updated.current_version_id;
        }
        // A newer save (for example in another tab) owns the protocol draft
        // now; amending it for this older blueprint would displace it. Check
        // right before the write, after the slower calls above.
        const project = (await getProject(projectId)) as {
          blueprint_id?: string | null;
        };
        if (project.blueprint_id !== blueprintId) {
          return 'stale';
        }
        await researchProtocolService.createProtocolVersion(brief.id, {
          question_version_id: questionVersionId,
          blueprint_id: blueprintId,
          snapshot,
          parent_version_id: draftId,
          amendment_reason: DAILY_BRIEF_AMENDMENT_REASON,
        });
        return 'synced';
      } finally {
        await queryClient.invalidateQueries({
          queryKey: researchProtocolKeys.root(projectId),
        });
      }
    },
    [projectId, queryClient]
  );
}
