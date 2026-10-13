import React, { useEffect, lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore, ROLE_GUEST, ROLE_OPERATOR } from './store/authStore';
import Login from './pages/Login';
import AppShell from './components/layout/AppShell';
import { LoadingBlock } from './components/ui';

// ── Lazy-loaded pages ─────────────────────────────────────────────────────────
const Overview        = lazy(() => import('./pages/Overview'));
const AuthPage        = lazy(() => import('./pages/auth/AuthPage'));
const BrokersPage     = lazy(() => import('./pages/brokers/BrokersPage'));
const MarketPage      = lazy(() => import('./pages/market/MarketPage'));
const StrategyPage    = lazy(() => import('./pages/strategy/StrategyPage'));
const RiskPage        = lazy(() => import('./pages/risk/RiskPage'));
const ExecutionPage   = lazy(() => import('./pages/execution/ExecutionPage'));
const ModulesPage     = lazy(() => import('./pages/modules/ModulesPage'));
const ChecklistPage   = lazy(() => import('./pages/checklist/ChecklistPage'));
const SignalsPage     = lazy(() => import('./pages/signals/SignalsPage'));
const WatchlistPage   = lazy(() => import('./pages/watchlist/WatchlistPage'));
const QueuePage       = lazy(() => import('./pages/queue/QueuePage'));
const PositionsPage   = lazy(() => import('./pages/positions/PositionsPage'));
const OrdersPage      = lazy(() => import('./pages/orders/OrdersPage'));
const TradesPage      = lazy(() => import('./pages/trades/TradesPage'));
const AuditPage       = lazy(() => import('./pages/audit/AuditPage'));
const APIHealthPage   = lazy(() => import('./pages/system/APIHealthPage'));
const EventsPage      = lazy(() => import('./pages/system/EventsPage'));
const ConfigPage      = lazy(() => import('./pages/system/ConfigPage'));
const ReconcilePage   = lazy(() => import('./pages/reconciliation/ReconcilePage'));

// ── Route guards ──────────────────────────────────────────────────────────────
function RequireAuth({ children }) {
  const auth = useAuthStore(s => s.isAuthenticated);
  return auth ? children : <Navigate to="/login" replace />;
}

function RequireOperator({ children }) {
  const { isAuthenticated, role } = useAuthStore(s => ({ isAuthenticated: s.isAuthenticated, role: s.role }));
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (role !== ROLE_OPERATOR) return (
    <div className="p-8 text-center">
      <div className="text-red-400 font-mono text-sm font-bold">ACCESS DENIED</div>
      <div className="text-[#8D98A8] text-xs mt-1">Operator role required</div>
    </div>
  );
  return children;
}

function PageFallback() {
  return (
    <div className="min-h-[200px] flex items-center justify-center">
      <span className="text-[#4A5568] font-mono text-[11px] animate-pulse">Loading…</span>
    </div>
  );
}

export default function App() {
  useEffect(() => {
    document.title = 'Strike Desk V4.5';
    let link = document.querySelector("link[rel*='icon']");
    if (!link) { link = document.createElement('link'); link.rel = 'shortcut icon'; document.head.appendChild(link); }
    link.href = 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>';
  }, []);

  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* Protected app shell — all sub-routes share TopBar + NavSidebar */}
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        {/* Overview */}
        <Route index element={<Suspense fallback={<PageFallback />}><Overview /></Suspense>} />
        <Route path="overview" element={<Navigate to="/" replace />} />

        {/* Workflow */}
        <Route path="auth"      element={<Suspense fallback={<PageFallback />}><AuthPage /></Suspense>} />
        <Route path="brokers"   element={<Suspense fallback={<PageFallback />}><BrokersPage /></Suspense>} />
        <Route path="market"    element={<Suspense fallback={<PageFallback />}><MarketPage /></Suspense>} />
        <Route path="strategy"  element={<Suspense fallback={<PageFallback />}><StrategyPage /></Suspense>} />
        <Route path="risk"      element={<Suspense fallback={<PageFallback />}><RiskPage /></Suspense>} />
        <Route path="execution" element={<Suspense fallback={<PageFallback />}><ExecutionPage /></Suspense>} />
        <Route path="modules"   element={<Suspense fallback={<PageFallback />}><ModulesPage /></Suspense>} />
        <Route path="checklist" element={<Suspense fallback={<PageFallback />}><ChecklistPage /></Suspense>} />

        {/* Trading */}
        <Route path="signals"   element={<Suspense fallback={<PageFallback />}><SignalsPage /></Suspense>} />
        <Route path="watchlist" element={<Suspense fallback={<PageFallback />}><WatchlistPage /></Suspense>} />
        <Route path="queue"     element={<Suspense fallback={<PageFallback />}><QueuePage /></Suspense>} />
        <Route path="positions" element={<Suspense fallback={<PageFallback />}><PositionsPage /></Suspense>} />
        <Route path="orders"    element={<Suspense fallback={<PageFallback />}><OrdersPage /></Suspense>} />
        <Route path="trades"    element={<Suspense fallback={<PageFallback />}><TradesPage /></Suspense>} />

        {/* System */}
        <Route path="audit"           element={<Suspense fallback={<PageFallback />}><AuditPage /></Suspense>} />
        <Route path="system/health"   element={<Suspense fallback={<PageFallback />}><APIHealthPage /></Suspense>} />
        <Route path="system/events"   element={<Suspense fallback={<PageFallback />}><EventsPage /></Suspense>} />
        <Route path="reconciliation"  element={<Suspense fallback={<PageFallback />}><ReconcilePage /></Suspense>} />
        <Route path="configuration"   element={<Suspense fallback={<PageFallback />}><ConfigPage /></Suspense>} />

        {/* Legacy paths kept */}
        <Route path="dashboard"       element={<Navigate to="/" replace />} />
        <Route path="broker-setup"    element={<Navigate to="/brokers" replace />} />
        <Route path="guest-dashboard" element={<Navigate to="/" replace />} />

        {/* Catch-all inside shell */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
