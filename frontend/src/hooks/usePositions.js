import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';
import { REQ, statusFromAxiosError } from '../utils/requestState';

export const POSITIONS_POLL_MS = 3000;

// Field values are passed through as-is. Substituting a default here would make a
// real position indistinguishable from a placeholder.
function normalize(data) {
  if (!Array.isArray(data)) return [];
  return data.map((p) => {
    const qty = typeof p.quantity === 'number' ? p.quantity : null;
    const entry = typeof p.entry_price === 'number' ? p.entry_price
      : typeof p.buy_price === 'number' ? p.buy_price : null;
    const ltp = typeof p.current_price === 'number' ? p.current_price
      : typeof p.ltp === 'number' ? p.ltp : null;
    const pnl = typeof p.pnl === 'number' ? p.pnl
      : typeof p.unrealized_pnl === 'number' ? p.unrealized_pnl : null;
    return {
      position_id: p.position_id ?? p.symbol ?? null,
      symbol: p.symbol ?? null,
      direction: p.direction ?? (qty == null ? null : qty >= 0 ? 'LONG' : 'SHORT'),
      lots: p.lots ?? null,
      quantity: qty == null ? null : Math.abs(qty),
      raw_qty: qty,   // signed; needed by the Exit button to size the closing order
      entry_price: entry,
      ltp,
      pnl,
    };
  }).filter((p) => p.quantity != null && p.quantity !== 0).slice(0, 4);
}

export function usePositions() {
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [backendOnline, setBackendOnline] = useState(false);
  const [status, setStatus] = useState(REQ.LOADING);

  const fetch = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/trading/positions');
      setPositions(normalize(data));
      setBackendOnline(true);
      setStatus(REQ.OK);
      setError(null);
    } catch (err) {
      // No fabricated rows on failure — the panel shows the error state instead.
      setPositions([]);
      setBackendOnline(false);
      setStatus(statusFromAxiosError(err));
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetch();
    const t = setInterval(fetch, POSITIONS_POLL_MS);
    return () => clearInterval(t);
  }, [fetch]);

  return { positions, loading, status, error, backendOnline, refresh: fetch };
}

export default usePositions;
