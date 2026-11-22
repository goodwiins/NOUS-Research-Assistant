import { NextRequest, NextResponse } from 'next/server';
import { triggerUpstreamError } from '@/lib/trigger-proxy';
import { createClient } from '@/lib/supabase/server';
import { createHmac } from 'node:crypto';

const TRIGGER_API_URL =
  process.env.TRIGGER_API_URL ?? 'https://api.trigger.dev';
const TRIGGER_SECRET_KEY = process.env.TRIGGER_SECRET_KEY;

function agentTicket(userId: string): string {
  const key = process.env.TRIGGER_AGENT_SIGNING_KEY;
  if (!key || Buffer.byteLength(key) < 32) {
    throw new Error('Trigger agent signing key is not configured');
  }
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(
    JSON.stringify({ alg: 'HS256', typ: 'JWT' })
  ).toString('base64url');
  const claims = Buffer.from(
    JSON.stringify({
      iss: 'nous-trigger',
      aud: 'nous-backend',
      sub: userId,
      scope: 'trigger:agent',
      iat: now,
      exp: now + 90 * 60,
    })
  ).toString('base64url');
  const signingInput = `${header}.${claims}`;
  const signature = createHmac('sha256', key)
    .update(signingInput)
    .digest('base64url');
  return `${signingInput}.${signature}`;
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (
    !TRIGGER_SECRET_KEY ||
    !process.env.TRIGGER_AGENT_SIGNING_KEY ||
    Buffer.byteLength(process.env.TRIGGER_AGENT_SIGNING_KEY) < 32
  ) {
    return NextResponse.json(
      { error: 'Trigger.dev not configured' },
      { status: 503 }
    );
  }

  const supabase = await createClient();
  // getUser() verifies the session with Supabase before we mint a scoped ticket.
  const {
    data: { user },
    error: authError,
  } = await supabase.auth.getUser();
  if (authError || !user) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const body = await request.json();

  const payload = {
    messages: body.messages,
    pageContext: body.page_context,
    model: body.model,
    useRag: body.use_rag,
    maxContextDocs: body.max_context_docs,
    threadId: body.thread_id,
    userId: user.id,
    backendToken: agentTicket(user.id),
  };

  const res = await fetch(
    `${TRIGGER_API_URL}/api/v1/tasks/execute-agent/trigger`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${TRIGGER_SECRET_KEY}`,
      },
      body: JSON.stringify({ payload }),
    }
  );

  if (!res.ok) {
    return triggerUpstreamError(res, 'agent run trigger');
  }

  const data = await res.json();
  return NextResponse.json({ runId: data.id });
}
