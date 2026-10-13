/**
 * Strike Desk V4.5 — Market Data Pipeline Page
 *
 * Data flow:
 *   /api/v1/market/ticks         → tickState   (5s fast poll, initialLoad/refreshing split)
 *   /pipeline/status             → pipeState   (10s medium poll)
 *   /api/v1/market/instruments   → instruments (once on mount)
 *   /api/v1/market/indicators/X  → indState    (on-demand, isolated)
 *   /api/v1/market/option-chain/X → chainState (on-demand, isolated, separate from indState)
 *
 * Notes:
 *  - No WebSocket: backend exposes NO WS endpoint; all data comes via REST polling.
 *  - TickResponse schema: symbol, ltp, timestamp, volume, open, high, low, close ONLY.
 *    No change/feed_source/instrument_count fields. change = ltp - close (derived).
 *  - Pipeline status provides: monitored_symbols[], stale_symbols[], pcr_stale_symbols[],
 *    consumed_ticks, is_running. This is the authoritative source for instrument count.
 *  - FullIndicatorResponse is nested: rsi.rsi, adx.adx, macd.macd_line, etc.
 *  - Strategy API is NOT called here — out of scope for market data page.
 *  - useSystemStatus (AppShell) also polls ticks at 15s for TopBar. MarketPage adds a
 *    supplemental 5s poll while mounted; both stop independently on unmount/navigation.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import { market as mktApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, EmptyBlock,
  PageHeader, ActionBtn, ErrorBlock, Freshness,
} from '../../components/ui';

const TICK_POLL     = 5_000;   // fast poll for live price table
const PIPE_POLL     = 10_000;  // medium poll for pipeline status / staleness
const STALE_WARN_MS = 8_000;   // warn after 8s without a new tick batch
const STALE_ERR_MS  = 20_000;  // error after 20s

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/**
 * Flatten FullIndicatorResponse nested structure into a display-ready map.
 * The backend response has: ema9, vwap (flat) + rsi{}, adx{}, macd{}, bollinger{},
 * atr{}, supertrend{}, sentiment{} (nested), plus pcr_slope, volume_surge, oi_change_pct.
 */
function flattenIndicators(d) {
  if (!d || typeof d !== 'object') return {};
  const out = {};
  const copy = (prefix, obj) => {
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      Object.entries(obj).forEach(([k, v]) => {
        out[prefix ? `${prefix}.${k}` : k] = v;
      });
    } else {
      out[prefix] = obj;
    }
  };
  // flat top-level fields
  ['ema9','ema20','vwap','pcr_slope','volume_surge','oi_change_pct','ltp','symbol','timestamp'].forEach(k => {
    if (d[k] !== undefined) out[k] = d[k];
  });
  // nested groups
  ['rsi','adx','macd','bollinger','atr','supertrend','sentiment'].forEach(group => {
    if (d[group] && typeof d[group] === 'object') {
      Object.entries(d[group]).forEach(([k, v]) => {
        out[`${group}.${k}`] = v;
      });
    }
  });
  return out;
}

function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'YES' : 'NO';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4);
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// Derive change from ltp and close; TickResponse has no `change` field.
function deriveChange(tick) {
  if (tick.ltp != null && tick.close != null) return tick.ltp - tick.close;
  return null;
}

function pipelineRunning(pipeState) {
  return pipeState.data?.is_running === true;
}

function staleness(pipeState, symbol) {
  return (pipeState.data?.stale_symbols ?? []).includes(symbol);
}

// ──────────────────────────────────────────────────────────────────────────────
// Main component
// ──────────────────────────────────────────────────────────────────────────────

export default function MarketPage() {
  // Separate initialLoad from refreshing so existing data stays visible during re-polls
  const [initialLoad, setInitialLoad] = useState(true);
  const [refreshing,  setRefreshing]  = useState(false);
  const [tickState,   setTickState]   = useState({ data: null, ageMs: null, err: null });
  const [pipeState,   setPipeState]   = useState({ data: null, err: null });
  const [instruments, setInstruments] = useState({ data: null, loading: true, err: null });

  // Instrument list pagination — purely presentational, no API changes
  const [instPage,     setInstPage]     = useState(1);
  const [instPageSize, setInstPageSize] = useState(20);

  // On-demand states — isolated so tabs don't corrupt each other
  const [indState,    setIndState]    = useState({ data: null, loading: false, err: null, symbol: '' });
  const [chainState,  setChainState]  = useState({ data: null, loading: false, err: null, symbol: '' });

  // Input fields — separate per tab
  const [indSymbol,   setIndSymbol]   = useState('');
  const [chainSymbol, setChainSymbol] = useState('');

  const [tab, setTab] = useState('feed');

  // Request sequence numbers prevent stale responses overwriting newer data
  const tickSeq  = useRef(0);
  const pipeSeq  = useRef(0);

  // Clamp page when instrument list length changes (e.g. after initial load)
  useEffect(() => {
    const total = instruments.data?.length ?? 0;
    const totalPages = Math.max(1, Math.ceil(total / instPageSize));
    if (instPage > totalPages) setInstPage(totalPages);
  }, [instruments.data?.length, instPageSize]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Tick poll ────────────────────────────────────────────────────────────────
  const fetchTicks = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);
    const seq = ++tickSeq.current;

    const [rTicks] = await Promise.allSettled([mktApi.ticks()]);

    if (seq !== tickSeq.current) return; // superseded by a newer request

    if (rTicks.status === 'fulfilled') {
      const raw = rTicks.value.data;
      const arr = Array.isArray(raw) ? raw : [];
      setTickState({ data: arr, ageMs: Date.now(), err: null });
    } else {
      const msg = rTicks.reason?.response?.data?.detail || rTicks.reason?.message || 'Tick fetch failed';
      setTickState(prev => ({ ...prev, err: msg }));
    }

    setInitialLoad(false);
    if (isManual) setRefreshing(false);
  }, []);

  // ── Pipeline status poll ──────────────────────────────────────────────────────
  const fetchPipeline = useCallback(async () => {
    const seq = ++pipeSeq.current;
    try {
      const r = await mktApi.pipelineStatus();
      if (seq !== pipeSeq.current) return;
      setPipeState({ data: r.data ?? null, err: null });
    } catch (e) {
      if (seq !== pipeSeq.current) return;
      const status = e.response?.status;
      // 404/501 = endpoint not yet deployed; don't show as error
      if (status === 404 || status === 501) {
        setPipeState({ data: null, err: null });
      } else {
        setPipeState({ data: null, err: e.response?.data?.detail || e.message });
      }
    }
  }, []);

  // ── Instruments (once on mount) ───────────────────────────────────────────────
  const fetchInstruments = useCallback(async () => {
    setInstruments(prev => ({ ...prev, loading: true }));
    try {
      const r = await mktApi.instruments();
      setInstruments({ data: r.data ?? [], loading: false, err: null });
    } catch (e) {
      const status = e.response?.status;
      if (status === 404 || status === 501) {
        setInstruments({ data: null, loading: false, err: null });
      } else {
        setInstruments({ data: null, loading: false, err: e.response?.data?.detail || e.message });
      }
    }
  }, []);

  // ── Mount / unmount lifecycle ─────────────────────────────────────────────────
  useEffect(() => {
    fetchTicks();
    fetchPipeline();
    fetchInstruments();

    const tickTimer = setInterval(fetchTicks, TICK_POLL);
    const pipeTimer = setInterval(fetchPipeline, PIPE_POLL);

    return () => {
      clearInterval(tickTimer);
      clearInterval(pipeTimer);
    };
  }, [fetchTicks, fetchPipeline, fetchInstruments]);

  // ── On-demand: indicators ────────────────────────────────────────────────────
  const doFetchIndicators = async () => {
    const sym = indSymbol.trim().toUpperCase();
    if (!sym) return;
    setIndState({ data: null, loading: true, err: null, symbol: sym });
    try {
      const r = await mktApi.indicators(sym);
      const flat = flattenIndicators(r.data);
      setIndState({ data: flat, loading: false, err: null, symbol: sym });
    } catch (e) {
      setIndState({ data: null, loading: false, err: e.response?.data?.detail || e.message, symbol: sym });
    }
  };

  // ── On-demand: option chain ──────────────────────────────────────────────────
  const doFetchChain = async () => {
    const sym = chainSymbol.trim().toUpperCase();
    if (!sym) return;
    setChainState({ data: null, loading: true, err: null, symbol: sym });
    try {
      const r = await mktApi.optionChain(sym);
      setChainState({ data: r.data ?? null, loading: false, err: null, symbol: sym });
    } catch (e) {
      setChainState({ data: null, loading: false, err: e.response?.data?.detail || e.message, symbol: sym });
    }
  };

  // ── Derived values ────────────────────────────────────────────────────────────
  const tickArr = tickState.data ?? [];
  const pipeData = pipeState.data;

  // True instrument count: prefer monitored_symbols from pipeline status
  const instrumentCount =
    pipeData?.monitored_symbols?.length ??
    instruments.data?.length ??
    (tickArr.length > 0 ? tickArr.length : null);

  const staleSymbols  = pipeData?.stale_symbols  ?? [];
  const pcrStale      = pipeData?.pcr_stale_symbols ?? [];
  const consumedTicks = pipeData?.consumed_ticks ?? null;
  const isRunning     = pipeData?.is_running ?? null;

  const tabs = ['feed', 'indicators', 'option-chain', 'health'];

  return (
    <div>
      <PageHeader
        title="Market Data Pipeline"
        subtitle="03 — Live feed, pipeline status, indicators, option chain."
        right={
          <ActionBtn onClick={() => fetchTicks(true)} disabled={refreshing || initialLoad}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </ActionBtn>
        }
      />

      {/* Tab bar */}
      <div className="flex gap-1 mb-4 border-b border-[#1E2A3A]">
        {tabs.map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-[11px] font-mono font-bold uppercase tracking-wider border-b-2 transition-colors ${
              tab === t
                ? 'border-amber-400 text-[#F5F7FA]'
                : 'border-transparent text-[#4A5568] hover:text-[#8D98A8]'
            }`}
          >
            {t.replace('-', ' ')}
          </button>
        ))}
      </div>

      {/* ── FEED TAB ──────────────────────────────────────────────────────────── */}
      {tab === 'feed' && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">

            {/* Live Feed summary */}
            <Panel>
              <PanelHeader title="LIVE FEED" />
              {initialLoad ? (
                <LoadingBlock label="Connecting to feed…" />
              ) : (
                <div className="p-4">
                  <KV
                    label="Instruments monitored"
                    value={instrumentCount != null ? instrumentCount : '—'}
                  />
                  <KV
                    label="Active ticks received"
                    value={tickArr.length > 0 ? tickArr.length : '—'}
                  />
                  <KV
                    label="Pipeline running"
                    value={
                      isRunning === null
                        ? <span className="text-[#4A5568]">—</span>
                        : <StatusBadge text={isRunning ? 'YES' : 'NO'} color={isRunning ? 'green' : 'red'} />
                    }
                  />
                  <KV
                    label="Ticks consumed (total)"
                    value={consumedTicks != null ? consumedTicks.toLocaleString() : '—'}
                  />
                  <KV
                    label="Data age"
                    value={<Freshness ageMs={tickState.ageMs} warnMs={STALE_WARN_MS} errorMs={STALE_ERR_MS} />}
                    mono={false}
                  />
                  <KV
                    label="Stale symbols"
                    value={
                      staleSymbols.length === 0
                        ? <span className="text-green-400 font-mono text-[11px]">None</span>
                        : <span className="text-amber-300 font-mono text-[11px]">{staleSymbols.length} ({staleSymbols.slice(0,3).join(', ')}{staleSymbols.length > 3 ? '…' : ''})</span>
                    }
                    mono={false}
                  />
                </div>
              )}
              {tickState.err && (
                <div className="mx-4 mb-4 px-3 py-2 bg-red-950/20 border border-red-800/30 rounded text-[11px] font-mono text-red-400">
                  {tickState.err}
                </div>
              )}
              {refreshing && !initialLoad && (
                <div className="px-4 pb-2 text-[10px] font-mono text-[#4A5568] animate-pulse">
                  re-polling feed…
                </div>
              )}
            </Panel>

            {/* Option chain + PCR status */}
            <Panel>
              <PanelHeader title="PCR / OPTION CHAIN" />
              <div className="p-4">
                <KV
                  label="PCR stale underlyings"
                  value={
                    pipeData === null
                      ? <span className="text-[#4A5568]">—</span>
                      : pcrStale.length === 0
                      ? <span className="text-green-400 font-mono text-[11px]">None</span>
                      : <span className="text-amber-300 font-mono text-[11px]">{pcrStale.slice(0,3).join(', ')}{pcrStale.length > 3 ? `…+${pcrStale.length - 3}` : ''}</span>
                  }
                  mono={false}
                />
                <KV
                  label="PCR gate"
                  value="On stale PCR → option signals disabled"
                  valueColor="text-[#8D98A8]"
                />
                <KV
                  label="Option chain source"
                  value="Dhan REST API"
                />
                <KV
                  label="Poll throttle"
                  value="3s per underlying"
                />
                <KV
                  label="Pipeline status source"
                  value={pipeState.err ? <span className="text-red-400">Unavailable</span> : (pipeData ? 'Connected' : '—')}
                  mono={false}
                />
              </div>
            </Panel>

            {/* Pipeline health — derived, not hardcoded */}
            <Panel>
              <PanelHeader title="PIPELINE HEALTH" subtitle="Derived from backend state" />
              <div className="p-4">
                {(() => {
                  const ageMs = tickState.ageMs;
                  const dataAge = ageMs ? Date.now() - ageMs : null;
                  const ticksFresh  = dataAge != null && dataAge < STALE_WARN_MS;
                  const ticksStale  = dataAge != null && dataAge >= STALE_WARN_MS && dataAge < STALE_ERR_MS;
                  const ticksError  = dataAge != null && dataAge >= STALE_ERR_MS;

                  const pipeOk    = isRunning === true;
                  const pipeOff   = isRunning === false;
                  const pipeUnk   = isRunning === null;

                  const staleCount = staleSymbols.length;
                  const pcrStaleCount = pcrStale.length;

                  const steps = [
                    {
                      label: 'Dhan REST Feed',
                      status: ticksError  ? 'ERROR'   :
                              ticksStale  ? 'STALE'   :
                              ticksFresh  ? 'LIVE'    : 'UNKNOWN',
                      note: dataAge != null ? `${Math.round(dataAge / 1000)}s old` : null,
                    },
                    {
                      label: 'Pipeline Orchestrator',
                      status: pipeOff   ? 'STOPPED' :
                              pipeOk    ? 'LIVE'    :
                              pipeUnk   ? 'UNKNOWN' : 'UNKNOWN',
                      note: null,
                    },
                    {
                      label: 'Tick Store',
                      status: ticksFresh && pipeOk ? 'LIVE'    :
                              ticksStale            ? 'STALE'   :
                              ticksError            ? 'ERROR'   : 'UNKNOWN',
                      note: staleCount > 0 ? `${staleCount} stale` : null,
                    },
                    {
                      label: 'Indicators',
                      status: ticksFresh && pipeOk ? 'LIVE'    :
                              pipeOff               ? 'STOPPED' :
                              pipeUnk && ticksFresh ? 'LIVE'    : 'UNKNOWN',
                      note: null,
                    },
                    {
                      label: 'Option Chain',
                      status: pipeOff ? 'STOPPED' :
                              pipeOk  ? 'LIVE'    : 'UNKNOWN',
                      note: null,
                    },
                    {
                      label: 'PCR / OI',
                      status: pcrStaleCount > 0 ? 'STALE'   :
                              pipeOff            ? 'STOPPED' :
                              pipeOk             ? 'LIVE'    : 'UNKNOWN',
                      note: pcrStaleCount > 0 ? `${pcrStaleCount} stale` : null,
                    },
                  ];

                  return steps.map(({ label, status, note }) => (
                    <div key={label} className="flex items-center justify-between py-1.5 border-b border-[#1E2A3A] last:border-0">
                      <span className="text-[12px] text-[#8D98A8]">{label}</span>
                      <div className="flex items-center gap-2">
                        {note && <span className="text-[9px] font-mono text-[#4A5568]">{note}</span>}
                        <StatusBadge
                          text={status}
                          color={
                            status === 'LIVE'    ? 'green' :
                            status === 'STALE'   ? 'amber' :
                            status === 'STOPPED' ? 'red'   :
                            status === 'ERROR'   ? 'red'   : 'gray'
                          }
                        />
                      </div>
                    </div>
                  ));
                })()}
              </div>
              <div className="px-4 pb-3 text-[9px] font-mono text-[#2A3F5A]">
                Data source: /api/v1/market/ticks + /pipeline/status
              </div>
            </Panel>
          </div>

          {/* Tick price table */}
          {initialLoad ? (
            <LoadingBlock label="Loading tick feed…" />
          ) : tickState.err && tickArr.length === 0 ? (
            <ErrorBlock label="TICK FEED UNAVAILABLE" detail={tickState.err} />
          ) : tickArr.length === 0 ? (
            <EmptyBlock label="No ticks received — pipeline may be idle or market closed." />
          ) : (
            <Panel>
              <PanelHeader
                title={`TICK FEED — ${tickArr.length} symbols`}
                subtitle={pipeData ? `${instrumentCount ?? '?'} monitored, ${staleSymbols.length} stale` : undefined}
              />
              <div className="grid grid-cols-[1fr_90px_90px_80px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
                <span>Symbol</span>
                <span className="text-right">LTP</span>
                <span className="text-right">Change</span>
                <span className="text-right">Volume</span>
                <span className="text-right">Age</span>
              </div>
              {tickArr.slice(0, 50).map((tick, i) => {
                const chg = deriveChange(tick);
                const isStale = staleSymbols.includes(tick.symbol);
                const tickTs = tick.timestamp ? new Date(tick.timestamp).getTime() : tickState.ageMs;
                return (
                  <div
                    key={tick.symbol ?? i}
                    className={`grid grid-cols-[1fr_90px_90px_80px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 text-[11px] font-mono ${isStale ? 'opacity-60' : ''}`}
                  >
                    <span className={isStale ? 'text-amber-400' : 'text-[#F5F7FA]'}>
                      {tick.symbol}
                      {isStale && <span className="ml-1 text-[9px] text-amber-500">STALE</span>}
                    </span>
                    <span className="text-right text-[#F5F7FA]">
                      {tick.ltp != null ? tick.ltp.toFixed(2) : '—'}
                    </span>
                    <span className={`text-right ${chg == null ? 'text-[#4A5568]' : chg >= 0 ? 'text-green-300' : 'text-red-400'}`}>
                      {chg == null ? '—' : `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}`}
                    </span>
                    <span className="text-right text-[#8D98A8]">
                      {tick.volume != null ? tick.volume.toLocaleString() : '—'}
                    </span>
                    <span className="text-right">
                      <Freshness ageMs={tickTs} warnMs={STALE_WARN_MS} errorMs={STALE_ERR_MS} />
                    </span>
                  </div>
                );
              })}
              {tickArr.length > 50 && (
                <div className="px-4 py-2 text-[10px] font-mono text-[#4A5568]">
                  + {tickArr.length - 50} more symbols not shown
                </div>
              )}
            </Panel>
          )}
        </div>
      )}

      {/* ── INDICATORS TAB ──────────────────────────────────────────────────────── */}
      {tab === 'indicators' && (
        <div className="space-y-4">
          <Panel>
            <PanelHeader title="SYMBOL INDICATORS" subtitle="Fetches FullIndicatorResponse from backend" />
            <div className="p-4 flex gap-2">
              <input
                value={indSymbol}
                onChange={e => setIndSymbol(e.target.value)}
                placeholder="Enter symbol (e.g. RELIANCE)"
                className="flex-1 bg-[#0B0F14] border border-[#2A3F5A] rounded px-3 py-1.5 text-[12px] font-mono text-[#F5F7FA] placeholder-[#4A5568] focus:outline-none focus:border-amber-500/60"
                onKeyDown={e => e.key === 'Enter' && doFetchIndicators()}
              />
              <ActionBtn onClick={doFetchIndicators} disabled={indState.loading || !indSymbol.trim()}>
                {indState.loading ? '…' : 'Fetch'}
              </ActionBtn>
            </div>
          </Panel>

          {indState.loading && <LoadingBlock label="Fetching indicators…" />}

          {indState.err && (
            <ErrorBlock label={`INDICATORS UNAVAILABLE — ${indState.symbol}`} detail={indState.err} />
          )}

          {indState.data && !indState.loading && !indState.err && (
            <Panel>
              <PanelHeader title={`INDICATORS — ${indState.symbol}`} />
              <div className="p-4">
                <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-2">
                  {Object.entries(indState.data).filter(([k]) =>
                    !['symbol','timestamp'].includes(k)
                  ).map(([key, val]) => (
                    <div key={key} className="bg-[#151C25] border border-[#1E2A3A] rounded p-2.5">
                      <div className="text-[9px] text-[#4A5568] uppercase tracking-wider mb-1">
                        {key.replace(/[_.]/g, ' ')}
                      </div>
                      <div className={`text-[12px] font-bold font-mono ${
                        typeof val === 'number' ? (val < 0 ? 'text-red-400' : 'text-green-300') : 'text-[#F5F7FA]'
                      }`}>
                        {fmt(val)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </Panel>
          )}
        </div>
      )}

      {/* ── OPTION CHAIN TAB ────────────────────────────────────────────────────── */}
      {tab === 'option-chain' && (
        <div className="space-y-4">
          <Panel>
            <PanelHeader title="OPTION CHAIN" subtitle="Dhan REST API — PCR must be fresher than 30s for signals" />
            <div className="p-4">
              <div className="p-3 bg-amber-950/20 border border-amber-800/30 rounded text-[11px] font-mono text-amber-300 mb-4">
                PCR stale underlyings: {pcrStale.length === 0 ? 'None' : pcrStale.join(', ')} — option signals disabled for stale underlyings.
              </div>
              <div className="flex gap-2">
                <input
                  value={chainSymbol}
                  onChange={e => setChainSymbol(e.target.value)}
                  placeholder="Enter underlying (e.g. RELIANCE)"
                  className="flex-1 bg-[#0B0F14] border border-[#2A3F5A] rounded px-3 py-1.5 text-[12px] font-mono text-[#F5F7FA] placeholder-[#4A5568] focus:outline-none focus:border-amber-500/60"
                  onKeyDown={e => e.key === 'Enter' && doFetchChain()}
                />
                <ActionBtn onClick={doFetchChain} disabled={chainState.loading || !chainSymbol.trim()}>
                  {chainState.loading ? '…' : 'Fetch Chain'}
                </ActionBtn>
              </div>
            </div>
          </Panel>

          {chainState.loading && <LoadingBlock label="Fetching option chain…" />}

          {chainState.err && (
            <ErrorBlock label={`OPTION CHAIN UNAVAILABLE — ${chainState.symbol}`} detail={chainState.err} />
          )}

          {chainState.data && !chainState.loading && !chainState.err && (() => {
            const c = chainState.data;
            return (
              <Panel>
                <PanelHeader
                  title={`OPTION CHAIN — ${chainState.symbol}`}
                  subtitle={`Expiry: ${c.expiry ?? '—'}  |  Spot: ${c.spot_price != null ? c.spot_price.toFixed(2) : '—'}  |  ATM: ${c.atm_strike ?? '—'}`}
                />
                <div className="p-4 space-y-1">
                  <KV label="Underlying"   value={c.underlying ?? chainState.symbol} />
                  <KV label="Spot price"   value={c.spot_price != null ? c.spot_price.toFixed(2) : '—'} />
                  <KV label="ATM strike"   value={c.atm_strike ?? '—'} />
                  <KV label="Expiry"       value={c.expiry ?? '—'} />
                  <KV label="Timestamp"    value={c.timestamp ?? '—'} />
                  <KV label="Strikes"      value={Array.isArray(c.strikes) ? `${c.strikes.length} strikes` : '—'} />
                </div>

                {Array.isArray(c.strikes) && c.strikes.length > 0 && (
                  <div className="overflow-x-auto">
                    <div className="grid grid-cols-[80px_70px_70px_80px_80px_70px_70px] gap-1 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase text-[#4A5568]">
                      <span>Strike</span>
                      <span className="text-right">Call OI</span>
                      <span className="text-right">Call LTP</span>
                      <span className="text-right">PCR</span>
                      <span className="text-right">Put LTP</span>
                      <span className="text-right">Put OI</span>
                      <span className="text-right">ATM</span>
                    </div>
                    {c.strikes.slice(0, 20).map((s, i) => (
                      <div
                        key={s.strike ?? i}
                        className={`grid grid-cols-[80px_70px_70px_80px_80px_70px_70px] gap-1 px-4 py-2 border-b border-[#1E2A3A]/40 text-[11px] font-mono hover:bg-[#111821]/50 ${
                          s.is_atm ? 'bg-amber-950/20 border-amber-800/20' : ''
                        }`}
                      >
                        <span className={s.is_atm ? 'text-amber-300 font-bold' : 'text-[#F5F7FA]'}>
                          {s.strike ?? '—'}
                        </span>
                        <span className="text-right text-green-300">{s.call_oi ?? '—'}</span>
                        <span className="text-right text-green-300">{s.call_ltp != null ? s.call_ltp.toFixed(2) : '—'}</span>
                        <span className="text-right text-[#F5F7FA]">{s.pcr != null ? s.pcr.toFixed(2) : '—'}</span>
                        <span className="text-right text-red-400">{s.put_ltp != null ? s.put_ltp.toFixed(2) : '—'}</span>
                        <span className="text-right text-red-400">{s.put_oi ?? '—'}</span>
                        <span className="text-right">{s.is_atm ? <StatusBadge text="ATM" color="amber" /> : ''}</span>
                      </div>
                    ))}
                    {c.strikes.length > 20 && (
                      <div className="px-4 py-2 text-[10px] font-mono text-[#4A5568]">
                        + {c.strikes.length - 20} more strikes not shown
                      </div>
                    )}
                  </div>
                )}
              </Panel>
            );
          })()}
        </div>
      )}

      {/* ── HEALTH TAB ──────────────────────────────────────────────────────────── */}
      {tab === 'health' && (
        <div className="space-y-4">
          <Panel>
            <PanelHeader title="DATA PIPELINE VISUALIZATION" subtitle="Real state from /pipeline/status and tick freshness" />
            <div className="p-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-8">

                {/* Tick pipeline */}
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-3">TICK PIPELINE</div>
                  {(() => {
                    const ageMs = tickState.ageMs;
                    const dataAge = ageMs ? Date.now() - ageMs : null;
                    const ticksFresh = dataAge != null && dataAge < STALE_WARN_MS;
                    const ticksStale = dataAge != null && dataAge >= STALE_WARN_MS;
                    const pipeOk = isRunning === true;

                    const steps = [
                      {
                        label: 'Dhan REST Feed',
                        status: !ageMs ? 'UNKNOWN' : ticksStale ? 'STALE' : 'LIVE',
                        color: !ageMs ? 'bg-gray-500' : ticksStale ? 'bg-amber-400' : 'bg-green-400',
                      },
                      {
                        label: 'Pipeline Orchestrator',
                        status: isRunning === null ? 'UNKNOWN' : isRunning ? 'LIVE' : 'STOPPED',
                        color: isRunning === null ? 'bg-gray-500' : isRunning ? 'bg-green-400' : 'bg-red-400',
                      },
                      {
                        label: 'Tick Store',
                        status: ticksFresh && pipeOk ? 'LIVE' : ticksStale ? 'STALE' : 'UNKNOWN',
                        color: ticksFresh && pipeOk ? 'bg-green-400' : ticksStale ? 'bg-amber-400' : 'bg-gray-500',
                      },
                      {
                        label: 'Indicators',
                        status: ticksFresh ? 'LIVE' : 'UNKNOWN',
                        color: ticksFresh ? 'bg-green-400' : 'bg-gray-500',
                      },
                    ];

                    return steps.map((s, i, arr) => (
                      <div key={s.label} className="flex flex-col items-start">
                        <div className="flex items-center gap-2 px-3 py-2 bg-[#151C25] border border-[#1E2A3A] rounded w-full">
                          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${s.color}`} />
                          <span className="text-[12px] text-[#F5F7FA] flex-1">{s.label}</span>
                          <StatusBadge
                            text={s.status}
                            color={s.status === 'LIVE' ? 'green' : s.status === 'STALE' ? 'amber' : s.status === 'STOPPED' ? 'red' : 'gray'}
                            className="ml-auto"
                          />
                        </div>
                        {i < arr.length - 1 && <div className="ml-4 text-[#4A5568] text-[18px] my-0.5">↓</div>}
                      </div>
                    ));
                  })()}
                </div>

                {/* Option chain pipeline */}
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-3">OPTION CHAIN PIPELINE</div>
                  {(() => {
                    const pipeOk = isRunning === true;
                    const pcrOk  = pcrStale.length === 0;

                    const steps = [
                      {
                        label: 'Dhan Option Chain API',
                        status: isRunning === null ? 'UNKNOWN' : pipeOk ? 'LIVE' : 'STOPPED',
                        color: isRunning === null ? 'bg-gray-500' : pipeOk ? 'bg-teal-400' : 'bg-red-400',
                      },
                      {
                        label: 'PCR / OI',
                        status: pipeOk && pcrOk ? 'LIVE' : pipeOk && !pcrOk ? 'STALE' : 'UNKNOWN',
                        color: pipeOk && pcrOk ? 'bg-teal-400' : pipeOk ? 'bg-amber-400' : 'bg-gray-500',
                      },
                      {
                        label: 'Strategy Gate',
                        status: pipeOk && pcrOk ? 'OPEN' : pipeOk && !pcrOk ? 'GATED' : 'UNKNOWN',
                        color: pipeOk && pcrOk ? 'bg-teal-400' : pipeOk ? 'bg-amber-400' : 'bg-gray-500',
                      },
                    ];

                    return steps.map((s, i, arr) => (
                      <div key={s.label} className="flex flex-col items-start">
                        <div className="flex items-center gap-2 px-3 py-2 bg-[#151C25] border border-[#1E2A3A] rounded w-full">
                          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${s.color}`} />
                          <span className="text-[12px] text-[#F5F7FA] flex-1">{s.label}</span>
                          <StatusBadge
                            text={s.status}
                            color={
                              s.status === 'LIVE' || s.status === 'OPEN' ? 'green' :
                              s.status === 'STALE' || s.status === 'GATED' ? 'amber' :
                              s.status === 'STOPPED' ? 'red' : 'gray'
                            }
                            className="ml-auto"
                          />
                        </div>
                        {i < arr.length - 1 && <div className="ml-4 text-[#4A5568] text-[18px] my-0.5">↓</div>}
                      </div>
                    ));
                  })()}
                </div>

              </div>
            </div>

            {/* Pipeline raw status */}
            {pipeData && (
              <div className="border-t border-[#1E2A3A] p-4">
                <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-3">
                  RAW PIPELINE STATUS
                </div>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-8">
                  <KV label="is_running"      value={String(pipeData.is_running ?? '—')} />
                  <KV label="is_paused"       value={String(pipeData.is_paused ?? '—')} />
                  <KV label="consumed_ticks"  value={pipeData.consumed_ticks?.toLocaleString() ?? '—'} />
                  <KV label="stale_count"     value={pipeData.stale_count ?? '—'} />
                  <KV label="pipeline_start"  value={pipeData.pipeline_start_time ?? '—'} />
                  <KV label="last_save"       value={pipeData.last_save_time ?? '—'} />
                </div>
              </div>
            )}
            {pipeState.err && (
              <div className="m-4 px-3 py-2 bg-red-950/20 border border-red-800/30 rounded text-[11px] font-mono text-red-400">
                /pipeline/status unavailable: {pipeState.err}
              </div>
            )}
            {!pipeData && !pipeState.err && (
              <div className="px-4 pb-3 text-[10px] font-mono text-[#4A5568]">
                Pipeline status not available — health derived from tick freshness only.
              </div>
            )}
          </Panel>

          {/* Instruments list */}
          {instruments.loading ? (
            <LoadingBlock label="Loading instruments…" />
          ) : instruments.err ? (
            <ErrorBlock label="INSTRUMENTS UNAVAILABLE" detail={instruments.err} />
          ) : instruments.data && instruments.data.length > 0 ? (
            <Panel>
              <PanelHeader title={`INSTRUMENTS (${instruments.data.length})`} subtitle="From /api/v1/market/instruments" />
              <div className="grid grid-cols-[1fr_100px_120px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase text-[#4A5568]">
                <span>Symbol</span>
                <span>Exchange</span>
                <span>Type</span>
                <span className="text-right">Lot Size</span>
              </div>
              {(() => {
                const total      = instruments.data.length;
                const totalPages = Math.max(1, Math.ceil(total / instPageSize));
                const start      = (instPage - 1) * instPageSize;
                const end        = Math.min(start + instPageSize, total);
                const page       = instruments.data.slice(start, end);
                return (
                  <>
                    {page.map((inst, i) => (
                      <div key={inst.symbol ?? (start + i)} className="grid grid-cols-[1fr_100px_120px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 text-[11px] font-mono">
                        <span className="text-[#F5F7FA]">{inst.symbol ?? '—'}</span>
                        <span className="text-[#8D98A8]">{inst.exchange ?? '—'}</span>
                        <span className="text-[#8D98A8]">{inst.instrument_type ?? inst.type ?? '—'}</span>
                        <span className="text-right text-[#8D98A8]">{inst.lot_size ?? '—'}</span>
                      </div>
                    ))}
                    <div className="flex items-center justify-between px-4 py-2 border-t border-[#1E2A3A] text-[10px] font-mono text-[#4A5568]">
                      <span>Showing {start + 1}–{end} of {total}</span>
                      <div className="flex items-center gap-3">
                        <select
                          value={instPageSize}
                          onChange={e => { setInstPageSize(Number(e.target.value)); setInstPage(1); }}
                          className="bg-[#0D1520] border border-[#1E2A3A] text-[#8D98A8] text-[10px] font-mono px-1 py-0.5 rounded"
                        >
                          <option value={20}>20 / page</option>
                          <option value={50}>50 / page</option>
                          <option value={100}>100 / page</option>
                        </select>
                        <button
                          onClick={() => setInstPage(p => Math.max(1, p - 1))}
                          disabled={instPage <= 1}
                          className="px-2 py-0.5 border border-[#1E2A3A] rounded disabled:opacity-30 hover:border-[#2D3F55] text-[#8D98A8]"
                        >Prev</button>
                        <span className="text-[#6B7A8D]">Page {instPage} of {totalPages}</span>
                        <button
                          onClick={() => setInstPage(p => Math.min(totalPages, p + 1))}
                          disabled={instPage >= totalPages}
                          className="px-2 py-0.5 border border-[#1E2A3A] rounded disabled:opacity-30 hover:border-[#2D3F55] text-[#8D98A8]"
                        >Next</button>
                      </div>
                    </div>
                  </>
                );
              })()}
            </Panel>
          ) : null}
        </div>
      )}
    </div>
  );
}
