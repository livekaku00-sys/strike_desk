import React, { useState, useEffect, useRef } from 'react';
import webSocketService from '../services/websocket';

// Starts empty: a seeded fill or P&L line is indistinguishable from a real
// audit entry once it is in the log.
export const AuditTrail = ({ isOpen, onClose }) => {
  const [logs, setLogs] = useState([]);
  const [filter, setFilter] = useState('ALL');
  const scrollRef = useRef(null);

  // Auto-scroll to bottom on new log entry
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  // Subscribe to WebSocket events to log entries dynamically
  useEffect(() => {
    const unsub = webSocketService.subscribe('*', (event) => {
      const now = new Date().toISOString().replace('T', ' ').substring(0, 19);
      let type = (event.channel || event.type || 'SIGNAL').toUpperCase();
      let message = '';

      // An audit line must never invent an order id, price or P&L — a fabricated
      // value here is indistinguishable from a real one in the log.
      const f = (v) => (v == null || v === '' ? '—' : v);
      if (type === 'ORDER') message = `Placed: ${f(event.order_id)}, Qty=${f(event.quantity)}`;
      else if (type === 'FILL') message = `Filled: ${f(event.order_id)}, Price=₹${f(event.price)}`;
      else if (type === 'RISK') message = `P&L Update: Total=₹${f(event.pnl)}`;
      else if (type === 'HALT') message = `EMERGENCY HALT TRIGGERED: ${f(event.reason)}`;
      else message = `${f(event.symbol)}: Score=${f(event.score)}`;

      const newEntry = {
        id: Date.now() + Math.random(),
        type,
        text: `${now} [${type}] ${message}`,
      };

      setLogs((prev) => [...prev.slice(-49), newEntry]);
    });

    return () => unsub();
  }, []);

  // Export to CSV
  const handleExportCSV = () => {
    const csvContent = 'data:text/csv;charset=utf-8,ID,Timestamp,Type,Log Entry\n' +
      logs.map((l) => `"${l.id}","${l.text.substring(0, 19)}","${l.type}","${l.text.replace(/"/g, '""')}"`).join('\n');
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `strike_desk_audit_trail_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const filteredLogs = filter === 'ALL' ? logs : logs.filter((l) => l.type === filter);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 select-none">
      <div className="bg-[#0B0E14] border border-slate-700 rounded-xl max-w-3xl w-full flex flex-col max-h-[85vh] shadow-2xl">
        {/* Modal Header */}
        <div className="p-4 border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-xl text-sky-400">📋</span>
            <h3 className="font-bold text-sm md:text-base text-slate-100 font-mono tracking-wide">
              AUDIT TRAIL LOG VIEWER (LAST 50 ENTRIES)
            </h3>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-100 p-1 text-lg font-bold"
          >
            ✕
          </button>
        </div>

        {/* Toolbar & Filters */}
        <div className="px-4 py-2.5 bg-[#11151F] border-b border-slate-800 flex flex-wrap items-center justify-between gap-2 text-xs font-mono">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-slate-500 mr-1">Filter:</span>
            {['ALL', 'SIGNAL', 'ORDER', 'FILL', 'RISK', 'HALT'].map((t) => (
              <button
                key={t}
                onClick={() => setFilter(t)}
                className={`px-2 py-0.5 rounded transition ${
                  filter === t
                    ? 'bg-sky-500/20 text-sky-400 border border-sky-500/40 font-bold'
                    : 'bg-slate-900 text-slate-400 hover:text-slate-200'
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          <button
            onClick={handleExportCSV}
            className="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded border border-slate-700 flex items-center gap-1 font-semibold"
          >
            <span>⬇️</span> Export CSV
          </button>
        </div>

        {/* Log Stream Container */}
        <div
          ref={scrollRef}
          className="flex-1 p-4 overflow-y-auto space-y-1.5 font-mono text-xs bg-[#07090E]"
        >
          {filteredLogs.length === 0 && (
            <div className="py-10 text-center text-slate-500">
              No audit entries yet — live events will appear here.
            </div>
          )}
          {filteredLogs.map((log) => {
            let colorClass = 'text-slate-300';
            if (log.type === 'SIGNAL') colorClass = 'text-blue-400 bg-blue-950/20 border-l-2 border-blue-500';
            else if (log.type === 'ORDER') colorClass = 'text-amber-400 bg-amber-950/20 border-l-2 border-amber-500';
            else if (log.type === 'FILL') colorClass = 'text-emerald-400 bg-emerald-950/20 border-l-2 border-emerald-500';
            else if (log.type === 'RISK') colorClass = 'text-purple-400 bg-purple-950/20 border-l-2 border-purple-500';
            else if (log.type === 'HALT') colorClass = 'text-rose-400 bg-rose-950/30 border-l-2 border-rose-500 font-bold';

            return (
              <div
                key={log.id}
                className={`p-2 rounded font-mono break-all leading-relaxed ${colorClass}`}
              >
                {log.text}
              </div>
            );
          })}
        </div>

        {/* Modal Footer */}
        <div className="p-3 border-t border-slate-800 flex justify-between items-center text-xs text-slate-400 font-mono">
          <span>Entries: {filteredLogs.length} / {logs.length}</span>
          <span>Live Auto-Scroll: Active</span>
        </div>
      </div>
    </div>
  );
};

export default AuditTrail;
