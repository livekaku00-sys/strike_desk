/**
 * Strike Desk V4.5 — Orders Page
 * Order lifecycle: PENDING → FILLED. Never derives position from order request.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { execution as execApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock,
} from '../../components/ui';

const POLL = 5000;
const STATUS_COLOR = {
  PENDING:      'amber',
  SUBMITTED:    'blue',
  ACKNOWLEDGED: 'blue',
  PARTIAL:      'amber',
  FILLED:       'green',
  CANCELLED:    'gray',
  REJECTED:     'red',
  FAILED:       'red',
};

export default function OrdersPage() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const fetch = useCallback(async () => {
    try {
      const r = await execApi.orders();
      const arr = r.data?.orders ?? r.data ?? [];
      setOrders(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const pending = orders.filter(o => !['FILLED','CANCELLED','REJECTED','FAILED'].includes(o.status));
  const completed = orders.filter(o => ['FILLED','CANCELLED','REJECTED','FAILED'].includes(o.status));

  const Row = ({ o }) => (
    <div className="grid grid-cols-[120px_100px_1fr_80px_80px_90px_100px_120px_90px] gap-2 px-4 py-2.5 border-b border-[#1E2A3A] hover:bg-[#111821]/50 text-[11px] font-mono items-center">
      <span className="text-[#4A5568] truncate">{o.order_id ?? '—'}</span>
      <span className="text-[#8D98A8] truncate">{o.signal_id ?? '—'}</span>
      <span className="text-[#F5F7FA] truncate">{o.symbol}</span>
      <span className={o.side === 'BUY' ? 'text-green-300 font-bold' : 'text-red-400 font-bold'}>{o.side}</span>
      <span className="text-right">{o.quantity}</span>
      <span className="text-right text-[#8D98A8]">{o.order_type ?? '—'}</span>
      <span className="text-right">{o.price != null ? `₹${o.price}` : '—'}</span>
      <StatusBadge text={o.status ?? '—'} color={STATUS_COLOR[o.status] ?? 'gray'} />
      <span className="text-[#4A5568]">{o.created_at ? o.created_at.substring(11,19) : '—'}</span>
    </div>
  );

  const ColHead = () => (
    <div className="grid grid-cols-[120px_100px_1fr_80px_80px_90px_100px_120px_90px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
      <span>Order ID</span><span>Signal ID</span><span>Symbol</span>
      <span>Side</span><span className="text-right">Qty</span><span className="text-right">Type</span>
      <span className="text-right">Price</span><span>Status</span><span>Time</span>
    </div>
  );

  return (
    <div>
      <PageHeader
        title="Orders"
        subtitle="Order lifecycle. Position created only after confirmed fill — not from order request."
        right={<ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>}
      />

      <div className="mb-3 px-3 py-2 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] font-mono text-[#8D98A8]">
        ORDER REQUEST ≠ FILLED. A position requires: Order → Broker Accept → Order ID → Pending → Fill confirmed.
      </div>

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="ORDERS UNAVAILABLE" detail={err} /> : (
        <div className="space-y-4">
          <Panel>
            <PanelHeader title={`ACTIVE ORDERS (${pending.length})`} />
            {pending.length === 0 ? (
              <EmptyBlock label="NO ACTIVE ORDERS" />
            ) : (
              <><ColHead />{pending.map((o,i) => <Row key={i} o={o} />)}</>
            )}
          </Panel>
          <Panel>
            <PanelHeader title={`ORDER HISTORY (${completed.length})`} />
            {completed.length === 0 ? (
              <EmptyBlock label="NO COMPLETED ORDERS" />
            ) : (
              <><ColHead />{completed.slice(0, 50).map((o,i) => <Row key={i} o={o} />)}</>
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
