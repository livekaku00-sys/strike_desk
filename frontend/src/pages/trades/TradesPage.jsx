/**
 * Strike Desk V4.5 — Trades & P&L Page
 * PAPER and LIVE clearly separated. Never mixed without distinction.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { trades as tradesApi, execution as execApi } from '../../api/services';
import {
  Panel, PanelHeader, Card, StatusBadge, LoadingBlock, EmptyBlock,
  PageHeader, ActionBtn, ErrorBlock, BackendRequired,
} from '../../components/ui';

const POLL = 10000;

function SummaryCards({ summary, mode }) {
  if (!summary) return null;
  const cards = [
    { label: 'Total Trades', value: summary.total_trades ?? '—' },
    { label: 'Win Rate',     value: summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(1)}%` : '—',
      color: (summary.win_rate ?? 0) >= 0.5 ? 'text-green-300' : 'text-red-400' },
    { label: 'Net P&L',      value: summary.net_pnl != null ? `₹ ${summary.net_pnl.toFixed(2)}` : '—',
      color: (summary.net_pnl ?? 0) >= 0 ? 'text-green-300' : 'text-red-400' },
    { label: 'Gross Profit', value: summary.gross_profit != null ? `₹ ${summary.gross_profit.toFixed(2)}` : '—', color: 'text-green-300' },
    { label: 'Gross Loss',   value: summary.gross_loss != null  ? `₹ ${summary.gross_loss.toFixed(2)}`  : '—', color: 'text-red-400' },
    { label: 'Avg Win',      value: summary.avg_win != null  ? `₹ ${summary.avg_win.toFixed(2)}`  : '—', color: 'text-green-300' },
    { label: 'Avg Loss',     value: summary.avg_loss != null ? `₹ ${summary.avg_loss.toFixed(2)}` : '—', color: 'text-red-400' },
    { label: 'Largest Win',  value: summary.largest_win != null  ? `₹ ${summary.largest_win.toFixed(2)}`  : '—', color: 'text-green-300' },
  ];
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
      {cards.map(c => (
        <div key={c.label} className="bg-[#151C25] border border-[#1E2A3A] rounded p-3">
          <div className="text-[10px] text-[#4A5568] uppercase tracking-wider mb-1">{c.label}</div>
          <div className={`text-[15px] font-bold font-mono ${c.color ?? 'text-[#F5F7FA]'}`}>{c.value}</div>
        </div>
      ))}
    </div>
  );
}

export default function TradesPage() {
  const [tradeList, setTradeList] = useState([]);
  const [summary, setSummary]     = useState(null);
  const [mode, setMode]           = useState(null);
  const [loading, setLoading]     = useState(true);
  const [err, setErr]             = useState(null);

  const [filterMode,      setFilterMode]      = useState('ALL');
  const [filterDirection, setFilterDirection] = useState('ALL');
  const [filterResult,    setFilterResult]    = useState('ALL');

  const fetch = useCallback(async () => {
    try {
      const [rTrades, rMode] = await Promise.allSettled([
        tradesApi.list(),
        execApi.mode(),
      ]);
      if (rTrades.status === 'fulfilled') {
        const arr = rTrades.value.data?.trades ?? rTrades.value.data ?? [];
        setTradeList(Array.isArray(arr) ? arr : []);
      }
      if (rMode.status === 'fulfilled') {
        setMode(rMode.value.data?.mode ?? null);
      }
      // Summary endpoint
      try {
        const rSum = await tradesApi.summary();
        setSummary(rSum.data);
      } catch { setSummary(null); }
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const filtered = tradeList.filter(t => {
    if (filterMode !== 'ALL' && t.execution_mode !== filterMode) return false;
    if (filterDirection !== 'ALL' && t.direction !== filterDirection) return false;
    if (filterResult !== 'ALL') {
      const isWin = (t.pnl ?? 0) >= 0;
      if (filterResult === 'WIN' && !isWin) return false;
      if (filterResult === 'LOSS' && isWin) return false;
    }
    return true;
  });

  const pnlC = (v) => { if (v == null) return ''; return v >= 0 ? 'text-green-300' : 'text-red-400'; };

  return (
    <div>
      <PageHeader
        title="Trades & P&L"
        subtitle="Closed trade journal. PAPER and LIVE are clearly separated."
        right={<ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>}
      />

      <SummaryCards summary={summary} mode={mode} />

      {/* Filters */}
      <div className="flex flex-wrap gap-2 mb-4">
        <div className="flex items-center gap-1">
          <span className="text-[10px] text-[#4A5568] uppercase font-mono">Mode</span>
          {['ALL','PAPER','LIVE'].map(v => (
            <button key={v} onClick={() => setFilterMode(v)}
              className={`px-2 py-1 text-[10px] font-mono font-bold rounded border transition-colors ${
                filterMode === v ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'bg-[#151C25] text-[#4A5568] border-[#1E2A3A] hover:text-[#8D98A8]'
              }`}>{v}</button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <span className="text-[10px] text-[#4A5568] uppercase font-mono">Result</span>
          {['ALL','WIN','LOSS'].map(v => (
            <button key={v} onClick={() => setFilterResult(v)}
              className={`px-2 py-1 text-[10px] font-mono font-bold rounded border transition-colors ${
                filterResult === v ? 'bg-teal-500/20 text-teal-300 border-teal-500/40' : 'bg-[#151C25] text-[#4A5568] border-[#1E2A3A] hover:text-[#8D98A8]'
              }`}>{v}</button>
          ))}
        </div>
        {filterMode !== 'ALL' && (
          <div className={`flex items-center px-2 py-1 rounded border text-[10px] font-mono font-bold ${
            filterMode === 'LIVE' ? 'bg-red-500/15 text-red-400 border-red-500/40' : 'bg-amber-500/15 text-amber-300 border-amber-500/40'
          }`}>
            {filterMode === 'LIVE' ? '🔴 LIVE TRADES' : '🟡 PAPER TRADES'} ONLY
          </div>
        )}
      </div>

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="TRADES UNAVAILABLE" detail={err} /> : (
        <Panel>
          <PanelHeader title={`${filtered.length} TRADE${filtered.length !== 1 ? 'S' : ''}`}
            subtitle="From /api/v1/trading/trades" />
          {filtered.length === 0 ? (
            <EmptyBlock label="NO TRADES" sub="No closed trades match current filters." />
          ) : (
            <>
              <div className="grid grid-cols-[100px_1fr_80px_80px_80px_90px_90px_100px_120px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
                <span>Trade ID</span><span>Symbol</span>
                <span className="text-right">Entry</span><span className="text-right">Exit</span>
                <span className="text-right">Qty</span>
                <span>Entry Time</span><span>Exit Time</span>
                <span className="text-right">P&L</span>
                <span>Exit Reason</span>
                <span>Mode</span>
              </div>
              {filtered.map((t, i) => (
                <div key={t.trade_id ?? i} className="grid grid-cols-[100px_1fr_80px_80px_80px_90px_90px_100px_120px_80px] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/50 hover:bg-[#111821]/50 text-[11px] font-mono items-center">
                  <span className="text-[#4A5568] truncate">{t.trade_id ?? '—'}</span>
                  <span className="text-[#F5F7FA] truncate">{t.symbol}</span>
                  <span className="text-right">{t.entry_price != null ? `₹${t.entry_price}` : '—'}</span>
                  <span className="text-right">{t.exit_price != null ? `₹${t.exit_price}` : '—'}</span>
                  <span className="text-right">{t.quantity}</span>
                  <span className="text-[#8D98A8]">{t.entry_time ? String(t.entry_time).substring(11,19) : '—'}</span>
                  <span className="text-[#8D98A8]">{t.exit_time ? String(t.exit_time).substring(11,19) : '—'}</span>
                  <span className={`text-right font-bold ${pnlC(t.pnl)}`}>{t.pnl != null ? `₹${t.pnl.toFixed(2)}` : '—'}</span>
                  <span className="text-[#8D98A8] truncate text-[10px]">{t.exit_reason ?? '—'}</span>
                  <StatusBadge text={t.execution_mode ?? '—'} color={t.execution_mode === 'LIVE' ? 'red' : 'amber'} />
                </div>
              ))}
            </>
          )}
        </Panel>
      )}
    </div>
  );
}
