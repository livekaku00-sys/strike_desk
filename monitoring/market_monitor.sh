#!/usr/bin/env bash
# Strike Desk V4.5 — Market-Open Diagnostic Monitor
# Read-only: all requests are HTTP GET. No state changes, no restarts.
# Server TZ: Asia/Kolkata (IST). All times in IST.
# Stable path: /root/strike_desk/monitoring/market_monitor.sh
#
# Phases (auto-detected from IST clock; override with first arg):
#   PRE         before 09:15 IST — process health only
#   TICK_CHECK  09:15–09:20 IST — verify consumed_ticks > 0
#   PCR_CHECK   09:21–09:25 IST — verify pcr_poller PASS
#   IND_CHECK   09:26–09:39 IST — verify per-symbol indicators
#   SESSION     09:40–15:29 IST — full pipeline + signal trace
#   POST        ≥15:30 IST      — session summary
#
# Usage:
#   bash /root/strike_desk/monitoring/market_monitor.sh            # auto-phase
#   bash /root/strike_desk/monitoring/market_monitor.sh TICK_CHECK

set -euo pipefail
BASE="http://localhost:8000"
LOG_DIR="/root/strike_desk/monitoring/logs"
mkdir -p "$LOG_DIR"
DATE_TAG=$(date '+%Y-%m-%d')
LOG="${LOG_DIR}/monitor_${DATE_TAG}.log"
IST_STR=$(date '+%Y-%m-%d %H:%M:%S IST')
HR=$(date '+%H'); MN=$(date '+%M')
HHMM=$((10#$HR * 100 + 10#$MN))

if   [ "$HHMM" -lt  915 ]; then PHASE="PRE"
elif [ "$HHMM" -lt  921 ]; then PHASE="TICK_CHECK"
elif [ "$HHMM" -lt  926 ]; then PHASE="PCR_CHECK"
elif [ "$HHMM" -lt  940 ]; then PHASE="IND_CHECK"
elif [ "$HHMM" -lt 1530 ]; then PHASE="SESSION"
else PHASE="POST"
fi
[ $# -ge 1 ] && PHASE="$1"

# ── Probe symbols ─────────────────────────────────────────────────────────────
INDEX_PROBES="NIFTY BANKNIFTY"
EQ_PROBES="RELIANCE HDFCBANK INFOSYS TCS ICICIBANK"

echo "============================================================" | tee -a "$LOG"
printf '[%s] PHASE=%-12s\n' "$IST_STR" "$PHASE" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# ── HTTP helpers ──────────────────────────────────────────────────────────────
# get_safe: curl -sf fails on 4xx/5xx; errors printed to STDERR only (not stdout).
# Caller captures stdout; errors appear in terminal AND log via STDERR redirect.
get_safe() {
    local url="$1" label="$2"
    local out
    out=$(curl -sf --max-time 6 "${BASE}${url}" 2>/dev/null) || {
        printf '  [ERR] %-22s UNREACHABLE or non-2xx: %s\n' \
            "$label" "${BASE}${url}" >&2
        echo "{}"
        return
    }
    echo "$out"
}

# get_raw: curl -s (no -f) — captures 4xx/5xx bodies as JSON.
# Used where error bodies contain useful diagnostics (indicator endpoint).
get_raw() {
    local url="$1"
    curl -s --max-time 6 "${BASE}${url}" 2>/dev/null || echo "{}"
}

fmt() { python3 -c "import json,sys; d=json.load(sys.stdin); $1" 2>/dev/null || echo "  (parse error)"; }

# Redirect stderr to both terminal and log for error messages from get_safe
exec 2> >(tee -a "$LOG" >&2)

# ── 1. PIPELINE STATUS ───────────────────────────────────────────────────────
printf '\n── 1. Pipeline Status ──────────────────────────────────\n' | tee -a "$LOG"
PIPE=$(get_safe "/pipeline/status" "pipeline/status")
PIPE_OK=$(echo "$PIPE" | python3 -c "import json,sys; d=json.load(sys.stdin); print('yes' if d.get('is_running') else 'no')" 2>/dev/null || echo "no")
echo "$PIPE" | fmt "
p=d
print(f'  is_running:      {p.get(\"is_running\")}')
print(f'  consumed_ticks:  {p.get(\"consumed_ticks\")}')
print(f'  stale_count:     {p.get(\"stale_count\")} symbols')
pcr_stale = p.get(\"pcr_stale_symbols\",[])
print(f'  pcr_stale:       {pcr_stale[:6] if pcr_stale else \"none\"}')
print(f'  pipeline_start:  {str(p.get(\"pipeline_start_time\",\"?\"))[:19]}')
print(f'  last_save:       {str(p.get(\"last_save_time\",\"?\"))[:19]}')
" | tee -a "$LOG"

CONSUMED=$(echo "$PIPE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('consumed_ticks',0))" 2>/dev/null || echo "0")

# ── 2. DHAN BROKER ───────────────────────────────────────────────────────────
printf '\n── 2. Dhan Broker ──────────────────────────────────────\n' | tee -a "$LOG"
DHAN=$(get_safe "/api/v1/brokers/dhan/status" "brokers/dhan/status")
DHAN_OK=$(echo "$DHAN" | python3 -c "import json,sys; print('yes' if json.load(sys.stdin).get('is_connected') else 'no')" 2>/dev/null || echo "no")
echo "$DHAN" | fmt "
print(f'  is_connected:    {d.get(\"is_connected\")}')
print(f'  latency_ms:      {d.get(\"latency_ms\")}')
print(f'  last_heartbeat:  {str(d.get(\"last_heartbeat\",\"?\"))[:19]}')
print(f'  error:           {d.get(\"error_message\")}')
" | tee -a "$LOG"

# ── 3. TICK FRESHNESS (TICK_CHECK phase onward) ───────────────────────────────
if [ "$PHASE" != "PRE" ]; then
    printf '\n── 3. Tick Freshness ────────────────────────────────────\n' | tee -a "$LOG"
    # Interpretation rule: ticks are only fresh if timestamped >= today's 09:15 IST.
    # Stale ticks persisted from a prior run must NOT be counted as live market data.
    TICKS=$(get_safe "/api/v1/market/ticks" "market/ticks")
    TICKS_JSON="$TICKS" python3 - <<'PYEOF' 2>/dev/null | tee -a "$LOG" || echo "  (tick parse error)" | tee -a "$LOG"
import json, os, datetime
data = json.loads(os.environ.get('TICKS_JSON','{}'))
ticks = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
is_session_time = now.hour * 100 + now.minute >= 915

print(f'  tick_store_total: {len(ticks)}')
fresh = []
stale_sample = []
for t in ticks:
    ts_str = str(t.get('timestamp',''))
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace('Z','+00:00'))
        ts_ist = ts.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        if ts_ist >= session_open:
            fresh.append((ts_ist, t))
        else:
            stale_sample.append((ts_ist, t))
    except:
        pass

print(f'  fresh_this_session: {len(fresh)}  (timestamp >= 09:15 IST today)')
print(f'  stale_prior_runs:   {len(stale_sample)}')

if is_session_time and len(fresh) == 0 and len(ticks) > 0:
    newest_stale = max(stale_sample, key=lambda x: x[0]) if stale_sample else None
    print(f'  ⚠  WARNING: store has {len(ticks)} tick(s) but NONE from current session')
    if newest_stale:
        print(f'  ⚠  Newest stale: {newest_stale[1].get("symbol","?")} ts={str(newest_stale[0])[:19]}')
    print(f'  ⚠  FAILURE MODE: stale tick store only — no live market data yet')
elif is_session_time and len(fresh) > 0:
    newest = max(fresh, key=lambda x: x[0])
    age_s = (now - newest[0]).total_seconds()
    print(f'  ✅ Live ticks present. Newest: {newest[1].get("symbol","?")} age={age_s:.0f}s ts={str(newest[0])[:19]}')
    # 5 most recent
    for ts_ist, t in sorted(fresh, key=lambda x: x[0], reverse=True)[:5]:
        print(f'    {str(t.get("symbol","?")):18s} ltp={t.get("ltp","?")} ts={str(ts_ist)[:19]}')
elif not is_session_time:
    if stale_sample:
        newest_stale = max(stale_sample, key=lambda x: x[0])
        print(f'  ℹ  Pre-market: no live ticks expected. Newest stored: {newest_stale[1].get("symbol","?")} ts={str(newest_stale[0])[:19]}')
    else:
        print(f'  ℹ  Pre-market: tick store empty.')

# EQ symbol mismatch check — Dhan delivers EQ ticks as NSE:SYMBOL-EQ but pipeline
# subscribes to bare SYMBOL. Ticks stored under NSE: prefix are NOT routed to any
# pipeline calculator, so EQ indicators never warm up and STOCK_OPTIONS signals
# are never generated.
nse_pfx = [t for t in ticks if str(t.get('symbol','')).startswith('NSE:')]
bare = [t for t in ticks if not str(t.get('symbol','')).startswith('NSE:')]
if nse_pfx:
    print(f'')
    print(f'  ⚠  SYMBOL MISMATCH: {len(nse_pfx)} ticks in NSE:SYMBOL-EQ format (e.g. NSE:RELIANCE-EQ)')
    print(f'  ⚠  Pipeline subscribes to bare SYMBOL names. EQ calculator never receives these ticks.')
    print(f'  ⚠  Result: EQ indicators empty → zero STOCK_OPTIONS signals (root cause confirmed).')
    for t in nse_pfx[:3]:
        print(f'     example: {t.get("symbol")} ltp={t.get("ltp")}')
else:
    print(f'  ✅ No NSE: prefix mismatch detected in tick store')
PYEOF
fi

# ── 4. PREFLIGHT / PCR ────────────────────────────────────────────────────────
if [ "$PHASE" = "PCR_CHECK" ] || [ "$PHASE" = "IND_CHECK" ] || \
   [ "$PHASE" = "SESSION" ] || [ "$PHASE" = "POST" ]; then
    printf '\n── 4. Preflight / PCR ───────────────────────────────────\n' | tee -a "$LOG"
    PRE_F=$(get_safe "/api/v1/preflight/status" "preflight/status")
    PRE_F_JSON="$PRE_F" python3 - <<'PYEOF' 2>/dev/null | tee -a "$LOG" || echo "  (preflight parse error)" | tee -a "$LOG"
import json, os, datetime
d = json.loads(os.environ.get('PRE_F_JSON','{}'))
checks = d.get('checks', [])
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
is_post_open = now.hour * 100 + now.minute >= 915

for c in checks:
    name = c.get('name','?'); sev = c.get('severity','?'); stat = c.get('status','?')
    msg  = c.get('message','')[:85]
    flag = '🔴 FAIL' if stat == 'FAIL' else ('🟡 PEND' if stat == 'PENDING' else '🟢 PASS')
    print(f'  {flag}  {name:25s} [{sev:8s}]  {msg}')

print()
ran_at = d.get('ran_at','?')
print(f'  live_mode_blocked: {d.get("live_mode_blocked","?")}')
print(f'  preflight_ran_at:  {str(ran_at)[:25]}')
# Stale preflight warning
import datetime as _dt
try:
    ran_dt = _dt.datetime.fromisoformat(ran_at.replace('Z','+00:00')) if ran_at != '?' else None
    age_min = (now - ran_dt.astimezone(now.tzinfo)).total_seconds() / 60 if ran_dt else None
    if age_min and age_min > 5:
        print(f'  ⚠  STALE CACHE: preflight ran {age_min:.0f} min ago — results above are outdated')
except Exception:
    pass
print()

# PCR-specific classification
for c in checks:
    if c.get('name') != 'pcr_poller':
        continue
    stat = c.get('status','?')
    msg  = c.get('message','')
    if stat == 'PENDING' and not is_post_open:
        print('  ℹ  PCR PENDING — expected before 09:15 IST (fetch loop not yet active)')
        print('     This is NOT a failure. Will re-check after market opens.')
    elif stat == 'PENDING' and is_post_open:
        # Check if this is just a stale cache
        try:
            ran_dt = _dt.datetime.fromisoformat(ran_at.replace('Z','+00:00')) if ran_at != '?' else None
            ran_ist = ran_dt.astimezone(now.tzinfo) if ran_dt else None
            if ran_ist and ran_ist.hour * 100 + ran_ist.minute < 915:
                print('  ℹ  PCR PENDING — stale preflight snapshot from pre-market')
                print('     Run POST /api/v1/preflight/run for current state')
            else:
                print('  ⚠  PCR PENDING after market open — unexpected. Fetch loop may be delayed.')
        except Exception:
            print('  ⚠  PCR PENDING after market open — unexpected. Fetch loop may be delayed.')
    elif stat == 'FAIL':
        print('  ❌ PCR FAIL — ALL option signals are blocked until PCR becomes fresh')
        print(f'     Reason: {msg}')
        print('     Investigate: _pcr_fetch_loop task, Dhan option-chain API, pcr_staleness_monitor._last_updates')
    elif stat == 'PASS':
        print('  ✅ PCR PASS — option signals unblocked (NIFTY/BANKNIFTY data fresh)')
PYEOF
fi

# ── 5. PER-SYMBOL INDICATORS (IND_CHECK phase onward) ────────────────────────
if [ "$PHASE" = "IND_CHECK" ] || [ "$PHASE" = "SESSION" ] || [ "$PHASE" = "POST" ]; then
    printf '\n── 5. Per-Symbol Indicators ─────────────────────────────\n' | tee -a "$LOG"
    printf '  Warm-up: MACD(12,26,9) needs 35 per-tick LTP updates per symbol\n' | tee -a "$LOG"
    printf '  Expected ready: ~30-60s after first tick arrives\n\n' | tee -a "$LOG"
    for SYM in $INDEX_PROBES $EQ_PROBES; do
        # Use get_raw (curl -s without -f) to capture 404 bodies
        IND=$(get_raw "/api/v1/market/indicators/$SYM")
        echo "$IND" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sym = '$SYM'
http_err = d.get('error') or d.get('detail')
if http_err:
    msg = d.get('message', str(http_err))
    if 'Insufficient' in msg or 'insufficient' in msg:
        print(f'  ⏳ {sym:15s}: WARMING UP — not enough ticks yet (MACD needs 35)')
    elif 'not available' in msg.lower() or '404' in str(d.get('status_code','')):
        print(f'  ❓ {sym:15s}: NOT IN REGISTRY — symbol not tracked this run')
    else:
        print(f'  ❌ {sym:15s}: ERROR — {msg[:70]}')
else:
    macd_d = d.get('macd') or {}
    ml  = macd_d.get('macd_line') if isinstance(macd_d, dict) else None
    sl  = macd_d.get('signal_line') if isinstance(macd_d, dict) else None
    ltp = d.get('ltp', '?')
    ts  = str(d.get('timestamp','?'))[:19]
    ok  = ml is not None and sl is not None
    flag = '✅' if ok else '⏳ NO MACD'
    ml_s = f'{ml:.4f}' if ml is not None else 'None'
    sl_s = f'{sl:.4f}' if sl is not None else 'None'
    print(f'  {flag} {sym:15s}: ltp={ltp}  macd={ml_s}  signal={sl_s}  ts={ts}')
" 2>/dev/null | tee -a "$LOG" || printf '  ❓ %s: (parse error)\n' "$SYM" | tee -a "$LOG"
    done
fi

# ── 6. SIGNALS ────────────────────────────────────────────────────────────────
printf '\n── 6. Signals ───────────────────────────────────────────\n' | tee -a "$LOG"
SIGS=$(get_safe "/api/v1/trading/signals" "trading/signals")
SIGS_JSON="$SIGS" python3 - <<'PYEOF' 2>/dev/null | tee -a "$LOG" || echo "  (signals parse error)" | tee -a "$LOG"
import json, os
sigs = json.loads(os.environ.get('SIGS_JSON','[]'))
if not isinstance(sigs, list): sigs = sigs.get('signals', [])
approved = [s for s in sigs if s.get('risk_status') == 'APPROVED_FOR_EXECUTION']
rejected = [s for s in sigs if s.get('risk_status') not in ('APPROVED_FOR_EXECUTION', None, '')]
no_risk  = [s for s in sigs if not s.get('risk_status')]
print(f'  total:                  {len(sigs)}')
print(f'  approved_for_execution: {len(approved)}')
print(f'  risk_rejected:          {len(rejected)}')
print(f'  awaiting_risk_check:    {len(no_risk)}')
if rejected:
    reasons = {}
    for s in rejected:
        r = s.get('risk_rejection_reason') or s.get('risk_status') or 'UNKNOWN'
        reasons[r] = reasons.get(r, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f'    rejection reason: {reason} × {count}')
for s in sorted(sigs, key=lambda x: str(x.get('timestamp','')), reverse=True)[:5]:
    sym = str(s.get('symbol','?'))
    print(f'  {str(s.get("signal_id","?"))[:12]} {sym:18s} dir={s.get("direction","?")} '
          f'risk={s.get("risk_status","?")} ts={str(s.get("timestamp","?"))[:19]}')
PYEOF

# ── 7. RISK STATE ─────────────────────────────────────────────────────────────
printf '\n── 7. Risk State ────────────────────────────────────────\n' | tee -a "$LOG"
RISK=$(get_safe "/api/v1/risk/state" "risk/state")
echo "$RISK" | fmt "
r=d
halted = r.get('emergency_halt_active', False)
print(f'  halt_active:       {halted}')
print(f'  halt_reason:       {r.get(\"emergency_halt_reason\",\"none\")}')
print(f'  realized_pnl:      {r.get(\"realized_pnl\",\"?\")}')
print(f'  trade_count:       {r.get(\"trade_count\",\"?\")} / {r.get(\"max_daily_trades\",\"?\")}')
print(f'  open_positions:    {r.get(\"open_positions_count\",\"?\")}')
print(f'  consec_losses:     {r.get(\"consecutive_losses\",\"?\")}')
print(f'  loss_ceiling_hit:  {r.get(\"loss_ceiling_breached\",\"?\")}')
print(f'  cooldown_active:   {r.get(\"cooldown_active\",\"?\")}')
if r.get('cooldown_until'): print(f'  cooldown_until:    {r.get(\"cooldown_until\")}')
" | tee -a "$LOG"

# ── 8. PAPER ACCOUNT + POSITIONS (SESSION / IND_CHECK / POST) ────────────────
if [ "$PHASE" = "IND_CHECK" ] || [ "$PHASE" = "SESSION" ] || [ "$PHASE" = "POST" ]; then
    printf '\n── 8. Paper Account ─────────────────────────────────────\n' | tee -a "$LOG"
    PAPER=$(get_safe "/api/v1/trading/paper/account" "trading/paper/account")
    echo "$PAPER" | fmt "
p=d
print(f'  starting_cash:   {p.get(\"starting_cash\",\"?\")}')
print(f'  cash:            {p.get(\"cash\",\"?\")}')
print(f'  realized_pnl:    {p.get(\"realized_pnl\",\"?\")}')
print(f'  unrealized_pnl:  {p.get(\"unrealized_pnl\",\"?\")}')
print(f'  fill_count:      {p.get(\"fill_count\",\"?\")}')
print(f'  open_positions:  {p.get(\"open_positions\",\"?\")}')
print(f'  execution_mode:  {p.get(\"execution_mode_active\",\"?\")}')
" | tee -a "$LOG"
    printf '\n── 8b. Open Positions ────────────────────────────────────\n' | tee -a "$LOG"
    POS=$(get_safe "/api/v1/trading/positions" "trading/positions")
    echo "$POS" | python3 -c "
import json, sys
d = json.load(sys.stdin)
positions = d if isinstance(d, list) else d.get('positions', [])
print(f'  open_positions: {len(positions)}')
for p in positions:
    sym = str(p.get('symbol','?'))
    print(f'  {sym:24s} qty={p.get(\"quantity\",\"?\")} entry={p.get(\"entry_price\",\"?\")} upnl={p.get(\"unrealized_pnl\",\"?\")}')
" 2>/dev/null | tee -a "$LOG" || echo "  (positions parse error)" | tee -a "$LOG"
fi

# ── 9. FAILURE MODE CLASSIFICATION ───────────────────────────────────────────
printf '\n── 9. Failure Mode Classification ───────────────────────\n' | tee -a "$LOG"
python3 - <<PYEOF 2>/dev/null | tee -a "$LOG"
import json, subprocess, datetime

IST_OFF = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
now = datetime.datetime.now(IST_OFF)
hhmm = now.hour * 100 + now.minute

def fetch(path):
    r = subprocess.run(['curl','-sf','--max-time','5','${BASE}'+path], capture_output=True, text=True)
    try: return json.loads(r.stdout) if r.stdout.strip() else {}
    except: return {}

pipe  = fetch("/pipeline/status")
pref  = fetch("/api/v1/preflight/status")
risk  = fetch("/api/v1/risk/state")
dhan  = fetch("/api/v1/brokers/dhan/status")
sigs_raw = fetch("/api/v1/trading/signals")
paper = fetch("/api/v1/trading/paper/account")

consumed  = pipe.get("consumed_ticks", 0)
halted    = risk.get("emergency_halt_active", False)
cooldown  = risk.get("cooldown_active", False)
ceil_hit  = risk.get("loss_ceiling_breached", False)
trades    = risk.get("trade_count", 0)
max_tr    = risk.get("max_daily_trades", 5)
dhan_ok   = dhan.get("is_connected", False)
fill_cnt  = paper.get("fill_count", 0)
sigs      = sigs_raw if isinstance(sigs_raw, list) else sigs_raw.get('signals',[])
approved  = [s for s in sigs if s.get('risk_status') == 'APPROVED_FOR_EXECUTION']
rejected  = [s for s in sigs if s.get('risk_status') not in ('APPROVED_FOR_EXECUTION', None,'')]

pcr_status = "UNKNOWN"
for c in pref.get("checks", []):
    if c.get("name") == "pcr_poller":
        pcr_status = c.get("status","UNKNOWN")
        break

print(f"  Snapshot at {now.strftime('%H:%M:%S')} IST")
print(f"  consumed_ticks={consumed}  dhan_ok={dhan_ok}  pcr={pcr_status}")
print(f"  halted={halted}  cooldown={cooldown}  ceil_hit={ceil_hit}  trades={trades}/{max_tr}")
print(f"  sigs_total={len(sigs)}  approved={len(approved)}  rejected={len(rejected)}  fills={fill_cnt}")
print()

if hhmm < 921:
    print("  ℹ  Stage analysis available after 09:21 IST")
else:
    # Stage 1: Market data
    if consumed == 0 and not dhan_ok:
        print("  ❌ STAGE 1 — market-data failure: Dhan WS disconnected AND zero ticks")
    elif consumed == 0 and dhan_ok:
        print("  ❌ STAGE 1 — market-data failure: Dhan connected but zero ticks consumed")
        print("     Check: subscription not acked, consumer paused, or market session clock wrong")
    else:
        print(f"  ✅ STAGE 1 — market data: ticks flowing (consumed={consumed})")

    # Stage 2: PCR — but check for stale preflight first
    if hhmm >= 926:
        ran_at = pref.get("ran_at", "")
        pref_stale = False
        try:
            import datetime as _dt2
            IST2 = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            ran_dt = _dt2.datetime.fromisoformat(ran_at.replace('Z','+00:00')) if ran_at else None
            if ran_dt:
                ran_ist = ran_dt.astimezone(IST2)
                # If preflight ran before market open, PENDING/FAIL may be stale
                pref_stale = (ran_ist.hour * 100 + ran_ist.minute) < 915
        except Exception:
            pass
        if pcr_status == "FAIL" and not pref_stale:
            print("  ❌ STAGE 2 — PCR blocked: all option signals suppressed")
            print("     Missing PCR data for NIFTY/BANKNIFTY. Fetch loop may have failed.")
        elif pcr_status == "FAIL" and pref_stale:
            print("  ⚠  STAGE 2 — PCR FAIL but preflight snapshot is stale (pre-market)")
            print(f"     Snapshot from {str(ran_at)[:19]} — run POST /api/v1/preflight/run for current state")
        elif pcr_status == "PASS":
            print("  ✅ STAGE 2 — PCR: data fresh, option signals unblocked")
        elif pcr_status == "PENDING" and pref_stale:
            print(f"  ℹ  STAGE 2 — PCR PENDING but from stale pre-market snapshot (ran_at={str(ran_at)[:19]})")
            print("     Check /pipeline/status → pcr_stale_symbols for actual PCR state")
        else:
            print(f"  ⚠  STAGE 2 — PCR status={pcr_status} (unexpected at this time)")

    # Stage 3: Risk gates
    risk_blocked = halted or cooldown or ceil_hit or (trades >= max_tr)
    if halted:   print("  ❌ STAGE 3 — risk gate: emergency halt active")
    if cooldown: print("  ❌ STAGE 3 — risk gate: cooldown active")
    if ceil_hit: print("  ❌ STAGE 3 — risk gate: daily loss ceiling breached")
    if trades >= max_tr:
        print(f"  ❌ STAGE 3 — risk gate: max daily trades reached ({trades}/{max_tr})")
    if not risk_blocked:
        print("  ✅ STAGE 3 — risk gates: all clear")

    # Stage 4: Paper execution
    if len(approved) > 0 and fill_cnt == 0 and hhmm >= 940:
        print(f"  ❌ STAGE 4 — paper execution: {len(approved)} approved signal(s) but zero fills")
        print("     Paper order book may not be processing. Inspect order queue.")
    elif fill_cnt > 0:
        print(f"  ✅ STAGE 4 — paper execution: {fill_cnt} fill(s) recorded")
    else:
        print(f"  ℹ  STAGE 4 — paper execution: no approved signals yet (fills=0)")

    # EQ symbol mismatch check — Dhan delivers EQ ticks as NSE:SYMBOL-EQ
    import subprocess as _sp
    ticks_r = _sp.run(['curl','-sf','--max-time','5','${BASE}/api/v1/market/ticks'], capture_output=True, text=True)
    try:
        tstore = json.loads(ticks_r.stdout) if ticks_r.stdout.strip() else {}
        tlist = list(tstore.values()) if isinstance(tstore, dict) else (tstore if isinstance(tstore, list) else [])
        nse_pfx_count = sum(1 for t in tlist if str(t.get('symbol','')).startswith('NSE:'))
    except Exception:
        nse_pfx_count = 0
    if nse_pfx_count > 0 and len(sigs) == 0:
        print(f"  ❌ ROOT CAUSE: {nse_pfx_count} EQ ticks stored as NSE:SYMBOL-EQ (e.g. NSE:RELIANCE-EQ)")
        print(f"     Pipeline subscribes to bare SYMBOL names — these ticks are NEVER routed to EQ calculators")
        print(f"     AUTO_STOCK_OPTIONS requires EQ indicator data → zero evaluation → zero signals")
        print(f"     Fix: normalize Dhan tick symbols before TickBuffer routing (strip 'NSE:' prefix and '-EQ' suffix)")

    # Zero-trade distinction (POST phase only)
    if hhmm >= 1530:
        print()
        if fill_cnt == 0:
            if len(sigs) == 0:
                print("  📊 SESSION RESULT: Zero trades — no signals generated")
                if nse_pfx_count > 0:
                    print("     PRIMARY CAUSE: EQ symbol mismatch (NSE:SYMBOL-EQ vs bare SYMBOL)")
                else:
                    print("     Cause candidates: PCR never became fresh, indicators never warmed,")
                    print("     strategy conditions never met (valid no-signal day)")
            elif len(approved) == 0 and len(rejected) > 0:
                print(f"  📊 SESSION RESULT: Zero trades — {len(rejected)} signal(s) generated but ALL risk-rejected")
                print("     Cause: risk gate rejection, NOT a strategy/PCR/indicator failure")
            elif len(approved) > 0:
                print(f"  📊 SESSION RESULT: Zero trades — {len(approved)} approved signal(s) but paper execution FAILED")
                print("     Cause: paper order book not processing fills (confirmed defect)")
        else:
            print(f"  📊 SESSION RESULT: {fill_cnt} paper fill(s) executed")
PYEOF

# ── 10. SUMMARY ──────────────────────────────────────────────────────────────
printf '\n── Summary ──────────────────────────────────────────────\n' | tee -a "$LOG"
printf '  timestamp:       %s\n' "$IST_STR" | tee -a "$LOG"
printf '  phase:           %s\n' "$PHASE"   | tee -a "$LOG"
printf '  consumed_ticks:  %s\n' "$CONSUMED" | tee -a "$LOG"
printf '  pipeline_ok:     %s\n' "$PIPE_OK" | tee -a "$LOG"
printf '  dhan_connected:  %s\n' "$DHAN_OK" | tee -a "$LOG"
printf '  log:             %s\n' "$LOG"     | tee -a "$LOG"
printf '\n' | tee -a "$LOG"
