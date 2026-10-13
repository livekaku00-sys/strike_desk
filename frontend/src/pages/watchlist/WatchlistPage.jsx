/**
 * Strike Desk V4.5 — Watchlist Page
 * Top-10 high-probability stocks from strategy engine.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { strategy as stratApi, market as mktApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock, Freshness,
} from '../../components/ui';

const TIER_COLOR = { 'A+': 'green', A: 'teal', B: 'amber' };
const POLL = 15000;

export default function WatchlistPage() {
  const [watchlist, setWatchlist] = useState([]);
  const [loading, setLoading]     = useState(true);
  const [err, setErr]             = useState(null);
  const [lastFetch, setLastFetch] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const fetch = useCallback(async () => {
    try {
      const r = await stratApi.watchlist();
      const arr = r.data?.watchlist ?? r.data?.stocks ?? r.data ?? [];
      setWatchlist(Array.isArray(arr) ? arr : []);
      setLastFetch(Date.now());
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  const forceRefresh = useCallback(async () => {
    setRefreshing(true);
    try { await stratApi.refreshWL(); await fetch(); }
    catch(e) { setErr(e.response?.data?.detail || e.message); }
    finally { setRefreshing(false); }
  }, [fetch]);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const scored = [...watchlist].sort((a, b) => (b.score ?? b.confidence_score ?? 0) - (a.score ?? a.confidence_score ?? 0));

  return (
    <div>
      <PageHeader
        title="Watchlist"
        subtitle="Top-10 high-probability stocks — updated every 15s from strategy engine."
        right={
          <div className="flex items-center gap-3">
            {lastFetch && <Freshness ts={lastFetch} />}
            <ActionBtn onClick={forceRefresh} disabled={refreshing} color="amber">
              {refreshing ? 'Refreshing…' : 'Force Refresh'}
            </ActionBtn>
          </div>
        }
      />

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="WATCHLIST UNAVAILABLE" detail={err} /> : (
        <>
          <Panel>
            <PanelHeader title={`${scored.length} STOCKS`} subtitle="Ranked by score" />
            {scored.length === 0 ? (
              <EmptyBlock label="NO WATCHLIST" sub="Strategy engine has not populated the watchlist yet" />
            ) : (
              <>
                <div className="grid grid-cols-[32px_1fr_80px_80px_80px_80px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
                  <span>#</span><span>Symbol</span><span>Tier</span>
                  <span>Score</span><span>EMA9</span><span>VWAP</span><span>Sentiment</span>
                </div>
                {scored.map((s, i) => {
                  const score = s.score ?? s.confidence_score ?? 0;
                  const tier  = score >= 85 ? 'A+' : score >= 70 ? 'A' : 'B';
                  return (
                    <div key={s.symbol ?? i}
                      className="grid grid-cols-[32px_1fr_80px_80px_80px_80px_80px] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 items-center">
                      <span className="text-[11px] font-mono text-[#4A5568]">{i + 1}</span>
                      <div>
                        <div className="text-[13px] font-mono font-bold text-[#F5F7FA]">{s.symbol ?? '—'}</div>
                        {s.sector && <div className="text-[10px] text-[#4A5568]">{s.sector}</div>}
                      </div>
                      <StatusBadge text={tier} color={TIER_COLOR[tier]} />
                      <span className={`text-[12px] font-mono font-bold ${
                        score >= 85 ? 'text-green-300' : score >= 70 ? 'text-teal-300' : 'text-amber-300'
                      }`}>{score.toFixed ? score.toFixed(1) : score}</span>
                      <span className="text-[11px] font-mono text-[#8D98A8]">{s.ema9_score ?? '—'}</span>
                      <span className="text-[11px] font-mono text-[#8D98A8]">{s.vwap_score ?? '—'}</span>
                      <span className="text-[11px] font-mono text-[#8D98A8]">{s.sentiment_score ?? '—'}</span>
                    </div>
                  );
                })}
              </>
            )}
          </Panel>

          <div className="mt-3 px-1 text-[10px] font-mono text-[#4A5568]">
            Watchlist is populated by the strategy engine during market hours. Scores reflect latest available indicators.
            Hard gates are applied at signal generation, not here.
          </div>
        </>
      )}
    </div>
  );
}
