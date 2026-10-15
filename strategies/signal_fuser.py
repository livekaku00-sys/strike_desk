"""
Strike Desk V4 - Signal Fusion (S13)

PDF status: Strike_Desk_Upgrade_Prompt_Series.pdf unavailable.
ALL thresholds, weights, tie-breaking rules, and confidence semantics below
are PROVISIONAL — derived from the existing codebase architecture.
They are marked [PROVISIONAL] in inline comments and must be reviewed against
the verified specification before use beyond testing.

Responsibility
--------------
Combines directional votes from multiple `Signal` instances for the same
instrument into a single `FusedSignal`.  The fuser is stateless, deterministic,
and completely decoupled from broker adapters, order management, and execution.

CRITICAL NOTICE — Agreement Score
-----------------------------------
`agreement_score` is an AGREEMENT SCORE, not a calibrated probability.
It measures what fraction of valid directional votes agreed with the winning
direction. A score of 80.0 means 4 out of 5 directional votes agreed.
It does NOT represent an 80 % probability of the trade being profitable.

Input contract
--------------
- `signals`      : List[Signal] — any combination of asset classes / strategies.
                   Signals for a different (symbol, exchange, asset_class) than
                   the reference instrument are EXCLUDED and counted in
                   `rejected_votes`.  The reference instrument is taken from the
                   first non-empty signal in the list.  If no signals are
                   provided, `insufficient_data` is set to True and
                   `fused_direction` is NEUTRAL.
- `regime_result`: Optional[RegimeResult] — S12 classifier output.  Used only
                   for informational regime alignment; it never overrides the
                   direction derived from signal votes.

Output contract
---------------
- `fused_direction`        : BULLISH | BEARISH | NEUTRAL | CONFLICT
- `agreement_score`        : 0.0–100.0 (fraction of directional votes that
                             agreed with the winning side × 100).
                             50.0 for CONFLICT (exact tie); 0.0 for NEUTRAL.
- `bullish_votes`          : count of valid LONG signals that voted.
- `bearish_votes`          : count of valid SHORT signals that voted.
- `neutral_votes`          : valid signals with neither LONG nor SHORT
                             (should not occur with current Direction enum but
                             reserved for future extension).
- `rejected_votes`         : signals excluded (wrong instrument, stale, Tier-C,
                             or already-expired at fusion time).
- `contributing_signal_ids`: signal_ids of all signals that cast a vote.
- `regime`                 : RegimeLabel from `regime_result`, or None.
- `regime_aligned`         : True if regime supports direction; False if it
                             opposes; None if regime is neutral / unavailable or
                             fused direction is NEUTRAL / CONFLICT.
- `insufficient_data`      : True when valid votes < MIN_VOTES [PROVISIONAL=1].
- `reason`                 : Human-readable classification summary.
- `fused_at`               : UTC datetime of fusion call.

Fusion rules — tie-breaking (PROVISIONAL)
------------------------------------------
1. LONG votes > SHORT votes  → BULLISH
2. SHORT votes > LONG votes  → BEARISH
3. LONG votes == SHORT votes, both > 0 → CONFLICT (deterministic; no coin flip)
4. No directional votes (0 LONG + 0 SHORT) → NEUTRAL

Signal validity (PROVISIONAL)
-------------------------------
Signals are excluded (→ `rejected_votes`) when:
- Already expired at the time of the `fuse()` call.
- Older than STALE_THRESHOLD_SECONDS relative to `_now`.  [PROVISIONAL=60s]
- confidence_tier == ConfidenceTier.C (below actionable threshold). [PROVISIONAL]
- symbol/exchange/asset_class does not match the reference instrument.

S12 compatibility note
-----------------------
`regime_result` may carry `RegimeLabel.RANGING` even when `bandwidth` was None
at classification time (S12-P3 limitation).  Do not treat `sufficient_data=True`
as proof that every secondary indicator was ready.  This module does not attempt
to re-classify the regime; it uses the label as-is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import logging

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal
from pipeline.regime_classifier import RegimeLabel, RegimeResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Output enum
# ─────────────────────────────────────────────────────────────────────────────

class FusedDirection(str, Enum):
    BULLISH = "BULLISH"   # majority LONG votes
    BEARISH = "BEARISH"   # majority SHORT votes
    NEUTRAL = "NEUTRAL"   # no directional votes
    CONFLICT = "CONFLICT" # tied LONG vs SHORT


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class FusedSignal:
    """
    Result of a single SignalFuser.fuse() call.

    Fields
    ------
    symbol                  : Instrument symbol (upper-case).
    exchange                : Exchange enum value.
    asset_class             : AssetClass enum value.
    fused_direction         : Majority vote outcome.
    agreement_score         : 0–100 fraction of directional votes that agreed
                              with the winning direction.  AGREEMENT SCORE ONLY —
                              NOT a probability of profit.
    bullish_votes           : Count of valid LONG-direction inputs.
    bearish_votes           : Count of valid SHORT-direction inputs.
    neutral_votes           : Count of valid inputs with no direction.
    rejected_votes          : Count of excluded inputs (stale/expired/Tier-C/
                              wrong instrument).
    contributing_signal_ids : signal_ids of signals that cast a vote.
    regime                  : RegimeLabel if a RegimeResult was supplied, else None.
    regime_aligned          : True/False/None (see module docstring).
    insufficient_data       : True when valid votes < MIN_VOTES.
    reason                  : Human-readable summary.
    fused_at                : UTC datetime of this fusion call.
    """
    symbol: str
    exchange: Exchange
    asset_class: AssetClass
    fused_direction: FusedDirection
    agreement_score: float
    bullish_votes: int
    bearish_votes: int
    neutral_votes: int
    rejected_votes: int
    contributing_signal_ids: List[str]
    regime: Optional[RegimeLabel]
    regime_aligned: Optional[bool]
    insufficient_data: bool
    reason: str
    fused_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.fused_at.tzinfo is None:
            self.fused_at = self.fused_at.replace(tzinfo=timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value if hasattr(self.exchange, "value") else str(self.exchange),
            "asset_class": self.asset_class.value if hasattr(self.asset_class, "value") else str(self.asset_class),
            "fused_direction": self.fused_direction.value,
            "agreement_score": self.agreement_score,
            "bullish_votes": self.bullish_votes,
            "bearish_votes": self.bearish_votes,
            "neutral_votes": self.neutral_votes,
            "rejected_votes": self.rejected_votes,
            "contributing_signal_ids": list(self.contributing_signal_ids),
            "regime": self.regime.value if self.regime is not None else None,
            "regime_aligned": self.regime_aligned,
            "insufficient_data": self.insufficient_data,
            "reason": self.reason,
            "fused_at": self.fused_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Fuser
# ─────────────────────────────────────────────────────────────────────────────

class SignalFuser:
    """
    Stateless, deterministic signal fusion component.

    All configuration thresholds are PROVISIONAL (PDF unavailable).  Override
    the class-level constants to adjust behaviour without subclassing.
    """

    # [PROVISIONAL] Minimum valid votes required before the result is
    # considered data-sufficient.  If fewer votes are available,
    # `insufficient_data=True` is set (the direction is still computed from
    # whatever votes exist).
    MIN_VOTES: int = 1

    # [PROVISIONAL] Signals older than this many seconds relative to `_now`
    # are treated as stale and excluded (rejected_votes).
    STALE_THRESHOLD_SECONDS: int = 60

    # [PROVISIONAL] Tier-C signals are below the actionable threshold and are
    # excluded from voting.  Set to False to include all tiers.
    EXCLUDE_TIER_C: bool = True

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_direction(direction: Any) -> Optional[Direction]:
        """Coerce direction to Direction enum; return None if unrecognised."""
        if isinstance(direction, Direction):
            return direction
        if direction is None:
            return None
        d = str(direction).strip().upper()
        if d in ("LONG", "BUY", "BULLISH"):
            return Direction.LONG
        if d in ("SHORT", "SELL", "BEARISH"):
            return Direction.SHORT
        return None

    @staticmethod
    def _regime_alignment(
        fused_direction: FusedDirection,
        regime: Optional[RegimeLabel],
    ) -> Optional[bool]:
        """
        Determines whether the market regime supports the fused direction.

        PROVISIONAL mapping:
          TRENDING_UP   + BULLISH → True   (regime confirms direction)
          TRENDING_DOWN + BEARISH → True
          TRENDING_UP   + BEARISH → False  (regime opposes direction)
          TRENDING_DOWN + BULLISH → False
          All other combinations  → None   (neutral / N/A)
        """
        if fused_direction in (FusedDirection.NEUTRAL, FusedDirection.CONFLICT):
            return None
        if regime is None:
            return None
        if regime == RegimeLabel.TRENDING_UP:
            if fused_direction == FusedDirection.BULLISH:
                return True
            if fused_direction == FusedDirection.BEARISH:
                return False
        if regime == RegimeLabel.TRENDING_DOWN:
            if fused_direction == FusedDirection.BEARISH:
                return True
            if fused_direction == FusedDirection.BULLISH:
                return False
        # RANGING, VOLATILE, UNKNOWN → neutral
        return None

    # ── public interface ─────────────────────────────────────────────────────

    @classmethod
    def fuse(
        cls,
        signals: List[Signal],
        regime_result: Optional[RegimeResult] = None,
        *,
        _now: Optional[datetime] = None,
    ) -> FusedSignal:
        """
        Fuse a list of Signals into a single FusedSignal.

        Parameters
        ----------
        signals       : Signals to fuse (may be from different strategies).
                        May be empty — `insufficient_data=True` is returned.
        regime_result : Optional S12 RegimeResult for context annotation.
        _now          : Override the current time for deterministic tests.
                        Production callers should omit this.

        Returns
        -------
        FusedSignal
        """
        now = _now if _now is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        regime_label: Optional[RegimeLabel] = (
            regime_result.label if regime_result is not None else None
        )

        # ── Determine reference instrument from first valid signal ────────
        ref_symbol: Optional[str] = None
        ref_exchange: Optional[Exchange] = None
        ref_asset_class: Optional[AssetClass] = None

        for s in signals:
            sym = s.symbol.strip().upper() if s.symbol else None
            if sym:
                ref_symbol = sym
                ref_exchange = s.exchange
                ref_asset_class = s.asset_class
                break

        # No signals at all — return minimal NEUTRAL result with placeholder
        if ref_symbol is None:
            return FusedSignal(
                symbol="",
                exchange=Exchange.NSE,
                asset_class=AssetClass.STOCK_CASH,
                fused_direction=FusedDirection.NEUTRAL,
                agreement_score=0.0,
                bullish_votes=0,
                bearish_votes=0,
                neutral_votes=0,
                rejected_votes=0,
                contributing_signal_ids=[],
                regime=regime_label,
                regime_aligned=None,
                insufficient_data=True,
                reason="No signals provided.",
                fused_at=now,
            )

        # ── Filter and vote ───────────────────────────────────────────────
        bullish_votes = 0
        bearish_votes = 0
        neutral_votes = 0
        rejected_votes = 0
        contributing_ids: List[str] = []
        rejection_reasons: List[str] = []

        for sig in signals:
            sig_sym = sig.symbol.strip().upper() if sig.symbol else ""

            # Wrong instrument
            if (
                sig_sym != ref_symbol
                or sig.exchange != ref_exchange
                or sig.asset_class != ref_asset_class
            ):
                rejected_votes += 1
                rejection_reasons.append(
                    f"{sig.signal_id[:8]}: wrong_instrument({sig_sym}/{sig.asset_class})"
                )
                continue

            # Already expired
            if sig.is_expired(now):
                rejected_votes += 1
                rejection_reasons.append(f"{sig.signal_id[:8]}: expired")
                continue

            # Stale (beyond stale threshold even if not formally expired)
            sig_ts = sig.timestamp
            if sig_ts.tzinfo is None:
                sig_ts = sig_ts.replace(tzinfo=timezone.utc)
            age_seconds = (now - sig_ts).total_seconds()
            if age_seconds > cls.STALE_THRESHOLD_SECONDS:
                rejected_votes += 1
                rejection_reasons.append(
                    f"{sig.signal_id[:8]}: stale({age_seconds:.0f}s>{cls.STALE_THRESHOLD_SECONDS}s)"
                )
                continue

            # Below actionable tier threshold [PROVISIONAL]
            if cls.EXCLUDE_TIER_C and sig.confidence_tier == ConfidenceTier.C:
                rejected_votes += 1
                rejection_reasons.append(f"{sig.signal_id[:8]}: tier_C")
                continue

            # Cast vote
            direction = cls._normalize_direction(sig.direction)
            contributing_ids.append(sig.signal_id)

            if direction == Direction.LONG:
                bullish_votes += 1
            elif direction == Direction.SHORT:
                bearish_votes += 1
            else:
                neutral_votes += 1

        # ── Compute fused direction ───────────────────────────────────────
        total_directional = bullish_votes + bearish_votes
        total_votes = total_directional + neutral_votes

        if total_directional == 0:
            fused_direction = FusedDirection.NEUTRAL
            agreement_score = 0.0
        elif bullish_votes > bearish_votes:
            fused_direction = FusedDirection.BULLISH
            agreement_score = round((bullish_votes / total_directional) * 100.0, 1)
        elif bearish_votes > bullish_votes:
            fused_direction = FusedDirection.BEARISH
            agreement_score = round((bearish_votes / total_directional) * 100.0, 1)
        else:
            # Exact tie — deterministic CONFLICT, no coin flip [PROVISIONAL]
            fused_direction = FusedDirection.CONFLICT
            agreement_score = 50.0

        insufficient_data = total_votes < cls.MIN_VOTES

        # ── Regime alignment ──────────────────────────────────────────────
        regime_aligned = cls._regime_alignment(fused_direction, regime_label)

        # ── Build reason string ───────────────────────────────────────────
        regime_str = regime_label.value if regime_label is not None else "N/A"
        aligned_str = (
            str(regime_aligned) if regime_aligned is not None else "N/A"
        )
        reason_parts = [
            f"Direction={fused_direction.value}",
            f"agreement={agreement_score:.1f}%",
            f"votes(B={bullish_votes},b={bearish_votes},n={neutral_votes},R={rejected_votes})",
            f"regime={regime_str}",
            f"regime_aligned={aligned_str}",
        ]
        if insufficient_data:
            reason_parts.append(f"insufficient_data(votes={total_votes}<min={cls.MIN_VOTES})")
        if rejection_reasons:
            reason_parts.append("rejected=[" + "; ".join(rejection_reasons[:3]) + "]")
        reason = "; ".join(reason_parts)

        logger.debug(
            "[SignalFuser] %s/%s → %s | agreement=%.1f%% | B=%d b=%d n=%d R=%d | regime=%s aligned=%s",
            ref_symbol,
            ref_asset_class,
            fused_direction.value,
            agreement_score,
            bullish_votes,
            bearish_votes,
            neutral_votes,
            rejected_votes,
            regime_str,
            aligned_str,
        )

        return FusedSignal(
            symbol=ref_symbol,
            exchange=ref_exchange,
            asset_class=ref_asset_class,
            fused_direction=fused_direction,
            agreement_score=agreement_score,
            bullish_votes=bullish_votes,
            bearish_votes=bearish_votes,
            neutral_votes=neutral_votes,
            rejected_votes=rejected_votes,
            contributing_signal_ids=contributing_ids,
            regime=regime_label,
            regime_aligned=regime_aligned,
            insufficient_data=insufficient_data,
            reason=reason,
            fused_at=now,
        )
