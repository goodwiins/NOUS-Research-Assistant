'use client';

import React, {
  useEffect,
  createContext,
  useContext,
  useCallback,
  useMemo,
  useSyncExternalStore,
  ReactNode,
} from 'react';
import { User, Organization, RegisterRequest, RegisterResult } from '@/types';
import {
  getAccountSessionRevision,
  onAccountSessionReset,
} from '@/lib/account-session';
import { useAuthStore } from '@/stores/authStore';
import { isBackendSessionFailure } from '@/lib/backendAuthFailure';
import { useShallow } from 'zustand/react/shallow';

interface AuthContextType {
  user: User | null;
  organization: Organization | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  pendingEmailConfirmation: boolean;
  pendingConfirmationEmail: string | null;
  pendingSignupPossiblyExisting: boolean;
  clearPendingEmailConfirmation: () => void;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (userData: RegisterRequest) => Promise<RegisterResult>;
  signOut: () => Promise<void>;
  resetPassword: (email: string) => Promise<void>;
  fetchProfile: () => Promise<void>;
  /**
   * Sign out because `error` shows the session is gone. Does nothing unless
   * it is a 401 from our own backend origin (GOO-727), so a third-party 401
   * can never sign the user out.
   */
  handleAuthError: (error: unknown) => void;
  // Legacy aliases for backward compatibility
  login: (
    email: string,
    password: string,
    rememberMe?: boolean
  ) => Promise<void>;
  register: (userData: RegisterRequest) => Promise<RegisterResult>;
  // signOut is async; it resolves even when server-side revocation fails (the
  // local session is destroyed either way), so bare `logout()` call sites can
  // never produce an unhandled rejection.
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

interface AuthProviderProps {
  children: ReactNode;
}

export const AuthProvider: React.FC<AuthProviderProps> = ({ children }) => {
  const store = useAuthStore(
    useShallow((s) => ({
      clearPendingEmailConfirmation: s.clearPendingEmailConfirmation,
      error: s.error,
      fetchProfile: s.fetchProfile,
      initialize: s.initialize,
      isAuthenticated: s.isAuthenticated,
      isLoading: s.isLoading,
      organization: s.organization,
      pendingConfirmationEmail: s.pendingConfirmationEmail,
      pendingEmailConfirmation: s.pendingEmailConfirmation,
      pendingSignupPossiblyExisting: s.pendingSignupPossiblyExisting,
      resetPassword: s.resetPassword,
      signIn: s.signIn,
      signOut: s.signOut,
      signUp: s.signUp,
      user: s.user,
    }))
  );
  const accountRevision = useSyncExternalStore(
    onAccountSessionReset,
    getAccountSessionRevision,
    () => 0
  );

  // Initialize auth state on mount
  useEffect(() => {
    store.initialize();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only run on mount
  }, []);

  const handleAuthError = useCallback(
    (error: unknown) => {
      if (!isBackendSessionFailure(error)) return;
      // signOut is now async and may reject if the network revocation fails;
      // local state is cleared regardless, so swallow the rejection here.
      void store.signOut().catch(() => {});
    },
    [store]
  );

  const contextValue: AuthContextType = useMemo(
    () => ({
      user: store.user,
      organization: store.organization,
      isAuthenticated: store.isAuthenticated,
      isLoading: store.isLoading,
      error: store.error,
      pendingEmailConfirmation: store.pendingEmailConfirmation,
      pendingConfirmationEmail: store.pendingConfirmationEmail,
      pendingSignupPossiblyExisting: store.pendingSignupPossiblyExisting,
      clearPendingEmailConfirmation: store.clearPendingEmailConfirmation,
      signIn: store.signIn,
      signUp: store.signUp,
      signOut: store.signOut,
      resetPassword: store.resetPassword,
      fetchProfile: store.fetchProfile,
      handleAuthError,
      // Legacy aliases
      login: store.signIn,
      register: store.signUp,
      logout: store.signOut,
    }),
    // store actions are stable references; only primitives change
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      store.user,
      store.organization,
      store.isAuthenticated,
      store.isLoading,
      store.error,
      store.pendingEmailConfirmation,
      store.pendingConfirmationEmail,
      store.pendingSignupPossiblyExisting,
      handleAuthError,
    ]
  );

  return React.createElement(
    AuthContext.Provider,
    // Discard page-local drafts, dialogs and cached results at an account boundary.
    // A token refresh keeps this key, so unsaved work stays mounted.
    { value: contextValue, key: accountRevision },
    children
  );
};

// Hook to use auth context
export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

export default useAuth;
