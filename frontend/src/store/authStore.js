import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import apiClient from '../api/client';

export const ROLE_OPERATOR = 'operator';
export const ROLE_GUEST = 'guest';

// Canonical broker ids used across the store and components.
export const BROKERS = {
  paper:    { id: 'paper',    label: 'Paper Trading', isLive: false },
  dhan:     { id: 'dhan',     label: 'Dhan',          isLive: true  },
  angelone: { id: 'angelone', label: 'AngelOne',       isLive: true  },
  fyers:    { id: 'fyers',    label: 'Fyers',          isLive: true  },
};

export const useAuthStore = create(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      isAuthenticated: false,
      role: null,
      // selectedExecutionBroker: the primary broker the operator chose at login.
      // 'paper' = simulated fills, any other value = LIVE execution via that broker.
      // Defaults to 'dhan' — Dhan is the sole F&O execution broker per architecture.
      selectedExecutionBroker: 'dhan',
      selectedAssetClass: 'AUTO_OPTIONS',

      // Guests get a real server-issued read-only token.
      loginGuest: async () => {
        try {
          const { data } = await apiClient.post('/api/v1/auth/guest');
          if (!data?.access_token) {
            return { success: false, error: 'Guest session was not issued by the server' };
          }
          set({
            token: data.access_token,
            user: data.user ?? { username: 'guest', role: ROLE_GUEST },
            role: data.user?.role ?? ROLE_GUEST,
            isAuthenticated: true,
          });
          return { success: true };
        } catch (error) {
          return {
            success: false,
            error: error.response?.data?.detail || error.message || 'Guest sign-in failed',
          };
        }
      },

      // executionBroker: 'paper' | 'dhan' | 'angelone' | 'fyers'
      login: async (username, password, executionBroker = 'dhan') => {
        const isLive = executionBroker !== 'paper';
        const execMode = isLive ? 'LIVE' : 'PAPER';

        try {
          const { data } = await apiClient.post('/api/v1/auth/login', { username, password });
          if (!data?.access_token) {
            return { success: false, error: 'Server did not issue an access token' };
          }
          set({
            token: data.access_token,
            user: data.user ?? { username },
            role: data.user?.role ?? null,
            isAuthenticated: true,
            selectedExecutionBroker: executionBroker,
          });

          // Arm server-side execution mode to match the chosen broker context.
          try {
            await apiClient.post('/api/v1/trading/execution-mode', { mode: execMode });
          } catch (e) {
            return {
              success: true,
              warning: `Signed in, but execution mode could not be set to ${execMode}.`,
            };
          }

          // For live brokers, also tell the backend which adapter is the active one.
          if (isLive) {
            try {
              await apiClient.post('/api/v1/brokers/active', { broker: executionBroker });
            } catch (e) {
              // Non-fatal: dashboard can still show correct state, backend defaults to angelone.
            }
          }

          return { success: true };
        } catch (error) {
          const message =
            error.response?.data?.detail ||
            error.response?.data?.message ||
            error.message ||
            'Authentication failed';
          return { success: false, error: message };
        }
      },

      logout: () => {
        // Save token before clearing so we can notify the backend with it.
        const savedToken = useAuthStore.getState().token;
        // Clear local state immediately — UI reacts without waiting for backend RTT.
        set({ token: null, user: null, isAuthenticated: false, role: null });
        // Fire-and-forget: invalidate the JWT server-side.
        // Using the saved token directly so the request doesn't go unauthenticated.
        if (savedToken) {
          apiClient.post('/api/v1/auth/logout', null, {
            headers: { Authorization: `Bearer ${savedToken}` },
          }).catch(() => {
            // Backend logout failed (network error or token already expired) —
            // local state is already cleared, so this is safe to ignore.
          });
        }
      },

      isGuest: () => get().role === ROLE_GUEST,
      canTrade: () => get().role === ROLE_OPERATOR,

      setExecutionBroker: (broker) => set({ selectedExecutionBroker: broker }),
      // Keep legacy setter name so BrokerSetup.jsx and other components don't break.
      setSelectedBroker: (broker) => set({ selectedExecutionBroker: broker }),
      setAssetClass: (assetClass) => set({ selectedAssetClass: assetClass }),
    }),
    {
      name: 'strike_desk_auth',
      storage: createJSONStorage(() => localStorage),
    }
  )
);

export const useCanTrade = () => useAuthStore((s) => s.role === ROLE_OPERATOR);
export const useIsGuest  = () => useAuthStore((s) => s.role === ROLE_GUEST);
