import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';
import { REQ, statusFromAxiosError } from '../utils/requestState';

export const ORDERS_POLL_MS = 3000;

function toTimeStr(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleTimeString('en-IN', {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch (_) {
    return '—';
  }
}

// Field values are passed through as-is. Substituting a default here would make a
// real order indistinguishable from a placeholder — a missing status must not read
// as FILLED, and a market order (no price) must not read as a limit price.
function normalize(data) {
  if (!Array.isArray(data)) return [];
  return data.map((o) => ({
    order_id: o.order_id ?? null,
    status: (o.status ?? 'UNKNOWN').toUpperCase(),
    time: toTimeStr(o.placed_at || o.timestamp),
    symbol: o.symbol ?? null,
    quantity: o.quantity ?? null,
    price: typeof o.price === 'number' ? o.price : null,
    reject_reason: o.reject_reason ?? null,
  })).slice(0, 5);
}

export function useOrders() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [backendOnline, setBackendOnline] = useState(false);
  const [status, setStatus] = useState(REQ.LOADING);

  const fetch = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/trading/orders');
      setOrders(normalize(data));
      setBackendOnline(true);
      setStatus(REQ.OK);
      setError(null);
    } catch (err) {
      // No fabricated rows on failure — the panel shows the error state instead.
      setOrders([]);
      setBackendOnline(false);
      setStatus(statusFromAxiosError(err));
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetch();
    const t = setInterval(fetch, ORDERS_POLL_MS);
    return () => clearInterval(t);
  }, [fetch]);

  return { orders, loading, status, error, backendOnline, refresh: fetch };
}

export default useOrders;
