import React, { useState, useEffect } from 'react';

// Global toast dispatch helper
let addToastFn = null;
export const triggerToast = (toast) => {
  if (addToastFn) addToastFn(toast);
};

export const ToastContainer = () => {
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    addToastFn = (toast) => {
      const id = Date.now() + Math.random();
      const newToast = { id, ...toast };
      setToasts((prev) => [...prev, newToast]);

      // Auto-dismiss after 5s (except HALT)
      if (toast.type !== 'HALT') {
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== id));
        }, 5000);
      }
    };

    return () => {
      addToastFn = null;
    };
  }, []);

  const dismissToast = (id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2.5 max-w-sm w-full pointer-events-none">
      {toasts.map((toast) => {
        let border = 'border-sky-500/50 bg-slate-900/95 text-slate-100';
        let icon = '⚡';

        if (toast.type === 'HALT') {
          border = 'border-rose-500 bg-rose-950/95 text-rose-100 shadow-rose-900/50';
          icon = '🚨';
        } else if (toast.type === 'SIGNAL_APLUS') {
          border = 'border-emerald-500 bg-emerald-950/95 text-emerald-100';
          icon = '🎯';
        } else if (toast.type === 'ORDER_FILLED') {
          border = 'border-emerald-500 bg-slate-900/95 text-emerald-300';
          icon = '✅';
        } else if (toast.type === 'RISK_80') {
          border = 'border-amber-500 bg-amber-950/95 text-amber-100';
          icon = '⚠️';
        } else if (toast.type === 'CASCADE_BRAKE') {
          border = 'border-rose-500 bg-rose-950/95 text-rose-200';
          icon = '🛑';
        }

        return (
          <div
            key={toast.id}
            className={`pointer-events-auto flex items-start justify-between p-3 rounded-lg border shadow-xl backdrop-blur transition-all duration-300 animate-slide-in ${border}`}
          >
            <div className="flex items-start gap-2.5">
              <span className="text-base shrink-0">{icon}</span>
              <div>
                <div className="font-bold text-xs uppercase tracking-wider font-mono">
                  {toast.title || 'System Notification'}
                </div>
                <div className="text-xs mt-0.5 leading-snug opacity-90 font-mono">
                  {toast.message}
                </div>
              </div>
            </div>

            <button
              onClick={() => dismissToast(toast.id)}
              className="text-slate-400 hover:text-white ml-2 text-sm font-bold"
            >
              ✕
            </button>
          </div>
        );
      })}
    </div>
  );
};

export default ToastContainer;
