import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';
import { REQ, statusFromAxiosError } from '../utils/requestState';

export const RISK_POLL_MS = 3000;

// Nulls, not placeholder numbers. A fabricated P&L or trade count on this panel
// would misrepresent how much risk budget is actually left.
const EMPTY = {
  daily_pnl: null,
  daily_loss_ceiling: null,
  trade_count: null,
  max_daily_trades: null,
  emergency_halt_active: false,
  cascade_brake: null,
  position_size: null,
  cooldown_active: null,
  cooldown_until: null,
};

function normalize(data) {
  if (!data || typeof data !== 'object') return { ...EMPTY };
  return {
    daily_pnl: typeof data.realized_pnl === 'number' ? data.realized_pnl : null,
    daily_loss_ceiling: typeof data.daily_loss_ceiling === 'number'
      ? Math.abs(data.daily_loss_ceiling) : null,
    trade_count: data.trade_count ?? null,
    max_daily_trades: data.max_daily_trades ?? null,
    emergency_halt_active: Boolean(data.emergency_halt_active),
    cascade_brake: data.cascade_brake == null ? null : (data.cascade_brake ? 'Active' : 'No'),
    position_size: data.position_size ?? null,
    // null = the backend did not report it (shown as NOT EXPOSED, not as "none").
    cooldown_active: typeof data.cooldown_active === 'boolean' ? data.cooldown_active : null,
    cooldown_until: data.cooldown_until ?? null,
  };
}

export function useRiskState() {
  const [riskState, setRiskState] = useState({ ...EMPTY });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [backendOnline, setBackendOnline] = useState(false);
  const [status, setStatus] = useState(REQ.LOADING);

  const fetch = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/risk/state');
      setRiskState(normalize(data));
      setBackendOnline(true);
      setStatus(REQ.OK);
      setError(null);
    } catch (err) {
      setRiskState({ ...EMPTY });
      setBackendOnline(false);
      setStatus(statusFromAxiosError(err));
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetch();
    const t = setInterval(fetch, RISK_POLL_MS);
    return () => clearInterval(t);
  }, [fetch]);

  return { riskState, loading, status, error, backendOnline, refresh: fetch };
}

export default useRiskState;
