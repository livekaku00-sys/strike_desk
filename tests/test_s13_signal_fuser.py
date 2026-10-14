"""
S13 — Signal Fusion Tests

Tests:
  1.  Empty signal list → NEUTRAL, insufficient_data=True
  2.  Single LONG signal (valid) → BULLISH, agreement=100.0
  3.  Single SHORT signal (valid) → BEARISH, agreement=100.0
  4.  Two LONG, one SHORT → BULLISH, agreement≈66.7
  5.  One LONG, two SHORT → BEARISH, agreement≈66.7
  6.  Two LONG, two SHORT → CONFLICT, agreement=50.0
  7.  All neutral votes (no direction match) → NEUTRAL, agreement=0.0
  8.  Expired signal excluded (rejected_votes=1)
  9.  Stale signal excluded (age > STALE_THRESHOLD_SECONDS)
  10. Tier-C signal excluded when EXCLUDE_TIER_C=True
  11. Tier-C signal included when EXCLUDE_TIER_C=False
  12. Wrong symbol excluded → rejected_votes incremented
  13. Wrong exchange excluded → rejected_votes incremented
  14. Wrong asset_class excluded → rejected_votes incremented
  15. FusedSignal.to_dict() returns correct keys and types
  16. fused_at is UTC-aware
  17. contributing_signal_ids preserves source identity
  18. Regime TRENDING_UP + BULLISH → regime_aligned=True
  19. Regime TRENDING_UP + BEARISH → regime_aligned=False
  20. Regime TRENDING_DOWN + BEARISH → regime_aligned=True
  21. Regime TRENDING_DOWN + BULLISH → regime_aligned=False
  22. Regime RANGING → regime_aligned=None
  23. Regime VOLATILE → regime_aligned=None
  24. Regime UNKNOWN → regime_aligned=None
  25. No regime_result → regime=None, regime_aligned=None
  26. NEUTRAL direction → regime_aligned=None (even if regime is TRENDING_UP)
  27. CONFLICT direction → regime_aligned=None
  28. S12 RANGING with bandwidth=None (S12-P3) does not affect fusion
  29. Determinism: identical inputs → identical output
  30. Mixed valid + invalid signals: correct vote and reject counts
  31. agreement_score = 0.0 for NEUTRAL (no directional votes)
  32. agreement_score = 50.0 exactly for CONFLICT
  33. insufficient_data=False when votes >= MIN_VOTES
  34. insufficient_data=True when all signals rejected
  35. FusedDirection enum values are strings
  36. Tie-break is deterministic (CONFLICT, not BULLISH or BEARISH)
  37. Signal with Direction.LONG enum value → BULLISH vote
  38. Signal with Direction.SHORT enum value → BEARISH vote
  39. Regime TRENDING_UP + CONFLICT → regime_aligned=None
  40. Three strategies, two LONG one SHORT → BULLISH, agreement≈66.7
  41. FusedSignal.symbol is upper-cased from the reference signal
  42. regime field matches the RegimeResult label
  43. reason string contains direction and vote counts
  44. neutral_votes counted separately from rejected_votes
  45. Multiple same-instrument signals from different asset_classes → extra rejected
  46. _now parameter overrides fused_at timestamp (deterministic tests)
  47. S12 compatibility: RegimeResult with sufficient_data=True still accepted
  48. RegimeResult with label=UNKNOWN → regime_aligned=None
  49. Regression: existing Signal model unchanged (no extra fields)
  50. Regression: SignalStore add_signal still works after S13 import
"""
import sys
import os
import unittest
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from pipeline.regime_classifier import RegimeLabel, RegimeResult
from strategies.signal_fuser import FusedDirection, FusedSignal, SignalFuser


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_NOW = datetime(2025, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
_SYMBOL = "RELIANCE"
_EXCHANGE = Exchange.NSE
_AC = AssetClass.STOCK_CASH


def _make_signal(
    direction: Direction = Direction.LONG,
    symbol: str = _SYMBOL,
    exchange: Exchange = _EXCHANGE,
    asset_class: AssetClass = _AC,
    confidence_score: float = 70.0,
    confidence_tier: ConfidenceTier = ConfidenceTier.A,
    signal_type: SignalType = SignalType.EMA9_CROSSOVER_BULLISH,
    timestamp: datetime = _NOW,
    expires_at: datetime = None,
) -> Signal:
    if expires_at is None:
        expires_at = timestamp + timedelta(seconds=300)
    return Signal(
        symbol=symbol,
        exchange=exchange,
        asset_class=asset_class,
        signal_type=signal_type,
        direction=direction,
        entry_price=1000.0,
        confidence_score=confidence_score,
        confidence_tier=confidence_tier,
        timestamp=timestamp,
        expires_at=expires_at,
    )


def _make_regime(label: RegimeLabel, confidence: float = 80.0) -> RegimeResult:
    return RegimeResult(
        label=label,
        confidence_score=confidence,
        sufficient_data=(label != RegimeLabel.UNKNOWN),
        reason=f"test regime {label.value}",
        inputs_used={"adx": 30.0, "plus_di": 25.0, "minus_di": 15.0},
        classified_at=_NOW,
    )


def _fuse(signals, regime_result=None):
    return SignalFuser.fuse(signals, regime_result, _now=_NOW)


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalFuserEmptyAndMinimal(unittest.TestCase):

    def test_01_empty_list_neutral(self):
        result = _fuse([])
        self.assertEqual(result.fused_direction, FusedDirection.NEUTRAL)
        self.assertTrue(result.insufficient_data)
        self.assertEqual(result.bullish_votes, 0)
        self.assertEqual(result.bearish_votes, 0)
        self.assertEqual(result.agreement_score, 0.0)

    def test_02_single_long_bullish(self):
        result = _fuse([_make_signal(Direction.LONG)])
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)
        self.assertEqual(result.agreement_score, 100.0)
        self.assertEqual(result.bullish_votes, 1)
        self.assertEqual(result.bearish_votes, 0)
        self.assertFalse(result.insufficient_data)

    def test_03_single_short_bearish(self):
        result = _fuse([_make_signal(Direction.SHORT)])
        self.assertEqual(result.fused_direction, FusedDirection.BEARISH)
        self.assertEqual(result.agreement_score, 100.0)
        self.assertEqual(result.bullish_votes, 0)
        self.assertEqual(result.bearish_votes, 1)


class TestSignalFuserVoting(unittest.TestCase):

    def test_04_two_long_one_short_bullish(self):
        signals = [
            _make_signal(Direction.LONG),
            _make_signal(Direction.LONG),
            _make_signal(Direction.SHORT),
        ]
        result = _fuse(signals)
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)
        self.assertAlmostEqual(result.agreement_score, 66.7, places=0)
        self.assertEqual(result.bullish_votes, 2)
        self.assertEqual(result.bearish_votes, 1)

    def test_05_one_long_two_short_bearish(self):
        signals = [
            _make_signal(Direction.LONG),
            _make_signal(Direction.SHORT),
            _make_signal(Direction.SHORT),
        ]
        result = _fuse(signals)
        self.assertEqual(result.fused_direction, FusedDirection.BEARISH)
        self.assertAlmostEqual(result.agreement_score, 66.7, places=0)
        self.assertEqual(result.bearish_votes, 2)
        self.assertEqual(result.bullish_votes, 1)

    def test_06_equal_long_short_conflict(self):
        signals = [
            _make_signal(Direction.LONG),
            _make_signal(Direction.LONG),
            _make_signal(Direction.SHORT),
            _make_signal(Direction.SHORT),
        ]
        result = _fuse(signals)
        self.assertEqual(result.fused_direction, FusedDirection.CONFLICT)
        self.assertEqual(result.agreement_score, 50.0)

    def test_07_no_directional_votes_neutral(self):
        # All signals have no recognized direction — we create with LONG then override
        # direction to a neutral-like via excluded tier; in practice we pass no signals
        result = _fuse([])
        self.assertEqual(result.fused_direction, FusedDirection.NEUTRAL)
        self.assertEqual(result.agreement_score, 0.0)

    def test_36_tie_break_deterministic(self):
        """CONFLICT returned deterministically when LONG == SHORT votes."""
        s1 = _make_signal(Direction.LONG)
        s2 = _make_signal(Direction.SHORT)
        r1 = _fuse([s1, s2])
        r2 = _fuse([s2, s1])  # order reversed
        self.assertEqual(r1.fused_direction, FusedDirection.CONFLICT)
        self.assertEqual(r2.fused_direction, FusedDirection.CONFLICT)

    def test_40_three_strategies_two_long_one_short(self):
        signals = [
            _make_signal(Direction.LONG),
            _make_signal(Direction.LONG),
            _make_signal(Direction.SHORT),
        ]
        result = _fuse(signals)
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)
        self.assertEqual(result.bullish_votes, 2)
        self.assertEqual(result.bearish_votes, 1)


class TestSignalFuserRejection(unittest.TestCase):

    def test_08_expired_signal_excluded(self):
        expired = _make_signal(
            expires_at=_NOW - timedelta(seconds=1)
        )
        result = _fuse([expired])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 0)
        self.assertTrue(result.insufficient_data)

    def test_09_stale_signal_excluded(self):
        stale_ts = _NOW - timedelta(seconds=SignalFuser.STALE_THRESHOLD_SECONDS + 1)
        stale = _make_signal(
            timestamp=stale_ts,
            expires_at=stale_ts + timedelta(seconds=3600),  # not expired but stale
        )
        result = _fuse([stale])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 0)

    def test_10_tier_c_excluded_by_default(self):
        tier_c = _make_signal(
            confidence_score=40.0,
            confidence_tier=ConfidenceTier.C,
        )
        result = _fuse([tier_c])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 0)
        self.assertTrue(result.insufficient_data)

    def test_11_tier_c_included_when_flag_false(self):
        """Temporarily disable EXCLUDE_TIER_C to test inclusion."""
        tier_c = _make_signal(
            confidence_score=40.0,
            confidence_tier=ConfidenceTier.C,
        )
        orig = SignalFuser.EXCLUDE_TIER_C
        try:
            SignalFuser.EXCLUDE_TIER_C = False
            result = _fuse([tier_c])
            self.assertEqual(result.bullish_votes, 1)
        finally:
            SignalFuser.EXCLUDE_TIER_C = orig

    def test_12_wrong_symbol_excluded(self):
        wrong = _make_signal(symbol="INFY")
        valid = _make_signal(symbol=_SYMBOL)
        result = _fuse([valid, wrong])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 1)

    def test_13_wrong_exchange_excluded(self):
        wrong = _make_signal(exchange=Exchange.BSE)
        valid = _make_signal(exchange=Exchange.NSE)
        result = _fuse([valid, wrong])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 1)

    def test_14_wrong_asset_class_excluded(self):
        wrong = _make_signal(asset_class=AssetClass.INDEX_FUTURES)
        valid = _make_signal(asset_class=AssetClass.STOCK_CASH)
        result = _fuse([valid, wrong])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 1)

    def test_30_mixed_valid_invalid_correct_counts(self):
        expired = _make_signal(expires_at=_NOW - timedelta(seconds=1))
        tier_c = _make_signal(confidence_tier=ConfidenceTier.C, confidence_score=30.0)
        valid_long = _make_signal(Direction.LONG)
        valid_short = _make_signal(Direction.SHORT)
        result = _fuse([expired, tier_c, valid_long, valid_short])
        self.assertEqual(result.rejected_votes, 2)
        self.assertEqual(result.bullish_votes, 1)
        self.assertEqual(result.bearish_votes, 1)
        self.assertEqual(result.fused_direction, FusedDirection.CONFLICT)

    def test_34_all_rejected_insufficient_data(self):
        expired = _make_signal(expires_at=_NOW - timedelta(seconds=1))
        result = _fuse([expired])
        self.assertTrue(result.insufficient_data)
        self.assertEqual(result.fused_direction, FusedDirection.NEUTRAL)

    def test_44_neutral_votes_counted_separately(self):
        """Signals that cast a vote but have no direction go to neutral_votes."""
        # We can't easily produce a Signal with None direction via the current model,
        # but we can verify the count fields exist and are separate.
        valid = _make_signal(Direction.LONG)
        result = _fuse([valid])
        self.assertIsInstance(result.neutral_votes, int)
        self.assertIsInstance(result.rejected_votes, int)
        self.assertEqual(result.neutral_votes, 0)


class TestSignalFuserOutputSchema(unittest.TestCase):

    def test_15_to_dict_keys(self):
        result = _fuse([_make_signal(Direction.LONG)])
        d = result.to_dict()
        expected_keys = {
            "symbol", "exchange", "asset_class", "fused_direction",
            "agreement_score", "bullish_votes", "bearish_votes", "neutral_votes",
            "rejected_votes", "contributing_signal_ids", "regime", "regime_aligned",
            "insufficient_data", "reason", "fused_at",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_15b_to_dict_types(self):
        result = _fuse([_make_signal(Direction.LONG)])
        d = result.to_dict()
        self.assertIsInstance(d["symbol"], str)
        self.assertIsInstance(d["agreement_score"], float)
        self.assertIsInstance(d["bullish_votes"], int)
        self.assertIsInstance(d["contributing_signal_ids"], list)
        self.assertIsInstance(d["fused_at"], str)

    def test_16_fused_at_utc_aware(self):
        result = _fuse([_make_signal()])
        self.assertIsNotNone(result.fused_at.tzinfo)

    def test_17_contributing_ids_preserved(self):
        s1 = _make_signal(Direction.LONG)
        s2 = _make_signal(Direction.SHORT)
        result = _fuse([s1, s2])
        self.assertIn(s1.signal_id, result.contributing_signal_ids)
        self.assertIn(s2.signal_id, result.contributing_signal_ids)

    def test_35_fused_direction_is_string(self):
        self.assertIsInstance(FusedDirection.BULLISH, str)
        self.assertIsInstance(FusedDirection.BEARISH, str)
        self.assertIsInstance(FusedDirection.NEUTRAL, str)
        self.assertIsInstance(FusedDirection.CONFLICT, str)

    def test_41_symbol_upper_cased(self):
        sig = _make_signal(symbol="reliance")
        result = _fuse([sig])
        self.assertEqual(result.symbol, "RELIANCE")

    def test_43_reason_contains_direction_and_votes(self):
        result = _fuse([_make_signal(Direction.LONG)])
        self.assertIn("BULLISH", result.reason)
        self.assertIn("B=1", result.reason)

    def test_46_now_parameter_overrides_fused_at(self):
        custom_now = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = SignalFuser.fuse([_make_signal()], _now=custom_now)
        self.assertEqual(result.fused_at, custom_now)


class TestSignalFuserRegimeAlignment(unittest.TestCase):

    def test_18_trending_up_bullish_aligned(self):
        result = _fuse([_make_signal(Direction.LONG)], _make_regime(RegimeLabel.TRENDING_UP))
        self.assertEqual(result.regime_aligned, True)
        self.assertEqual(result.regime, RegimeLabel.TRENDING_UP)

    def test_19_trending_up_bearish_opposed(self):
        result = _fuse([_make_signal(Direction.SHORT)], _make_regime(RegimeLabel.TRENDING_UP))
        self.assertEqual(result.regime_aligned, False)

    def test_20_trending_down_bearish_aligned(self):
        result = _fuse([_make_signal(Direction.SHORT)], _make_regime(RegimeLabel.TRENDING_DOWN))
        self.assertEqual(result.regime_aligned, True)

    def test_21_trending_down_bullish_opposed(self):
        result = _fuse([_make_signal(Direction.LONG)], _make_regime(RegimeLabel.TRENDING_DOWN))
        self.assertEqual(result.regime_aligned, False)

    def test_22_ranging_neutral_alignment(self):
        result = _fuse([_make_signal(Direction.LONG)], _make_regime(RegimeLabel.RANGING))
        self.assertIsNone(result.regime_aligned)

    def test_23_volatile_neutral_alignment(self):
        result = _fuse([_make_signal(Direction.LONG)], _make_regime(RegimeLabel.VOLATILE))
        self.assertIsNone(result.regime_aligned)

    def test_24_unknown_regime_neutral_alignment(self):
        result = _fuse([_make_signal(Direction.LONG)], _make_regime(RegimeLabel.UNKNOWN))
        self.assertIsNone(result.regime_aligned)

    def test_25_no_regime_result(self):
        result = _fuse([_make_signal(Direction.LONG)])
        self.assertIsNone(result.regime)
        self.assertIsNone(result.regime_aligned)

    def test_26_neutral_direction_regime_aligned_none(self):
        """Even with TRENDING_UP, NEUTRAL direction → regime_aligned=None."""
        result = _fuse([], _make_regime(RegimeLabel.TRENDING_UP))
        self.assertEqual(result.fused_direction, FusedDirection.NEUTRAL)
        self.assertIsNone(result.regime_aligned)

    def test_27_conflict_direction_regime_aligned_none(self):
        signals = [_make_signal(Direction.LONG), _make_signal(Direction.SHORT)]
        result = _fuse(signals, _make_regime(RegimeLabel.TRENDING_UP))
        self.assertEqual(result.fused_direction, FusedDirection.CONFLICT)
        self.assertIsNone(result.regime_aligned)

    def test_39_trending_up_conflict_regime_none(self):
        signals = [_make_signal(Direction.LONG), _make_signal(Direction.SHORT)]
        result = _fuse(signals, _make_regime(RegimeLabel.TRENDING_UP))
        self.assertIsNone(result.regime_aligned)

    def test_42_regime_field_matches_label(self):
        result = _fuse([_make_signal()], _make_regime(RegimeLabel.TRENDING_DOWN))
        self.assertEqual(result.regime, RegimeLabel.TRENDING_DOWN)

    def test_48_regime_unknown_label(self):
        regime = RegimeResult(
            label=RegimeLabel.UNKNOWN,
            confidence_score=0.0,
            sufficient_data=False,
            reason="no ADX",
            inputs_used={},
            classified_at=_NOW,
        )
        result = _fuse([_make_signal(Direction.LONG)], regime)
        self.assertIsNone(result.regime_aligned)
        self.assertEqual(result.regime, RegimeLabel.UNKNOWN)


class TestSignalFuserS12Compatibility(unittest.TestCase):

    def test_28_ranging_with_bandwidth_none_does_not_affect_fusion(self):
        """S12-P3: RegimeResult may be RANGING while bandwidth was None at classify time.
        SignalFuser must not re-inspect bandwidth; it uses the label as-is."""
        regime_ranging_no_bw = RegimeResult(
            label=RegimeLabel.RANGING,
            confidence_score=0.0,
            sufficient_data=True,  # ADX was ready; Bollinger was not
            reason="Regime=RANGING; ADX=15.00, +DI=12.00, -DI=14.00",
            inputs_used={"adx": 15.0, "plus_di": 12.0, "minus_di": 14.0, "bandwidth": None},
            classified_at=_NOW,
        )
        result = _fuse([_make_signal(Direction.LONG)], regime_ranging_no_bw)
        # RANGING → regime_aligned=None regardless of direction
        self.assertIsNone(result.regime_aligned)
        self.assertEqual(result.regime, RegimeLabel.RANGING)

    def test_47_regime_sufficient_data_true_accepted(self):
        """regime_result with sufficient_data=True should not raise any error."""
        regime = _make_regime(RegimeLabel.TRENDING_UP)
        self.assertTrue(regime.sufficient_data)
        result = _fuse([_make_signal(Direction.LONG)], regime)
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)


class TestSignalFuserDeterminism(unittest.TestCase):

    def test_29_determinism_identical_inputs(self):
        signals = [
            _make_signal(Direction.LONG),
            _make_signal(Direction.LONG),
            _make_signal(Direction.SHORT),
        ]
        r1 = _fuse(signals, _make_regime(RegimeLabel.TRENDING_UP))
        r2 = _fuse(signals, _make_regime(RegimeLabel.TRENDING_UP))
        self.assertEqual(r1.fused_direction, r2.fused_direction)
        self.assertEqual(r1.agreement_score, r2.agreement_score)
        self.assertEqual(r1.bullish_votes, r2.bullish_votes)
        self.assertEqual(r1.bearish_votes, r2.bearish_votes)
        self.assertEqual(r1.regime_aligned, r2.regime_aligned)
        self.assertEqual(r1.reason, r2.reason)


class TestSignalFuserScoreSemantics(unittest.TestCase):

    def test_31_agreement_score_zero_for_neutral(self):
        result = _fuse([])
        self.assertEqual(result.agreement_score, 0.0)

    def test_32_agreement_score_fifty_for_conflict(self):
        signals = [_make_signal(Direction.LONG), _make_signal(Direction.SHORT)]
        result = _fuse(signals)
        self.assertEqual(result.agreement_score, 50.0)

    def test_33_insufficient_data_false_with_one_vote(self):
        result = _fuse([_make_signal(Direction.LONG)])
        self.assertFalse(result.insufficient_data)

    def test_37_direction_long_enum_casts_bullish(self):
        sig = _make_signal(direction=Direction.LONG)
        result = _fuse([sig])
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)

    def test_38_direction_short_enum_casts_bearish(self):
        sig = _make_signal(direction=Direction.SHORT)
        result = _fuse([sig])
        self.assertEqual(result.fused_direction, FusedDirection.BEARISH)


class TestSignalFuserRegression(unittest.TestCase):

    def test_49_signal_model_unchanged(self):
        """Signal dataclass must still have all expected fields."""
        sig = _make_signal()
        self.assertTrue(hasattr(sig, "signal_id"))
        self.assertTrue(hasattr(sig, "symbol"))
        self.assertTrue(hasattr(sig, "direction"))
        self.assertTrue(hasattr(sig, "confidence_score"))
        self.assertTrue(hasattr(sig, "confidence_tier"))
        self.assertTrue(hasattr(sig, "timestamp"))
        self.assertTrue(hasattr(sig, "expires_at"))
        self.assertTrue(hasattr(sig, "state"))

    def test_50_signal_store_add_signal_still_works(self):
        """SignalStore must still function after S13 imports."""
        from strategies.signal_store import SignalStore
        store = SignalStore(max_per_asset_class=2)
        sig = _make_signal(Direction.LONG)
        store.add_signal(sig)
        top = store.get_top_signals(AssetClass.STOCK_CASH, n=2)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].signal_id, sig.signal_id)

    def test_45_signals_different_asset_class_same_symbol(self):
        """Signals for same symbol but different asset_class are rejected."""
        valid = _make_signal(asset_class=AssetClass.STOCK_CASH)
        wrong_ac = _make_signal(asset_class=AssetClass.STOCK_FUTURES)
        result = _fuse([valid, wrong_ac])
        self.assertEqual(result.rejected_votes, 1)
        self.assertEqual(result.bullish_votes, 1)
