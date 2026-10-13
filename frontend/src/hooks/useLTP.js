import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';

/**
 * Polls /api/v1/market/ticks every 5s and returns a map of
 * { SYMBOL_UPPER: ltp } for all actively streaming instruments.
 * Returns an empty object when the market feed is offline.
 */
export function useLTP() {
  const [ltpMap, setLtpMap] = useState({});

  const fetchTicks = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/market/ticks');
      if (Array.isArray(data) && data.length > 0) {
        const map = {};
        data.forEach((tick) => {
          if (tick.symbol && tick.ltp != null) {
            map[tick.symbol.toUpperCase()] = tick.ltp;
          }
        });
        setLtpMap(map);
      }
    } catch (_) {
      // silent — LTP is best-effort; signal cards remain usable without it
    }
  }, []);

  useEffect(() => {
    fetchTicks();
    const t = setInterval(fetchTicks, 5000);
    return () => clearInterval(t);
  }, [fetchTicks]);

  return ltpMap;
}

export default useLTP;
