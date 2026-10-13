/**
 * Unified facade over authStore + trading API side-effects.
 * Use this hook instead of useAuthStore when components need:
 *   - setAssetClass to also sync to the backend trading mode
 *   - a single import for both auth and trading state
 */
import { useAuthStore } from './authStore';
import apiClient from '../api/client';

export function useStore() {
  const state = useAuthStore();

  const setBroker = (broker) => {
    state.setSelectedBroker(broker);
  };

  const setAssetClass = async (assetClass) => {
    state.setAssetClass(assetClass);
    try {
      await apiClient.post('/api/v1/trading/mode', { mode: assetClass });
    } catch (_) {}
  };

  return {
    // Auth
    token: state.token,
    user: state.user,
    isAuthenticated: state.isAuthenticated,
    login: state.login,
    logout: state.logout,
    // Trading
    activeBroker: state.selectedExecutionBroker || 'dhan',
    activeAssetClass: state.selectedAssetClass || 'AUTO_CASH_INTRADAY',
    // Actions (with API side-effects)
    setBroker,
    setAssetClass,
  };
}

export default useStore;
