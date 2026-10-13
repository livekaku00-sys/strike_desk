import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';
import { REQ, statusFromAxiosError } from '../utils/requestState';
import { useSystemState } from '../context/SystemStateContext';

export const EXEC_PAPER = 'PAPER';
export const EXEC_LIVE = 'LIVE';

/**
 * The execution mode is server state, not a client preference — the auto-execution
 * loop obeys the same switch — so it is always read back from the API rather than
 * assumed from whatever was chosen at login. A failed read reports its status and
 * no mode; it never falls back to PAPER or LIVE.
 */
export function useExecutionMode({ pollMs = 5000, enabled = true } = {}) {
  const [execMode, setExecMode] = useState({ status: REQ.LOADING, value: null, updatedAt: null });

  const fetchMode = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/trading/execution-mode');
      setExecMode({ status: REQ.OK, value: data?.execution_mode ?? null, updatedAt: Date.now() });
    } catch (err) {
      setExecMode({ status: statusFromAxiosError(err), value: null, updatedAt: null });
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    fetchMode();
    const t = setInterval(fetchMode, pollMs);
    return () => clearInterval(t);
  }, [enabled, fetchMode, pollMs]);

  const changeMode = useCallback(async (next) => {
    const { data } = await apiClient.post('/api/v1/trading/execution-mode', { mode: next });
    setExecMode({ status: REQ.OK, value: data?.execution_mode ?? next, updatedAt: Date.now() });
    return data;
  }, []);

  const mode = execMode.status === REQ.OK ? execMode.value : null;
  return {
    execMode,
    mode,
    isPaper: mode === EXEC_PAPER,
    loading: execMode.status === REQ.LOADING,
    changeMode,
    refresh: fetchMode,
  };
}

// Inside DashboardLayout the mode is already polled once for everyone; elsewhere
// (the guest page) the component polls for itself, as before.
export function useSharedExecutionMode() {
  const shared = useSystemState();
  const own = useExecutionMode({ enabled: !shared });
  return shared ? shared.execMode : own.execMode;
}

export default useExecutionMode;
