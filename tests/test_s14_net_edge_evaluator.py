"""
Strike Desk V4 — S14 Net Edge Shadow Evaluator Tests

Tests the NetEdgeEvaluator, NetEdgeConfig, NetEdgeDecision, and NetEdgeResult
components introduced in S14.

All tests are isolated and deterministic.  No live orders are placed or
authorised.  The evaluator is shadow-only; tests verify that it has no
connection to order placement or execution paths.

Fixed reference time: _NOW = 2025-01-15T09:30:00 UTC
"""
import math
import os
import unittest
from datetime import datetime, timezone
from typing import Optional

_NOW = datetime(2025, 1, 15, 9, 30, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helper factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(
    direction: str = "LONG",
    symbol: str = "RELIANCE",
    entry_price: float = 2000.0,
    target_price: Optional[float] = 2050.0,
    stop_loss_price: Optional[float] = 1970.0,
    asset_class: str = "STOCK_CASH",
    exchange: str = "NSE",
    confidence_score: float = 72.0,
):
    from models.instrument import AssetClass, Exchange
    from models.signal import Direction, Signal, SignalType

    return Signal(
        symbol=symbol,
        exchange=Exchange(exchange),
        asset_class=AssetClass(asset_class),
        signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
        direction=Direction(direction),
        entry_price=entry_price,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        confidence_score=confidence_score,
        timestamp=_NOW,
        expires_at=_NOW.replace(second=_NOW.second + 30),
    )


def _cfg(min_net_edge: float = 0.0):
    from strategies.net_edge_evaluator import NetEdgeConfig
    return NetEdgeConfig(min_net_edge=min_net_edge)


def _evaluate(signal, **kwargs):
    from strategies.net_edge_evaluator import NetEdgeEvaluator
    return NetEdgeEvaluator.evaluate(signal, _now=_NOW, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Part A — NetEdgeConfig validation
# ─────────────────────────────────────────────────────────────────────────────

class TestNetEdgeConfig(unittest.TestCase):

    def test_01_default_min_net_edge_zero(self):
        """default() returns min_net_edge=0.0."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        cfg = NetEdgeConfig.default()
        self.assertEqual(cfg.min_net_edge, 0.0)

    def test_02_valid_positive_value(self):
        """Positive finite min_net_edge is accepted."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        cfg = NetEdgeConfig(min_net_edge=10.0)
        self.assertEqual(cfg.min_net_edge, 10.0)

    def test_03_zero_is_valid(self):
        """min_net_edge=0.0 is accepted (boundary)."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        cfg = NetEdgeConfig(min_net_edge=0.0)
        self.assertEqual(cfg.min_net_edge, 0.0)

    def test_04_negative_raises(self):
        """min_net_edge < 0 raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        with self.assertRaises(ValueError):
            NetEdgeConfig(min_net_edge=-1.0)

    def test_05_nan_raises(self):
        """min_net_edge=NaN raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        with self.assertRaises(ValueError):
            NetEdgeConfig(min_net_edge=float("nan"))

    def test_06_inf_raises(self):
        """min_net_edge=Inf raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        with self.assertRaises(ValueError):
            NetEdgeConfig(min_net_edge=float("inf"))

    def test_07_neg_inf_raises(self):
        """min_net_edge=-Inf raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        with self.assertRaises(ValueError):
            NetEdgeConfig(min_net_edge=float("-inf"))

    def test_08_from_env_default(self):
        """from_env() with no env var returns 0.0."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        env_backup = os.environ.pop("MIN_NET_EDGE", None)
        try:
            cfg = NetEdgeConfig.from_env()
            self.assertEqual(cfg.min_net_edge, 0.0)
        finally:
            if env_backup is not None:
                os.environ["MIN_NET_EDGE"] = env_backup

    def test_09_from_env_valid_override(self):
        """from_env() parses a valid float override."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        os.environ["MIN_NET_EDGE"] = "5.5"
        try:
            cfg = NetEdgeConfig.from_env()
            self.assertAlmostEqual(cfg.min_net_edge, 5.5)
        finally:
            del os.environ["MIN_NET_EDGE"]

    def test_10_from_env_non_numeric_raises(self):
        """from_env() with a non-numeric string raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        os.environ["MIN_NET_EDGE"] = "notanumber"
        try:
            with self.assertRaises(ValueError):
                NetEdgeConfig.from_env()
        finally:
            del os.environ["MIN_NET_EDGE"]

    def test_11_from_env_negative_raises(self):
        """from_env() with a negative string raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        os.environ["MIN_NET_EDGE"] = "-1.0"
        try:
            with self.assertRaises(ValueError):
                NetEdgeConfig.from_env()
        finally:
            del os.environ["MIN_NET_EDGE"]

    def test_12_from_env_inf_string_raises(self):
        """from_env() with 'inf' raises ValueError (non-finite)."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        os.environ["MIN_NET_EDGE"] = "inf"
        try:
            with self.assertRaises(ValueError):
                NetEdgeConfig.from_env()
        finally:
            del os.environ["MIN_NET_EDGE"]

    def test_13_config_is_frozen(self):
        """NetEdgeConfig is immutable (frozen dataclass)."""
        from strategies.net_edge_evaluator import NetEdgeConfig
        cfg = NetEdgeConfig(min_net_edge=1.0)
        with self.assertRaises((AttributeError, TypeError)):
            cfg.min_net_edge = 2.0  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# Part B — Gross edge arithmetic
# ─────────────────────────────────────────────────────────────────────────────

class TestGrossEdge(unittest.TestCase):

    def test_14_long_target_above_entry_positive(self):
        """LONG with target > entry yields positive gross edge."""
        sig = _make_signal(direction="LONG", entry_price=2000.0, target_price=2050.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.gross_edge, 50.0, places=4)

    def test_15_long_target_below_entry_negative(self):
        """LONG with target < entry yields negative gross edge."""
        sig = _make_signal(direction="LONG", entry_price=2000.0, target_price=1950.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.gross_edge, -50.0, places=4)

    def test_16_short_entry_above_target_positive(self):
        """SHORT with entry > target yields positive gross edge."""
        sig = _make_signal(direction="SHORT", entry_price=2000.0, target_price=1950.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.gross_edge, 50.0, places=4)

    def test_17_short_entry_below_target_negative(self):
        """SHORT with entry < target yields negative gross edge."""
        sig = _make_signal(direction="SHORT", entry_price=2000.0, target_price=2050.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.gross_edge, -50.0, places=4)

    def test_18_long_equal_prices_zero_edge(self):
        """LONG with target == entry yields gross edge of 0."""
        sig = _make_signal(direction="LONG", entry_price=2000.0, target_price=2000.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.gross_edge, 0.0, places=4)

    def test_19_lot_size_scales_gross_edge(self):
        """lot_size=2 doubles the gross edge."""
        sig = _make_signal(direction="LONG", entry_price=2000.0, target_price=2050.0)
        r1 = _evaluate(sig, lot_size=1)
        r2 = _evaluate(sig, lot_size=2)
        self.assertAlmostEqual(r2.gross_edge, r1.gross_edge * 2, places=4)

    def test_20_large_lot_size(self):
        """lot_size=75 (Nifty-like) produces correct gross edge."""
        sig = _make_signal(
            direction="LONG", asset_class="INDEX_FUTURES",
            entry_price=22000.0, target_price=22100.0,
        )
        res = _evaluate(sig, lot_size=75)
        self.assertAlmostEqual(res.gross_edge, 100.0 * 75, places=4)


# ─────────────────────────────────────────────────────────────────────────────
# Part C — Cost components
# ─────────────────────────────────────────────────────────────────────────────

class TestCostComponents(unittest.TestCase):

    def setUp(self):
        from risk.charge_calculator import ChargeCalculator
        self.CC = ChargeCalculator

    def test_21_brokerage_is_nonnegative(self):
        """deduction_brokerage is >= 0 for all asset classes."""
        for ac in ("STOCK_CASH", "STOCK_FUTURES", "INDEX_FUTURES"):
            sig = _make_signal(asset_class=ac)
            res = _evaluate(sig)
            self.assertGreaterEqual(res.deduction_brokerage, 0.0, ac)

    def test_22_stt_is_nonnegative(self):
        """deduction_stt is >= 0."""
        sig = _make_signal(direction="LONG")
        res = _evaluate(sig)
        self.assertGreaterEqual(res.deduction_stt, 0.0)

    def test_23_exchange_charges_is_nonnegative(self):
        """deduction_exchange_charges is >= 0."""
        sig = _make_signal()
        res = _evaluate(sig)
        self.assertGreaterEqual(res.deduction_exchange_charges, 0.0)

    def test_24_gst_is_nonnegative(self):
        """deduction_gst is >= 0."""
        sig = _make_signal()
        res = _evaluate(sig)
        self.assertGreaterEqual(res.deduction_gst, 0.0)

    def test_25_sebi_charges_is_nonnegative(self):
        """deduction_sebi_charges is >= 0."""
        sig = _make_signal()
        res = _evaluate(sig)
        self.assertGreaterEqual(res.deduction_sebi_charges, 0.0)

    def test_26_total_statutory_equals_sum_of_components(self):
        """total_statutory_charges equals sum of all 5 statutory components."""
        sig = _make_signal()
        res = _evaluate(sig)
        expected = round(
            res.deduction_brokerage
            + res.deduction_stt
            + res.deduction_exchange_charges
            + res.deduction_gst
            + res.deduction_sebi_charges,
            4,
        )
        self.assertAlmostEqual(res.total_statutory_charges, expected, places=4)

    def test_27_fno_brokerage_flat_20_each_leg(self):
        """F&O brokerage is ₹20 per leg = ₹40 round-trip."""
        sig = _make_signal(asset_class="STOCK_FUTURES", entry_price=500.0, target_price=520.0)
        res = _evaluate(sig, lot_size=1)
        self.assertAlmostEqual(res.deduction_brokerage, 40.0, places=4)

    def test_28_equity_intraday_stt_on_sell_leg_only(self):
        """For LONG STOCK_CASH: STT applies on exit (sell) leg only.
        Entry turnover: 2000, exit turnover: 2050.
        STT = 0.025% of exit = 2050 * 0.00025 = 0.5125."""
        sig = _make_signal(direction="LONG", entry_price=2000.0, target_price=2050.0, asset_class="STOCK_CASH")
        res = _evaluate(sig, lot_size=1)
        expected_stt = round(2050.0 * 0.00025, 4)
        self.assertAlmostEqual(res.deduction_stt, expected_stt, places=4)

    def test_29_short_stt_on_entry_sell_leg(self):
        """For SHORT STOCK_CASH: STT applies on entry (sell) leg only.
        Entry turnover: 2000, exit turnover: 1950.
        STT = 0.025% of 2000 = 0.5."""
        sig = _make_signal(direction="SHORT", entry_price=2000.0, target_price=1950.0, asset_class="STOCK_CASH")
        res = _evaluate(sig, lot_size=1)
        expected_stt = round(2000.0 * 0.00025, 4)
        self.assertAlmostEqual(res.deduction_stt, expected_stt, places=4)

    def test_30_turnover_fields_correct(self):
        """entry_turnover and exit_turnover are correctly computed."""
        sig = _make_signal(entry_price=2000.0, target_price=2050.0)
        res = _evaluate(sig, lot_size=2)
        self.assertAlmostEqual(res.entry_turnover, 2000.0 * 2, places=4)
        self.assertAlmostEqual(res.exit_turnover,  2050.0 * 2, places=4)

    def test_31_lot_size_scales_statutory_charges(self):
        """lot_size=2 doubles statutory charges vs lot_size=1."""
        sig = _make_signal(entry_price=2000.0, target_price=2050.0)
        r1 = _evaluate(sig, lot_size=1)
        r2 = _evaluate(sig, lot_size=2)
        self.assertAlmostEqual(
            r2.total_statutory_charges, r1.total_statutory_charges * 2, places=3
        )


# ─────────────────────────────────────────────────────────────────────────────
# Part D — Optional deductions
# ─────────────────────────────────────────────────────────────────────────────

class TestOptionalDeductions(unittest.TestCase):

    def test_32_all_optional_none_omitted(self):
        """With no optional costs, all three names appear in omitted_deductions."""
        sig = _make_signal()
        res = _evaluate(sig)
        self.assertIn("spread",   res.omitted_deductions)
        self.assertIn("slippage", res.omitted_deductions)
        self.assertIn("funding",  res.omitted_deductions)

    def test_33_spread_provided_applied_not_omitted(self):
        """Provided spread is applied and not in omitted_deductions."""
        sig = _make_signal()
        res = _evaluate(sig, spread_per_unit=0.5)
        self.assertNotIn("spread", res.omitted_deductions)
        self.assertIsNotNone(res.deduction_spread)
        self.assertAlmostEqual(res.deduction_spread, 0.5, places=4)

    def test_34_slippage_provided_applied(self):
        """Provided slippage is applied."""
        sig = _make_signal()
        res = _evaluate(sig, slippage_per_unit=0.3)
        self.assertNotIn("slippage", res.omitted_deductions)
        self.assertAlmostEqual(res.deduction_slippage, 0.3, places=4)

    def test_35_funding_provided_applied(self):
        """Provided funding_per_lot is applied."""
        sig = _make_signal()
        res = _evaluate(sig, funding_per_lot=2.0)
        self.assertNotIn("funding", res.omitted_deductions)
        self.assertAlmostEqual(res.deduction_funding, 2.0, places=4)

    def test_36_spread_zero_is_applied_not_omitted(self):
        """Explicit spread=0.0 is applied (not the same as None)."""
        sig = _make_signal()
        res = _evaluate(sig, spread_per_unit=0.0)
        self.assertNotIn("spread", res.omitted_deductions)
        self.assertAlmostEqual(res.deduction_spread, 0.0, places=4)

    def test_37_all_optional_provided_total_deductions_includes_all(self):
        """total_deductions = statutory + spread + slippage + funding."""
        sig = _make_signal()
        res = _evaluate(sig, spread_per_unit=1.0, slippage_per_unit=2.0, funding_per_lot=3.0)
        self.assertEqual(res.omitted_deductions, [])
        expected_total = round(
            res.total_statutory_charges + 1.0 + 2.0 + 3.0, 4
        )
        self.assertAlmostEqual(res.total_deductions, expected_total, places=4)

    def test_38_spread_scaled_by_lot_size(self):
        """spread_per_unit × lot_size = deduction_spread."""
        sig = _make_signal()
        res = _evaluate(sig, spread_per_unit=0.5, lot_size=10)
        self.assertAlmostEqual(res.deduction_spread, 5.0, places=4)

    def test_39_slippage_scaled_by_lot_size(self):
        """slippage_per_unit × lot_size = deduction_slippage."""
        sig = _make_signal()
        res = _evaluate(sig, slippage_per_unit=0.25, lot_size=4)
        self.assertAlmostEqual(res.deduction_slippage, 1.0, places=4)


# ─────────────────────────────────────────────────────────────────────────────
# Part E — Invalid optional cost inputs → ValueError
# ─────────────────────────────────────────────────────────────────────────────

class TestInvalidOptionalCosts(unittest.TestCase):

    def test_40_spread_nan_raises(self):
        """spread_per_unit=NaN raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        sig = _make_signal()
        with self.assertRaises(ValueError):
            NetEdgeEvaluator.evaluate(sig, spread_per_unit=float("nan"), _now=_NOW)

    def test_41_spread_negative_raises(self):
        """spread_per_unit < 0 raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        sig = _make_signal()
        with self.assertRaises(ValueError):
            NetEdgeEvaluator.evaluate(sig, spread_per_unit=-0.01, _now=_NOW)

    def test_42_slippage_inf_raises(self):
        """slippage_per_unit=Inf raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        sig = _make_signal()
        with self.assertRaises(ValueError):
            NetEdgeEvaluator.evaluate(sig, slippage_per_unit=float("inf"), _now=_NOW)

    def test_43_funding_negative_raises(self):
        """funding_per_lot < 0 raises ValueError."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        sig = _make_signal()
        with self.assertRaises(ValueError):
            NetEdgeEvaluator.evaluate(sig, funding_per_lot=-1.0, _now=_NOW)


# ─────────────────────────────────────────────────────────────────────────────
# Part F — INSUFFICIENT_DATA edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestInsufficientData(unittest.TestCase):

    def _assert_insufficient(self, sig, **kwargs):
        from strategies.net_edge_evaluator import NetEdgeDecision
        res = _evaluate(sig, **kwargs)
        self.assertEqual(res.decision, NetEdgeDecision.INSUFFICIENT_DATA)
        self.assertTrue(res.is_shadow)

    def test_44_target_price_none(self):
        """target_price=None → INSUFFICIENT_DATA."""
        sig = _make_signal(target_price=None)
        self._assert_insufficient(sig)

    def test_45_entry_price_zero(self):
        """entry_price=0 → INSUFFICIENT_DATA."""
        sig = _make_signal(entry_price=0.0)
        self._assert_insufficient(sig)

    def test_46_entry_price_negative(self):
        """entry_price < 0 → INSUFFICIENT_DATA."""
        sig = _make_signal(entry_price=-100.0)
        self._assert_insufficient(sig)

    def test_47_entry_price_nan(self):
        """entry_price=NaN → INSUFFICIENT_DATA."""
        sig = _make_signal(entry_price=float("nan"))
        self._assert_insufficient(sig)

    def test_48_entry_price_inf(self):
        """entry_price=Inf → INSUFFICIENT_DATA."""
        sig = _make_signal(entry_price=float("inf"))
        self._assert_insufficient(sig)

    def test_49_target_price_zero(self):
        """target_price=0 → INSUFFICIENT_DATA."""
        sig = _make_signal(target_price=0.0)
        self._assert_insufficient(sig)

    def test_50_target_price_nan(self):
        """target_price=NaN → INSUFFICIENT_DATA."""
        sig = _make_signal(target_price=float("nan"))
        self._assert_insufficient(sig)

    def test_51_lot_size_zero(self):
        """lot_size=0 → INSUFFICIENT_DATA."""
        sig = _make_signal()
        self._assert_insufficient(sig, lot_size=0)

    def test_52_lot_size_negative(self):
        """lot_size=-1 → INSUFFICIENT_DATA."""
        sig = _make_signal()
        self._assert_insufficient(sig, lot_size=-1)

    def test_53_insufficient_data_reason_is_nonempty(self):
        """INSUFFICIENT_DATA result always has a non-empty reason string."""
        sig = _make_signal(target_price=None)
        res = _evaluate(sig)
        self.assertIsInstance(res.reason, str)
        self.assertGreater(len(res.reason), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Part G — PASS / FAIL decisions
# ─────────────────────────────────────────────────────────────────────────────

class TestPassFailDecisions(unittest.TestCase):

    def test_54_pass_when_net_edge_exceeds_threshold(self):
        """net_edge > min_net_edge → PASS."""
        from strategies.net_edge_evaluator import NetEdgeDecision
        sig = _make_signal(entry_price=2000.0, target_price=2100.0)
        res = _evaluate(sig, config=_cfg(1.0))
        self.assertEqual(res.decision, NetEdgeDecision.PASS)

    def test_55_fail_when_net_edge_below_threshold(self):
        """net_edge < min_net_edge → FAIL."""
        from strategies.net_edge_evaluator import NetEdgeDecision
        # Small 1-rupee move will be eaten by charges
        sig = _make_signal(entry_price=2000.0, target_price=2001.0)
        res = _evaluate(sig, config=_cfg(500.0))
        self.assertEqual(res.decision, NetEdgeDecision.FAIL)

    def test_56_pass_at_boundary_exactly_equal(self):
        """net_edge exactly equal to min_net_edge → PASS (>= boundary)."""
        from strategies.net_edge_evaluator import NetEdgeDecision, NetEdgeEvaluator
        sig = _make_signal(entry_price=2000.0, target_price=2050.0)
        res_ref = _evaluate(sig, config=_cfg(0.0))
        # Use the computed net_edge as the threshold — should be exactly PASS
        cfg_exact = _cfg(res_ref.net_edge)
        res = _evaluate(sig, config=cfg_exact)
        self.assertEqual(res.decision, NetEdgeDecision.PASS)

    def test_57_fail_when_gross_edge_equals_costs(self):
        """When gross_edge is entirely consumed by costs, net_edge <= 0 → FAIL with threshold > 0."""
        from strategies.net_edge_evaluator import NetEdgeDecision
        sig = _make_signal(entry_price=2000.0, target_price=2000.0)  # zero gross
        res = _evaluate(sig, config=_cfg(0.001))
        self.assertEqual(res.decision, NetEdgeDecision.FAIL)

    def test_58_pass_with_zero_threshold_and_positive_net(self):
        """min_net_edge=0.0 with clearly positive net edge → PASS."""
        from strategies.net_edge_evaluator import NetEdgeDecision
        sig = _make_signal(entry_price=2000.0, target_price=2200.0)
        res = _evaluate(sig, config=_cfg(0.0))
        self.assertEqual(res.decision, NetEdgeDecision.PASS)

    def test_59_net_edge_equals_gross_minus_deductions(self):
        """net_edge == gross_edge - total_deductions (accounting identity)."""
        sig = _make_signal(entry_price=2000.0, target_price=2050.0)
        res = _evaluate(sig, spread_per_unit=0.5, slippage_per_unit=0.3)
        expected = round(res.gross_edge - res.total_deductions, 4)
        self.assertAlmostEqual(res.net_edge, expected, places=4)


# ─────────────────────────────────────────────────────────────────────────────
# Part H — Output schema and audit metadata
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputSchema(unittest.TestCase):

    def setUp(self):
        self.sig = _make_signal()
        self.res = _evaluate(self.sig)

    def test_60_is_shadow_always_true(self):
        """is_shadow is always True."""
        self.assertTrue(self.res.is_shadow)

    def test_61_to_dict_all_keys_present(self):
        """to_dict() contains all expected keys."""
        d = self.res.to_dict()
        for key in (
            "is_shadow", "signal_id", "symbol", "exchange", "asset_class",
            "direction", "entry_price", "target_price", "lot_size",
            "gross_edge", "deduction_brokerage", "deduction_stt",
            "deduction_exchange_charges", "deduction_gst", "deduction_sebi_charges",
            "deduction_spread", "deduction_slippage", "deduction_funding",
            "total_statutory_charges", "total_deductions", "net_edge",
            "min_net_edge", "decision", "reason", "omitted_deductions",
            "entry_turnover", "exit_turnover", "evaluated_at",
        ):
            self.assertIn(key, d, key)

    def test_62_decision_is_string_in_dict(self):
        """decision is a plain string in to_dict() output."""
        d = self.res.to_dict()
        self.assertIsInstance(d["decision"], str)

    def test_63_evaluated_at_is_utc_aware(self):
        """evaluated_at has tzinfo=UTC."""
        self.assertIsNotNone(self.res.evaluated_at.tzinfo)
        self.assertEqual(self.res.evaluated_at, _NOW)

    def test_64_evaluated_at_isoformat_in_dict(self):
        """evaluated_at in to_dict() is an ISO 8601 string."""
        d = self.res.to_dict()
        self.assertIsInstance(d["evaluated_at"], str)
        self.assertIn("T", d["evaluated_at"])

    def test_65_signal_id_preserved(self):
        """signal_id in result matches the input signal."""
        self.assertEqual(self.res.signal_id, self.sig.signal_id)

    def test_66_symbol_uppercased(self):
        """symbol is upper-cased in the result."""
        sig = _make_signal(symbol="reliance")
        res = _evaluate(sig)
        self.assertEqual(res.symbol, "RELIANCE")

    def test_67_reason_contains_decision_and_edge_values(self):
        """reason string contains 'decision=', 'gross_edge=', and 'net_edge='."""
        self.assertIn("decision=", self.res.reason)
        self.assertIn("gross_edge=", self.res.reason)
        self.assertIn("net_edge=", self.res.reason)

    def test_68_omitted_deductions_is_list(self):
        """omitted_deductions is a list."""
        self.assertIsInstance(self.res.omitted_deductions, list)

    def test_69_insufficient_data_is_shadow(self):
        """INSUFFICIENT_DATA result also has is_shadow=True."""
        sig = _make_signal(target_price=None)
        res = _evaluate(sig)
        self.assertTrue(res.is_shadow)


# ─────────────────────────────────────────────────────────────────────────────
# Part I — Shadow isolation from order / execution paths
# ─────────────────────────────────────────────────────────────────────────────

class TestShadowIsolation(unittest.TestCase):

    def test_70_evaluate_does_not_modify_signal(self):
        """NetEdgeEvaluator.evaluate() does not mutate the input signal."""
        sig = _make_signal()
        original_entry = sig.entry_price
        original_target = sig.target_price
        original_id = sig.signal_id
        _evaluate(sig)
        self.assertEqual(sig.entry_price, original_entry)
        self.assertEqual(sig.target_price, original_target)
        self.assertEqual(sig.signal_id, original_id)

    def test_71_net_edge_result_not_in_order_authorizer(self):
        """NetEdgeResult has no authorize_order, place_order, or submit_order method."""
        sig = _make_signal()
        res = _evaluate(sig)
        for attr in ("authorize_order", "place_order", "submit_order", "cancel_order"):
            self.assertFalse(hasattr(res, attr), attr)

    def test_72_net_edge_evaluator_has_no_broker_adapter(self):
        """NetEdgeEvaluator has no broker adapter attribute."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        for attr in ("adapter", "broker", "place_order", "submit_order"):
            self.assertFalse(hasattr(NetEdgeEvaluator, attr), attr)

    def test_73_evaluate_not_in_signal_store(self):
        """NetEdgeEvaluator.evaluate() does not call signal_store.add_signal."""
        from strategies.signal_store import signal_store
        sig = _make_signal()
        before_count = len(signal_store.get_all_actionable_signals())
        _evaluate(sig)
        after_count = len(signal_store.get_all_actionable_signals())
        self.assertEqual(before_count, after_count)


# ─────────────────────────────────────────────────────────────────────────────
# Part J — Determinism
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterminism(unittest.TestCase):

    def test_74_identical_inputs_same_result(self):
        """Two calls with identical inputs produce identical results."""
        sig = _make_signal()
        r1 = _evaluate(sig, lot_size=1, spread_per_unit=0.5, config=_cfg(2.0))
        r2 = _evaluate(sig, lot_size=1, spread_per_unit=0.5, config=_cfg(2.0))
        self.assertEqual(r1.gross_edge,             r2.gross_edge)
        self.assertEqual(r1.total_deductions,       r2.total_deductions)
        self.assertEqual(r1.net_edge,               r2.net_edge)
        self.assertEqual(r1.decision,               r2.decision)
        self.assertEqual(r1.total_statutory_charges, r2.total_statutory_charges)

    def test_75_now_parameter_sets_evaluated_at(self):
        """_now parameter overrides evaluated_at in result."""
        from strategies.net_edge_evaluator import NetEdgeEvaluator
        sig = _make_signal()
        custom_now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        res = NetEdgeEvaluator.evaluate(sig, _now=custom_now)
        self.assertEqual(res.evaluated_at, custom_now)


# ─────────────────────────────────────────────────────────────────────────────
# Part K — Regression: existing components unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestRegression(unittest.TestCase):

    def test_76_charge_calculator_still_works(self):
        """ChargeCalculator.calculate_total_charges() is unmodified."""
        from risk.charge_calculator import ChargeCalculator
        total = ChargeCalculator.calculate_total_charges(100_000.0, "INTRADAY", "EQUITY", True)
        self.assertGreater(total, 0.0)

    def test_77_signal_model_unchanged(self):
        """Signal dataclass fields are unchanged by S14."""
        sig = _make_signal()
        for field_name in (
            "symbol", "exchange", "asset_class", "signal_type", "direction",
            "entry_price", "target_price", "stop_loss_price",
            "confidence_score", "confidence_tier", "timestamp",
            "expires_at", "signal_id", "state",
        ):
            self.assertTrue(hasattr(sig, field_name), field_name)

    def test_78_s13_signal_fuser_unmodified(self):
        """SignalFuser still operates independently of S14."""
        from strategies.signal_fuser import SignalFuser
        from models.instrument import AssetClass, Exchange
        from models.signal import Direction, Signal, SignalType
        sig = Signal(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=2000.0,
            confidence_score=72.0,
            timestamp=_NOW,
            expires_at=_NOW.replace(hour=10),
        )
        result = SignalFuser.fuse([sig], _now=_NOW)
        from strategies.signal_fuser import FusedDirection
        self.assertEqual(result.fused_direction, FusedDirection.BULLISH)

    def test_79_strategies_init_exports_s14_symbols(self):
        """strategies/__init__.py exports all S14 public symbols."""
        import strategies
        for name in ("NetEdgeConfig", "NetEdgeDecision", "NetEdgeEvaluator", "NetEdgeResult"):
            self.assertTrue(hasattr(strategies, name), name)

    def test_80_net_edge_result_no_connection_to_prohibit_live_order(self):
        """NetEdgeResult and NetEdgeEvaluator do not import live_order_guard."""
        import sys
        import strategies.net_edge_evaluator as mod
        self.assertNotIn("live_order_guard", str(vars(mod)))
        self.assertNotIn("prohibit_live_order", str(vars(mod)))


if __name__ == "__main__":
    unittest.main()
