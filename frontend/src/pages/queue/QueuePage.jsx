/**
 * Strike Desk V4.5 — Signal Queue Page
 * Signals waiting while a position is open (one-at-a-time gate).
 */
import React, { useState, useCallback, useEffect } from 'react';
import { strategy as stratApi, execution as execApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock, Freshness,
} from '../../components/ui';

const POLL = 5000;
const TIER_COLOR = { 'A+': 'green', A: 'teal', B: 'amber' };

export default function QueuePage() {
  const [queue, setQueue]         = useState([]);
  const [positions, setPositions] = useState([]);
  const [loading, setLoading]     = useState(true);
  const [err, setErr]             = useState(null);
  const [lastFetch, setLastFetch] = useState(null);

  const fetch = useCallback(async () => {
    try {
      const [rQ, rP] = await Promise.allSettled([
        stratApi.queue(),
        execApi.positions(),
      ]);
      if (rQ.status === 'fulfilled') {
        const arr = rQ.value.data?.queue ?? rQ.value.data?.signals ?? rQ.value.data ?? [];
        setQueue(Array.isArray(arr) ? arr : []);
      }
      if (rP.status === 'fulfilled') {
        const arr = rP.value.data?.positions ?? rP.value.data ?? [];
        setPositions(Array.isArray(arr) ? arr : []);
      }
      setLastFetch(Date.now());
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const hasPosition = positions.length > 0;

  return (
    <div>
      <PageHeader
        title="Signal Queue"
        subtitle="Signals waiting for position slot to clear. One-at-a-time gate active."
        right={
          <div className="flex items-center gap-3">
            {lastFetch && <Freshness ts={lastFetch} />}
            <ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>
          </div>
        }
      />

      {/* Gate status */}
      <div className={`mb-4 p-4 rounded border ${
        hasPosition ? 'bg-amber-950/20 border-amber-800/40' : 'bg-green-950/20 border-green-800/40'
      }`}>
        <div className="flex items-center gap-3">
          <StatusBadge
            text={hasPosition ? 'ONE-AT-A-TIME GATE: LOCKED' : 'ONE-AT-A-TIME GATE: OPEN'}
            color={hasPosition ? 'amber' : 'green'}
          />
        </div>
        <div className="text-[11px] text-[#8D98A8] mt-2">
          {hasPosition
            ? `${positions.length} open position${positions.length > 1 ? 's' : ''}. New signals queue here until position clears.`
            : 'No open positions. Next queued signal can execute.'}
        </div>
        {hasPosition && (
          <div className="mt-2 flex flex-wrap gap-2">
            {positions.map((p, i) => (
              <span key={i} className="px-2 py-0.5 bg-amber-500/10 border border-amber-500/30 rounded text-[11px] font-mono text-amber-300">
                {p.symbol ?? `POS-${i+1}`} ({p.direction ?? '?'})
              </span>
            ))}
          </div>
        )}
      </div>

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="QUEUE UNAVAILABLE" detail={err} /> : (
        <Panel>
          <PanelHeader title={`${queue.length} QUEUED SIGNALS`} subtitle="Will execute when gate opens" />
          {queue.length === 0 ? (
            <EmptyBlock label="QUEUE EMPTY" sub="No signals waiting" />
          ) : (
            <>
              <div className="grid grid-cols-[32px_1fr_80px_80px_80px_1fr] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
                <span>#</span><span>Symbol</span><span>Tier</span><span>Score</span><span>Direction</span><span>Added</span>
              </div>
              {queue.map((sig, i) => {
                const score = sig.confidence_score ?? sig.score ?? 0;
                const tier  = score >= 85 ? 'A+' : score >= 70 ? 'A' : 'B';
                return (
                  <div key={sig.id ?? i}
                    className="grid grid-cols-[32px_1fr_80px_80px_80px_1fr] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 items-center">
                    <span className="text-[11px] font-mono text-[#4A5568]">{i + 1}</span>
                    <div>
                      <div className="text-[13px] font-mono font-bold text-[#F5F7FA]">{sig.symbol ?? '—'}</div>
                      {sig.instrument && <div className="text-[10px] text-[#4A5568]">{sig.instrument}</div>}
                    </div>
                    <StatusBadge text={tier} color={TIER_COLOR[tier]} />
                    <span className={`text-[12px] font-mono font-bold ${
                      score >= 85 ? 'text-green-300' : score >= 70 ? 'text-teal-300' : 'text-amber-300'
                    }`}>{typeof score === 'number' ? score.toFixed(1) : score}</span>
                    <StatusBadge text={sig.direction ?? '—'} color={sig.direction === 'LONG' ? 'green' : sig.direction === 'SHORT' ? 'red' : 'gray'} />
                    <span className="text-[10px] font-mono text-[#4A5568]">{sig.queued_at ?? sig.generated_at ?? '—'}</span>
                  </div>
                );
              })}
            </>
          )}
        </Panel>
      )}

      <div className="mt-3 px-1 text-[10px] font-mono text-[#4A5568]">
        Queue order is FIFO. When the position closes and no halt/risk block applies, the first queued signal advances to execution.
        Queued signals expire per strategy TTL — they are not held indefinitely.
      </div>
    </div>
  );
}
