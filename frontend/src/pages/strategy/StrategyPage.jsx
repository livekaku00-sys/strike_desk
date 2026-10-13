/**
 * Strike Desk V4.5 — Strategy Engine Page
 *
 * Data flow:
 *   GET /api/v1/trading/mode      → { active_mode }                  (30s poll)
 *   GET /api/v1/trading/signals   → Signal[]                         (10s poll)
 *   GET /api/v1/trading/watchlist → { count, entries: WatchlistEntry[] } (60s poll)
 *
 * Signal.to_dict() shape:
 *   signal_id, symbol, asset_class, signal_type, direction (LONG|SHORT),
 *   entry_price, target_price, stop_loss_price, confidence_score,
 *   confidence_tier (A+|A|B|C), checklist[], timestamp, expires_at,
 *   strategy_name, metadata, indicators_triggered, state, category,
 *   status (APPROVED_FOR_EXECUTION | ALERT_ONLY)
 *
 * Scoring model: EMA9(30) + PCR(20) + VWAP(20) + Sentiment(15) + Volume(15) = 100
 * The signal may score above 100 component sum due to bonus indicators (MACD/RSI/Supertrend).
 * confidence_score is always authoritative.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import { strategy as stratApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, EmptyBlock,
  PageHeader, ActionBtn, ErrorBlock, Freshness,
} from '../../components/ui';

// ── Static config ─────────────────────────────────────────────────────────────

const MODES = [
  { id: 'AUTO_STOCK_OPTIONS', label: 'Stock Options',   sub: 'Individual equity CE/PE', available: true,  broker: 'Dhan (F&O)' },
  { id: 'AUTO_OPTIONS',       label: 'Index Options',   sub: 'NIFTY, BANKNIFTY, SENSEX', available: false, broker: 'Dhan (F&O)' },
  { id: 'AUTO_FUTURES',       label: 'Index Futures',   sub: 'NIFTY-FUT, BANKNIFTY-FUT', available: false, broker: 'Dhan (F&O)' },
  { id: 'AUTO_STOCK_FUTURES', label: 'Stock Futures',   sub: 'Individual equity FUT',    available: false, broker: 'Dhan (F&O)' },
  { id: 'AUTO_CASH_INTRADAY', label: 'Cash Intraday',   sub: 'Equity MIS intraday',      available: false, broker: 'Dhan' },
  { id: 'AUTO_CASH_SWING',    label: 'Cash Swing',      sub: '2–10 day holds',           available: false, broker: 'Dhan' },
  { id: 'AUTO_CASH_DELIVERY', label: 'Cash Delivery',   sub: 'CNC equity delivery',      available: false, broker: 'Dhan' },
];

// Core scoring criteria — weights match backend StockOptionsStrategy.CRITERIA_WEIGHTS
const SCORE_CRITERIA = [
  { label: 'EMA9 Crossover',      key: 'ema9',         pts: 30, color: 'bg-green-500' },
  { label: 'PCR Slope',           key: 'pcr_slope',    pts: 20, color: 'bg-teal-400'  },
  { label: 'VWAP Alignment',      key: 'vwap',         pts: 20, color: 'bg-blue-400'  },
  { label: 'Sentiment Alignment', key: 'sentiment',    pts: 15, color: 'bg-purple-400' },
  { label: 'Volume Surge',        key: 'volume_surge', pts: 15, color: 'bg-amber-400' },
];

const TIERS = [
  { label: 'A+', range: '≥ 85', color: 'green',  action: 'Execute first in queue' },
  { label: 'A',  range: '≥ 70', color: 'blue',   action: 'Execute after A+' },
  { label: 'B',  range: '≥ 55', color: 'amber',  action: 'Execute if no higher tier' },
];

const HARD_GATES = [
  'PCR freshness < 900s for stock options (throttled endpoint)',
  'Option liquidity ≥ 1,000 contracts',
  'Score ≥ 55 minimum threshold',
  'Direction: CE = EMA9 bullish cross + PCR slope < 0',
  'Direction: PE = EMA9 bearish cross + PCR slope > 0',
  'Market hours (09:15–15:10 IST weekdays)',
  'Live market data freshness (underlying tick ≤ 60s)',
  'Risk state clear (no emergency halt)',
  'Broker authenticated and token valid',
];

// Poll intervals
const MODE_POLL    = 30_000;
const SIGNAL_POLL  = 10_000;
const WL_POLL      = 60_000;

// ── Helpers ───────────────────────────────────────────────────────────────────

function tierColor(tier) {
  if (tier === 'A+') return 'green';
  if (tier === 'A')  return 'blue';
  if (tier === 'B')  return 'amber';
  return 'gray';
}

function directionBadge(direction) {
  if (direction === 'LONG')  return { text: 'BUY / CE',  color: 'green' };
  if (direction === 'SHORT') return { text: 'SELL / PE', color: 'red'   };
  return                            { text: direction,   color: 'gray'  };
}

function fmtPrice(v) {
  if (v == null) return '—';
  return `₹${Number(v).toFixed(2)}`;
}

function fmtPct(entry, current) {
  if (entry == null || current == null || entry === 0) return null;
  const pct = ((current - entry) / entry * 100).toFixed(1);
  return pct >= 0 ? `+${pct}%` : `${pct}%`;
}

// ── Score Breakdown ───────────────────────────────────────────────────────────

function ScoreBreakdown({ signal }) {
  if (!signal) {
    // Static reference when no live signal
    return (
      <div className="space-y-3">
        {SCORE_CRITERIA.map(s => (
          <div key={s.key} className="flex items-center gap-3">
            <span className="text-[12px] text-[#8D98A8] min-w-[185px]">{s.label}</span>
            <div className="flex-1 h-1.5 bg-[#1E2A3A] rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${s.color} opacity-30`} style={{ width: `${s.pts}%` }} />
            </div>
            <span className="font-mono text-[12px] text-[#4A5568] min-w-[60px] text-right">— / {s.pts}</span>
          </div>
        ))}
        <div className="pt-1 border-t border-[#1E2A3A] flex justify-between items-center">
          <span className="text-[11px] text-[#4A5568] font-mono">Total</span>
          <span className="text-[12px] text-[#4A5568] font-mono">— / 100</span>
        </div>
        <div className="text-[10px] text-[#4A5568] font-mono">No active signal — bars show maximum possible pts</div>
      </div>
    );
  }

  const triggered = signal.indicators_triggered ?? {};
  const baseSum = SCORE_CRITERIA.reduce((acc, c) => acc + (triggered[c.key] ? c.pts : 0), 0);
  const totalScore = signal.confidence_score ?? 0;
  const hasBonus = totalScore > baseSum;

  return (
    <div className="space-y-3">
      {SCORE_CRITERIA.map(s => {
        const fired = triggered[s.key] === true;
        return (
          <div key={s.key} className="flex items-center gap-3">
            <span className={`text-[12px] min-w-[185px] ${fired ? 'text-[#F5F7FA]' : 'text-[#4A5568]'}`}>{s.label}</span>
            <div className="flex-1 h-1.5 bg-[#1E2A3A] rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${fired ? s.color : 'bg-[#2A3F5A]'}`}
                   style={{ width: fired ? `${s.pts}%` : '0%' }} />
            </div>
            <span className={`font-mono text-[12px] min-w-[60px] text-right ${fired ? 'text-[#F5F7FA]' : 'text-[#4A5568]'}`}>
              {fired ? s.pts : 0} / {s.pts}
            </span>
          </div>
        );
      })}
      <div className="pt-1 border-t border-[#1E2A3A] flex justify-between items-center">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-[#8D98A8] font-mono">Core score</span>
          <span className="text-[11px] text-[#4A5568] font-mono">{baseSum}/100</span>
          {hasBonus && (
            <span className="text-[10px] text-[#4A5568] font-mono">
              + bonus indicators (MACD/RSI/Supertrend) → total {Math.round(totalScore)}
            </span>
          )}
        </div>
        <span className={`font-mono text-[14px] font-bold ${
          totalScore >= 85 ? 'text-green-300' :
          totalScore >= 70 ? 'text-blue-300' :
          totalScore >= 55 ? 'text-amber-300' : 'text-[#4A5568]'
        }`}>{Math.round(totalScore)} / 100</span>
      </div>
    </div>
  );
}

// ── Signal Card ───────────────────────────────────────────────────────────────

function SignalCard({ signal, activeMode }) {
  const dir = directionBadge(signal.direction);
  const tier = signal.confidence_tier ?? '?';
  const isExec = signal.status === 'APPROVED_FOR_EXECUTION' && signal.category === activeMode;
  const meta = signal.metadata ?? {};

  return (
    <div className={`bg-[#111821] border rounded-lg overflow-hidden ${
      isExec ? 'border-green-700/40' : 'border-[#1E2A3A]'
    }`}>
      {/* Header */}
      <div className="px-4 py-3 border-b border-[#1E2A3A] flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px] font-mono font-bold text-[#F5F7FA]">{signal.symbol}</span>
            <StatusBadge text={dir.text} color={dir.color} />
            <StatusBadge text={`Tier ${tier}`} color={tierColor(tier)} />
            {isExec
              ? <StatusBadge text="APPROVED" color="green" />
              : <StatusBadge text="ALERT ONLY" color="gray" />
            }
          </div>
          {meta.underlying && (
            <div className="text-[10px] text-[#4A5568] mt-0.5 font-mono">
              Underlying: {meta.underlying} @ {fmtPrice(meta.underlying_price)}
              {meta.option_type && ` · ${meta.option_type} · Strike ${meta.selected_strike}`}
            </div>
          )}
        </div>
        <div className={`text-[22px] font-mono font-bold flex-shrink-0 ${
          (signal.confidence_score ?? 0) >= 85 ? 'text-green-300' :
          (signal.confidence_score ?? 0) >= 70 ? 'text-blue-300' : 'text-amber-300'
        }`}>{Math.round(signal.confidence_score ?? 0)}</div>
      </div>

      {/* Price levels */}
      <div className="px-4 py-3 grid grid-cols-3 gap-3 border-b border-[#1E2A3A]">
        <div>
          <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-0.5">Entry</div>
          <div className="text-[12px] font-mono text-[#F5F7FA]">{fmtPrice(signal.entry_price)}</div>
        </div>
        <div>
          <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-0.5">Target (+35%)</div>
          <div className="text-[12px] font-mono text-green-300">{fmtPrice(signal.target_price)}</div>
        </div>
        <div>
          <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-0.5">Stop Loss (−25%)</div>
          <div className="text-[12px] font-mono text-red-400">{fmtPrice(signal.stop_loss_price)}</div>
        </div>
      </div>

      {/* Meta */}
      <div className="px-4 py-3 text-[10px] font-mono text-[#4A5568] flex flex-wrap gap-x-4">
        <span>{signal.strategy_name ?? 'Strategy'}</span>
        <span>{signal.signal_type}</span>
        {signal.timestamp && (
          <span>Generated: {new Date(signal.timestamp).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST</span>
        )}
        {signal.expires_at && (
          <span>Expires: {new Date(signal.expires_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST</span>
        )}
        {meta.criteria_met_count != null && (
          <span>Criteria: {meta.criteria_met_count}/{meta.total_criteria}</span>
        )}
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function StrategyPage() {
  const { role } = useAuthStore();
  const isOperator = role === 'operator';

  const [activeMode, setActiveMode]   = useState(null);
  const [signals,    setSignals]      = useState([]);
  const [watchlist,  setWatchlist]    = useState({ count: 0, entries: [] });
  const [initialLoad, setInitialLoad] = useState(true);
  const [refreshing,  setRefreshing]  = useState(false);
  const [modeErr,     setModeErr]     = useState(null);
  const [sigErr,      setSigErr]      = useState(null);
  const [ageMs,       setAgeMs]       = useState(null);
  const [switching,   setSwitching]   = useState(false);
  const [switchMsg,   setSwitchMsg]   = useState(null);

  const reqId = useRef(0);

  const fetchAll = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);
    const id = ++reqId.current;

    const [rMode, rSig, rWl] = await Promise.allSettled([
      stratApi.mode(),
      stratApi.signals(),
      stratApi.watchlist(),
    ]);

    if (id !== reqId.current) {
      if (isManual) setRefreshing(false);
      return; // superseded by a newer request
    }

    if (rMode.status === 'fulfilled') {
      const d = rMode.value.data ?? {};
      setActiveMode(d.active_mode ?? d.mode ?? null);
      setModeErr(null);
    } else {
      setModeErr(rMode.reason?.response?.data?.detail || rMode.reason?.message || 'Failed to fetch mode');
    }

    if (rSig.status === 'fulfilled') {
      const raw = rSig.value.data;
      setSignals(Array.isArray(raw) ? raw : []);
      setSigErr(null);
    } else {
      const status = rSig.reason?.response?.status;
      if (status !== 404 && status !== 501) {
        setSigErr(rSig.reason?.response?.data?.detail || rSig.reason?.message || 'Failed to fetch signals');
      }
    }

    if (rWl.status === 'fulfilled') {
      const d = rWl.value.data ?? {};
      setWatchlist({ count: d.count ?? 0, entries: Array.isArray(d.entries) ? d.entries : [] });
    }
    // watchlist failure is non-critical; keep previous state

    setAgeMs(Date.now());
    setInitialLoad(false);
    if (isManual) setRefreshing(false);
  }, []);

  useEffect(() => {
    fetchAll();
    // Signals expire in 5 min; poll at 10s for responsiveness.
    // Mode changes rarely; same interval keeps them in sync.
    const t = setInterval(fetchAll, SIGNAL_POLL);
    return () => clearInterval(t);
  }, [fetchAll]);

  const handleModeSwitch = async (modeId) => {
    if (!isOperator) return;
    const mode = MODES.find(m => m.id === modeId);
    if (!mode?.available) {
      setSwitchMsg({ ok: false, text: `${modeId} is not yet wired in this build. Only AUTO_STOCK_OPTIONS is active.` });
      return;
    }
    setSwitching(true);
    setSwitchMsg(null);
    try {
      const r = await stratApi.setMode(modeId);
      setActiveMode(r.data?.active_mode ?? modeId);
      setSwitchMsg({ ok: true, text: `Mode switched to ${modeId}` });
    } catch(e) {
      setSwitchMsg({ ok: false, text: e.response?.data?.detail || e.message });
    } finally {
      setSwitching(false);
    }
  };

  // Sort signals: APPROVED first, then by score desc
  const sortedSignals = [...signals].sort((a, b) => {
    const aExec = a.status === 'APPROVED_FOR_EXECUTION' ? 1 : 0;
    const bExec = b.status === 'APPROVED_FOR_EXECUTION' ? 1 : 0;
    if (bExec !== aExec) return bExec - aExec;
    return (b.confidence_score ?? 0) - (a.confidence_score ?? 0);
  });

  // Top signal for score breakdown
  const topSignal = sortedSignals[0] ?? null;

  const execSignals = sortedSignals.filter(s => s.status === 'APPROVED_FOR_EXECUTION');
  const alertSignals = sortedSignals.filter(s => s.status !== 'APPROVED_FOR_EXECUTION');

  return (
    <div>
      <PageHeader
        title="Strategy Engine"
        subtitle="04 — Trading modes, live signals, scoring, confidence tiers, hard gates."
        right={
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-[#4A5568]">
              <Freshness ageMs={ageMs} warnMs={20_000} errorMs={45_000} />
            </span>
            <ActionBtn onClick={() => fetchAll(true)} disabled={refreshing || initialLoad}>
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </ActionBtn>
          </div>
        }
      />

      {switchMsg && (
        <div className={`mb-4 px-4 py-2 rounded border text-[12px] font-mono ${
          switchMsg.ok ? 'bg-green-950/20 border-green-800/30 text-green-300' : 'bg-red-950/20 border-red-800/40 text-red-300'
        }`}>
          {switchMsg.text}
        </div>
      )}

      {initialLoad ? (
        <LoadingBlock label="Loading strategy state…" />
      ) : (
        <div className="space-y-4">

          {/* ── Row 1: Mode + Tiers ──────────────────────────────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {/* Trading Modes */}
            <div className="lg:col-span-2">
              <Panel>
                <PanelHeader
                  title="TRADING MODES"
                  subtitle={activeMode ? `Active: ${activeMode}` : modeErr ? 'Error fetching mode' : 'Loading…'}
                />
                <div className="p-3 space-y-2">
                  {MODES.map(m => {
                    const isServerActive = activeMode === m.id;
                    return (
                      <div key={m.id} className={`flex items-center gap-3 p-3 rounded border ${
                        isServerActive ? 'bg-amber-500/8 border-amber-500/30' :
                        m.available    ? 'bg-[#151C25] border-[#1E2A3A]' :
                                         'bg-[#0B0F14] border-[#1E2A3A]/50 opacity-60'
                      }`}>
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <span className={`text-[12px] font-mono font-bold ${
                              isServerActive ? 'text-amber-300' : m.available ? 'text-[#F5F7FA]' : 'text-[#4A5568]'
                            }`}>{m.id}</span>
                            {isServerActive && <StatusBadge text="ACTIVE" color="amber" />}
                            {!isServerActive && !m.available && <StatusBadge text="NOT WIRED" color="gray" />}
                          </div>
                          <div className="text-[10px] text-[#4A5568] mt-0.5">{m.sub} · {m.broker}</div>
                        </div>
                        {isOperator && m.available && !isServerActive && (
                          <ActionBtn color="default" onClick={() => handleModeSwitch(m.id)} disabled={switching}>
                            Switch
                          </ActionBtn>
                        )}
                      </div>
                    );
                  })}
                </div>
              </Panel>
            </div>

            {/* Confidence Tiers */}
            <div className="space-y-4">
              <Panel>
                <PanelHeader title="CONFIDENCE TIERS" />
                <div className="p-4 space-y-3">
                  {TIERS.map(t => (
                    <div key={t.label} className={`p-3 rounded border ${
                      t.color === 'green' ? 'bg-green-500/8 border-green-500/30' :
                      t.color === 'blue'  ? 'bg-blue-500/8 border-blue-500/30' :
                                            'bg-amber-500/8 border-amber-500/30'
                    }`}>
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`text-[20px] font-bold font-mono ${
                          t.color === 'green' ? 'text-green-300' : t.color === 'blue' ? 'text-blue-300' : 'text-amber-300'
                        }`}>{t.label}</span>
                        <span className="text-[11px] font-mono text-[#8D98A8]">Score {t.range}</span>
                      </div>
                      <div className="text-[10px] text-[#8D98A8]">{t.action}</div>
                    </div>
                  ))}
                  <div className="p-2 bg-[#151C25] border border-[#1E2A3A] rounded text-[10px] font-mono text-[#4A5568]">
                    Below 55 → NO SIGNAL generated
                  </div>
                </div>
              </Panel>
            </div>
          </div>

          {/* ── Row 2: Live Signals ──────────────────────────────────────── */}
          <Panel>
            <PanelHeader
              title="LIVE SIGNALS"
              subtitle={`${signals.length} active signal${signals.length !== 1 ? 's' : ''} from signal store`}
            />
            <div className="p-4">
              {sigErr ? (
                <ErrorBlock label="SIGNAL FETCH FAILED" detail={sigErr} />
              ) : signals.length === 0 ? (
                <div className="py-6 text-center">
                  <div className="text-[12px] font-mono text-[#4A5568]">No active signals</div>
                  <div className="text-[10px] text-[#4A5568] mt-1">
                    Signals are generated when indicators meet the strategy threshold (score ≥ 55).
                    Hard gates block signals when PCR is stale, market is closed, or risk is engaged.
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  {execSignals.length > 0 && (
                    <>
                      <div className="text-[10px] font-mono text-green-400 uppercase tracking-wider mb-2">
                        Approved for execution — mode: {activeMode}
                      </div>
                      {execSignals.map(s => (
                        <SignalCard key={s.signal_id} signal={s} activeMode={activeMode} />
                      ))}
                    </>
                  )}
                  {alertSignals.length > 0 && (
                    <>
                      <div className="text-[10px] font-mono text-[#4A5568] uppercase tracking-wider mt-3 mb-2">
                        Alert only — different asset class
                      </div>
                      {alertSignals.map(s => (
                        <SignalCard key={s.signal_id} signal={s} activeMode={activeMode} />
                      ))}
                    </>
                  )}
                </div>
              )}
              {refreshing && (
                <div className="text-[10px] font-mono text-[#4A5568] animate-pulse text-center mt-2">
                  re-checking signals…
                </div>
              )}
            </div>
          </Panel>

          {/* ── Row 3: Score Breakdown + Hard Gates ──────────────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-2">
              <Panel>
                <PanelHeader
                  title={topSignal
                    ? `SCORE BREAKDOWN — ${topSignal.symbol}`
                    : 'SCORE BREAKDOWN — AUTO_STOCK_OPTIONS (100 pts max)'}
                  subtitle={topSignal
                    ? `Live signal · Tier ${topSignal.confidence_tier} · ${topSignal.status === 'APPROVED_FOR_EXECUTION' ? 'APPROVED FOR EXECUTION' : 'ALERT ONLY'}`
                    : 'No active signal — showing criteria weights'}
                />
                <div className="p-4">
                  <ScoreBreakdown signal={topSignal} />
                  {topSignal && (
                    <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
                      {[
                        { k: 'Signal state',  v: topSignal.state ?? '—' },
                        { k: 'Exec status',   v: topSignal.status === 'APPROVED_FOR_EXECUTION' ? 'APPROVED' : 'ALERT ONLY' },
                        { k: 'Options SL',    v: '75% of entry (−25%)' },
                        { k: 'PCR gate',      v: '< 900s freshness (stocks)' },
                      ].map(({ k, v }) => (
                        <div key={k} className="bg-[#151C25] border border-[#1E2A3A] rounded p-2.5">
                          <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-1">{k}</div>
                          <div className="text-[11px] font-mono text-[#F5F7FA]">{v}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {!topSignal && (
                    <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
                      {[
                        { k: 'Options SL',     v: '75% of entry (−25%)' },
                        { k: 'Trailing SL',    v: '5% below running high' },
                        { k: 'PCR gate',       v: '< 900s freshness (stocks)' },
                        { k: 'Min liquidity',  v: '≥ 1,000 contracts' },
                      ].map(({ k, v }) => (
                        <div key={k} className="bg-[#151C25] border border-[#1E2A3A] rounded p-2.5">
                          <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-1">{k}</div>
                          <div className="text-[11px] font-mono text-[#F5F7FA]">{v}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </Panel>
            </div>

            <div>
              <Panel>
                <PanelHeader title="HARD GATES" subtitle="Any failure = no signal emitted" />
                <div className="p-4">
                  <div className="mb-2 text-[10px] font-mono text-red-400 font-bold">
                    GATES OVERRIDE SCORE — a 92-point signal is blocked if PCR is stale.
                  </div>
                  {HARD_GATES.map((g, i) => (
                    <div key={i} className="flex items-start gap-2 py-1.5 border-b border-[#1E2A3A] last:border-0 text-[11px]">
                      <span className="text-[#4A5568] flex-shrink-0 mt-0.5">•</span>
                      <span className="text-[#8D98A8]">{g}</span>
                    </div>
                  ))}
                  <div className="mt-3 text-[10px] font-mono text-[#4A5568]">
                    Active signals already passed all gates. No signal = gate blocked or threshold not met.
                  </div>
                </div>
              </Panel>
            </div>
          </div>

          {/* ── Row 4: Watchlist ─────────────────────────────────────────── */}
          {watchlist.entries.length > 0 && (
            <Panel>
              <PanelHeader
                title="TOP CANDIDATES — DAILY WATCHLIST"
                subtitle={`${watchlist.count} symbols ranked by composite score · auto-refreshes each scan`}
              />
              <div className="overflow-x-auto">
                <div className="grid grid-cols-[40px_1fr_60px_50px_60px_50px_1fr] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase text-[#4A5568]">
                  <span>#</span>
                  <span>Symbol</span>
                  <span>Dir</span>
                  <span>Score</span>
                  <span>ADX</span>
                  <span>LTP</span>
                  <span>Reasons</span>
                </div>
                {watchlist.entries.slice(0, 10).map((e) => (
                  <div key={e.rank} className="grid grid-cols-[40px_1fr_60px_50px_60px_50px_1fr] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/40 hover:bg-[#151C25]/50 text-[11px] font-mono">
                    <span className="text-[#4A5568]">{e.rank}</span>
                    <span className="text-[#F5F7FA] font-bold">{e.symbol}</span>
                    <span className={e.direction === 'CE' ? 'text-green-300' : 'text-red-400'}>
                      {e.direction}
                    </span>
                    <span className={
                      e.score >= 80 ? 'text-green-300' :
                      e.score >= 65 ? 'text-amber-300' : 'text-[#8D98A8]'
                    }>{e.score}</span>
                    <span className="text-[#8D98A8]">{e.adx ?? '—'}</span>
                    <span className="text-[#8D98A8]">{e.ltp != null ? `₹${e.ltp}` : '—'}</span>
                    <span className="text-[#4A5568] truncate">{(e.reasons ?? []).join(' · ')}</span>
                  </div>
                ))}
              </div>
            </Panel>
          )}

        </div>
      )}
    </div>
  );
}
