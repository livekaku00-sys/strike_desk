/**
 * Strike Desk V4.5 — Positions Page
 * Open positions + exit engine status. One-at-a-time gate visible.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { execution as execApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, Card, StatusBadge, KV, LoadingBlock,
  EmptyBlock, PageHeader, ActionBtn, ErrorBlock,
} from '../../components/ui';

const POLL = 5000;

function ExitEngine({ pos }) {
  const ltp  = pos.current_price;
  const sl   = pos.stop_loss_price;
  const tgt  = pos.target_price;
  const trail = pos.trail_sl_price;

  const exitConditions = [
    {
      name: 'Stop Loss',
      value: sl != null ? `₹ ${sl}` : '—',
      status: sl != null && ltp != null ? (ltp <= sl ? 'TRIGGERED' : 'ARMED') : 'NOT SET',
      color: 'red',
      detail: sl != null && ltp != null ? `LTP ₹${ltp} vs SL ₹${sl} (dist: ₹${(ltp - sl).toFixed(2)})` : '',
    },
    {
      name: 'Target',
      value: tgt != null ? `₹ ${tgt}` : '—',
      status: tgt != null && ltp != null ? (ltp >= tgt ? 'TRIGGERED' : 'ARMED') : 'NOT SET',
      color: 'green',
      detail: tgt != null && ltp != null ? `LTP ₹${ltp} vs TGT ₹${tgt} (dist: ₹${(tgt - ltp).toFixed(2)})` : '',
    },
    {
      name: 'Trailing SL',
      value: trail != null ? `₹ ${trail}` : '—',
      status: trail != null && ltp != null ? (ltp <= trail ? 'TRIGGERED' : 'ARMED') : 'NOT SET',
      color: 'blue',
      detail: 'Ratchets 5% below running high',
    },
    {
      name: 'Stagnation (15 min)',
      value: pos.opened_at ? `${Math.round((Date.now() - new Date(pos.opened_at).getTime()) / 60000)}m` : '—',
      status: 'ARMED',
      color: 'amber',
      detail: 'Triggers if no SL/target hit after 15 minutes',
    },
    {
      name: 'EOD Square-Off',
      value: '15:10 IST',
      status: 'ARMED',
      color: 'purple',
      detail: 'All positions closed at market after 15:10',
    },
  ];

  const stColor = { TRIGGERED: 'red', ARMED: 'green', 'NOT SET': 'gray' };

  return (
    <div>
      <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-3">EXIT ENGINE — PRIORITY ORDER</div>
      <div className="space-y-0">
        {exitConditions.map((c, i) => (
          <div key={c.name} className="flex items-center gap-3 py-2.5 border-b border-[#1E2A3A]">
            <span className="text-[10px] font-mono text-[#4A5568] w-3">{i + 1}</span>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="text-[12px] text-[#F5F7FA]">{c.name}</span>
                <StatusBadge text={c.status} color={stColor[c.status] ?? 'gray'} />
              </div>
              {c.detail && <div className="text-[10px] text-[#8D98A8] mt-0.5 font-mono">{c.detail}</div>}
            </div>
            <span className="font-mono text-[12px] text-[#F5F7FA]">{c.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function EODCountdown() {
  const [countdown, setCountdown] = useState('');
  useEffect(() => {
    const update = () => {
      const now = new Date();
      const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
      const eod = new Date(ist);
      eod.setHours(15, 10, 0, 0);
      const diff = eod - ist;
      if (diff <= 0) { setCountdown('EOD SQUARE-OFF ACTIVE'); return; }
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      setCountdown(`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`);
    };
    update();
    const t = setInterval(update, 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="flex items-center gap-2 text-[11px] font-mono text-[#8D98A8]">
      <span>EOD SQUARE-OFF</span>
      <span className="text-amber-300 font-bold">{countdown}</span>
    </div>
  );
}

export default function PositionsPage() {
  const { role } = useAuthStore();
  const isOperator = role === 'operator';

  const [positions, setPositions] = useState([]);
  const [loading, setLoading]     = useState(true);
  const [err, setErr]             = useState(null);
  const [selected, setSelected]   = useState(null);
  const [squaring, setSquaring]   = useState({});
  const [sqMsg, setSqMsg]         = useState(null);

  const fetch = useCallback(async () => {
    try {
      const r = await execApi.positions();
      const arr = r.data?.positions ?? r.data ?? [];
      setPositions(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const doSquareOff = async (symbol) => {
    setSquaring(p => ({ ...p, [symbol]: true }));
    setSqMsg(null);
    try {
      await execApi.squareOff(encodeURIComponent(symbol));
      setSqMsg({ ok: true, text: `Square-off submitted for ${symbol}` });
      await fetch();
    } catch(e) {
      setSqMsg({ ok: false, text: e.response?.data?.detail || e.message });
    } finally {
      setSquaring(p => ({ ...p, [symbol]: false }));
    }
  };

  const pnlColor = (v) => { if (v == null) return ''; return v >= 0 ? 'text-green-300' : 'text-red-400'; };

  return (
    <div>
      <PageHeader
        title="Positions"
        subtitle="Open positions from backend. P&L, exits, and manual square-off."
        right={
          <div className="flex items-center gap-3">
            <EODCountdown />
            <ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>
          </div>
        }
      />

      {sqMsg && (
        <div className={`mb-4 px-4 py-2 rounded border text-[12px] font-mono ${
          sqMsg.ok ? 'bg-green-950/20 border-green-800/30 text-green-300' : 'bg-red-950/20 border-red-800/40 text-red-300'
        }`}>
          {sqMsg.text}
        </div>
      )}

      {/* One-at-a-time note */}
      {positions.length > 0 && (
        <div className="mb-4 px-3 py-2 bg-amber-950/20 border border-amber-800/30 rounded text-[11px] font-mono text-amber-300">
          ONE-AT-A-TIME GATE ACTIVE — Signals are queued while a position is open
        </div>
      )}

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="POSITIONS UNAVAILABLE" detail={err} /> :
       positions.length === 0 ? (
        <Panel>
          <EmptyBlock
            label="NO OPEN POSITIONS"
            sub="The execution engine is currently flat."
          />
        </Panel>
       ) : (
        <div className="space-y-4">
          {positions.map((pos, i) => (
            <Panel key={pos.symbol ?? i}>
              <PanelHeader
                title={pos.symbol}
                subtitle={pos.asset_class ?? 'OPTION'}
                right={
                  <div className="flex items-center gap-2">
                    <StatusBadge text={pos.direction === 'LONG' ? 'CE ↑' : 'PE ↓'} color={pos.direction === 'LONG' ? 'green' : 'red'} />
                    {isOperator && (
                      <ActionBtn
                        color="red"
                        disabled={squaring[pos.symbol]}
                        onClick={() => doSquareOff(pos.symbol)}
                      >
                        {squaring[pos.symbol] ? '…' : 'Square Off'}
                      </ActionBtn>
                    )}
                  </div>
                }
              />
              <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-3">POSITION DETAIL</div>
                  <KV label="Symbol"       value={pos.symbol} />
                  <KV label="Direction"    value={pos.direction} />
                  <KV label="Quantity"     value={pos.quantity} />
                  <KV label="Entry Price"  value={pos.entry_price != null ? `₹ ${pos.entry_price}` : null} />
                  <KV label="LTP"          value={pos.current_price != null ? `₹ ${pos.current_price}` : null} />
                  <KV label="Unrealized P&L" value={pos.unrealized_pnl != null ? `₹ ${pos.unrealized_pnl.toFixed(2)}` : null}
                    valueColor={pnlColor(pos.unrealized_pnl)} />
                  <KV label="Opened At"   value={pos.opened_at} />
                  <KV label="Broker"      value={pos.broker ?? 'dhan'} />
                  <KV label="Order ID"    value={pos.order_id ?? pos.broker_order_id} />
                </div>
                <div>
                  <ExitEngine pos={pos} />
                </div>
              </div>
            </Panel>
          ))}
        </div>
       )}
    </div>
  );
}
