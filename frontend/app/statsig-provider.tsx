'use client';

import { useAuth } from '@/hooks/useAuth';
import {
  LogLevel,
  StatsigProvider,
  useClientAsyncInit,
} from '@statsig/react-bindings';
import { StatsigAutoCapturePlugin } from '@statsig/web-analytics';
import { useEffect, useMemo, useSyncExternalStore } from 'react';

interface StatsigClientProviderProps {
  children: React.ReactNode;
}

const SDK_KEY = process.env.NEXT_PUBLIC_STATSIG_CLIENT_KEY?.trim();

// False on the server and during hydration, true once mounted on the client,
// without a setState-in-effect re-render.
const subscribeToNothing = (): (() => void) => () => {};
const isClient = (): boolean => true;
const isServer = (): boolean => false;

function StatsigInitializedProvider({ children }: StatsigClientProviderProps) {
  const { user } = useAuth();

  const statsigUser = useMemo(
    () => ({
      userID: user?.id ?? 'anonymous',
      email: user?.email,
      customIDs: user?.organization_id
        ? { organizationID: user.organization_id }
        : undefined,
      custom: user
        ? { role: user.role, organization_id: user.organization_id }
        : undefined,
    }),
    [user]
  );

  const { client } = useClientAsyncInit(SDK_KEY ?? '', statsigUser, {
    logLevel:
      process.env.NODE_ENV === 'production' ? LogLevel.Warn : LogLevel.Debug,
    // No session replay (GOO-774): Statsig's recorder ran with no privacy
    // options, outside Sentry's masking, iframe block list and URL scrubbing.
    // Re-enabling it needs a config at least as strict as
    // REPLAY_PRIVACY_OPTIONS, proven in a real browser, and an entry in
    // src/lib/__tests__/sessionReplayRegistration.test.ts.
    plugins: [new StatsigAutoCapturePlugin()],
  });

  useEffect(() => {
    if (!client) return;
    void client.updateUserAsync(statsigUser);
  }, [client, statsigUser]);

  return <StatsigProvider client={client}>{children}</StatsigProvider>;
}

export function StatsigClientProvider({
  children,
}: StatsigClientProviderProps) {
  const mounted = useSyncExternalStore(subscribeToNothing, isClient, isServer);

  if (!SDK_KEY || !mounted) {
    return <>{children}</>;
  }

  return <StatsigInitializedProvider>{children}</StatsigInitializedProvider>;
}

export default StatsigClientProvider;
