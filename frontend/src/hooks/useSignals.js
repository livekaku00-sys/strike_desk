import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';

export const ASSET_CLASSES = [
  'AUTO_OPTIONS',
  'AUTO_FUTURES',
  'AUTO_STOCK_FUTURES',
  'AUTO_STOCK_OPTIONS',
  'AUTO_CASH_INTRADAY',
  'AUTO_CASH_SWING',
  'AUTO_CASH_DELIVERY',
];

export const ASSET_CLASS_LABELS = {
  AUTO_OPTIONS: 'Index Options',
  AUTO_FUTURES: 'Index Futures',
  AUTO_STOCK_FUTURES: 'Stock Futures',
  AUTO_STOCK_OPTIONS: 'Stock Options',
  AUTO_CASH_INTRADAY: 'Cash — Intraday',
  AUTO_CASH_SWING: 'Cash — Swing',
  AUTO_CASH_DELIVERY: 'Cash — Delivery',
};

const SIGNALS_PER_CLASS = 2;

/**
 * Groups live signals by asset class, keeping the top N per class by score.
 *
 * Classes with no live signal stay empty. Padding them with placeholders used to
 * put a live BUY button on a fabricated contract, so an absent signal must read
 * as absent.
 */
function groupSignals(raw, activeMode) {
  const grouped = Object.fromEntries(ASSET_CLASSES.map((c) => [c, []]));
  if (!Array.isArray(raw)) return grouped;

  raw.forEach((sig) => {
    // Backend sends both asset_class ("INDEX_OPTIONS", strategy enum) and
    // category ("AUTO_OPTIONS", the mode string). Group by the mode string.
    const cls = (sig.category || sig.asset_class || '').toUpperCase();
    if (!grouped[cls]) return;
    const score = typeof sig.confidence_score === 'number'
      ? sig.confidence_score
      : (typeof sig.score === 'number' ? sig.score : null);
    grouped[cls].push({
      signal_id: sig.signal_id ?? null,
      asset_class: cls,
      symbol: sig.symbol ?? null,
      direction: sig.direction ? String(sig.direction).toUpperCase() : null,
      score,
      tier: sig.confidence_tier ?? sig.tier ?? null,
      status: sig.status ?? (cls === activeMode ? 'EXECUTING' : 'ALERT_ONLY'),
      entry_price: sig.entry_price ?? null,
      target_price: sig.target_price ?? null,
      stop_loss_price: sig.stop_loss_price ?? null,
      timestamp: sig.timestamp ?? null,
      // Contract identity travels with the signal: brokers spell a contract name
      // differently, so the order carries these fields rather than just the name.
      metadata: sig.metadata ?? null,
    });
  });

  ASSET_CLASSES.forEach((cls) => {
    grouped[cls] = grouped[cls]
      .sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity))
      .slice(0, SIGNALS_PER_CLASS);
  });

  return grouped;
}

export function useSignals(activeMode = 'AUTO_OPTIONS') {
  const [grouped, setGrouped] = useState(() => groupSignals([], activeMode));
  const [loading, setLoading] = useState(true);
  const [backendOnline, setBackendOnline] = useState(false);

  const fetchSignals = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/trading/signals');
      setGrouped(groupSignals(data, activeMode));
      setBackendOnline(true);
    } catch (_) {
      setGrouped(groupSignals([], activeMode));
      setBackendOnline(false);
    } finally {
      setLoading(false);
    }
  }, [activeMode]);

  useEffect(() => {
    fetchSignals();
    const t = setInterval(fetchSignals, 5000);
    return () => clearInterval(t);
  }, [fetchSignals]);

  const signals = ASSET_CLASSES.flatMap((c) => grouped[c]);

  return { signals, grouped, loading, backendOnline };
}

export default useSignals;
