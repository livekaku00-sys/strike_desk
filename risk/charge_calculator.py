"""
Strike Desk V4 - Regulatory Charge & Transaction Cost Calculator
Calculates Indian market charges per regulatory norms (SEBI, NSE, Brokerage, STT, GST):
- Brokerage: Intraday equity min(₹20, 0.03%), Delivery 0%, F&O flat ₹20/order
- STT (Securities Transaction Tax):
    * Intraday equity: 0.025% on sell side only
    * Delivery equity: 0.1% on buy and sell
    * F&O: 0.0625% on sell side (options/futures)
- Exchange Transaction Charges (NSE):
    * Equity: 0.00325% of turnover
    * F&O: 0.002% of turnover
- GST (Goods & Services Tax): 18% on (Brokerage + Exchange Charges)
- SEBI Charges: ₹10 per crore (0.0001% of turnover)
"""
from typing import Dict, Optional, Union


class ChargeCalculator:
    """
    Computes statutory and transactional costs per trade for accurate Net P&L tracking.
    """

    @classmethod
    def calculate_brokerage(
        cls,
        turnover: float,
        trade_type: str = "INTRADAY",
        segment: str = "EQUITY",
    ) -> float:
        """
        Calculates brokerage:
        - Delivery Equity: ₹0 (free)
        - F&O: Flat ₹20 per executed order
        - Intraday Equity: Lower of ₹20 or 0.03% of turnover
        """
        if turnover <= 0.0:
            return 0.0

        t_type = trade_type.strip().upper()
        seg = segment.strip().upper()

        if seg in ("FNO", "FUTURES", "OPTIONS") or t_type in ("FNO", "FUTURES", "OPTIONS"):
            return 20.0
        if t_type in ("DELIVERY", "CNC"):
            return 0.0

        # Intraday Equity: min(₹20, 0.03%)
        return round(min(20.0, 0.0003 * turnover), 4)

    @classmethod
    def calculate_stt(
        cls,
        turnover: float,
        trade_type: str = "INTRADAY",
        segment: str = "EQUITY",
        is_sell: bool = True,
    ) -> float:
        """
        Calculates Securities Transaction Tax (STT):
        - Intraday equity sell: 0.025%
        - Delivery equity: 0.1% on buy and sell
        - F&O sell: 0.0625% on sell side
        """
        if turnover <= 0.0:
            return 0.0

        t_type = trade_type.strip().upper()
        seg = segment.strip().upper()

        if seg in ("FNO", "FUTURES", "OPTIONS") or t_type in ("FNO", "FUTURES", "OPTIONS"):
            return round(0.000625 * turnover, 4) if is_sell else 0.0

        if t_type in ("DELIVERY", "CNC"):
            return round(0.001 * turnover, 4)

        # Intraday Equity: STT applies only on sell side
        return round(0.00025 * turnover, 4) if is_sell else 0.0

    @classmethod
    def calculate_exchange_charges(
        cls,
        turnover: float,
        segment: str = "EQUITY",
    ) -> float:
        """
        Calculates NSE Exchange Transaction Charges:
        - Equity: 0.00325%
        - F&O: 0.002%
        """
        if turnover <= 0.0:
            return 0.0

        seg = segment.strip().upper()
        if seg in ("FNO", "FUTURES", "OPTIONS"):
            return round(0.00002 * turnover, 4)
        return round(0.0000325 * turnover, 4)

    @classmethod
    def calculate_gst(
        cls,
        subtotal: Optional[float] = None,
        brokerage: float = 0.0,
        exchange_charges: float = 0.0,
    ) -> float:
        """
        Calculates GST at 18% on (Brokerage + Exchange Charges).
        Accepts either a pre-summed subtotal or separate components.
        """
        base = subtotal if subtotal is not None else (brokerage + exchange_charges)
        if base <= 0.0:
            return 0.0
        return round(0.18 * base, 4)

    @classmethod
    def calculate_sebi_charges(cls, turnover: float) -> float:
        """
        Calculates SEBI turnover fee: ₹10 per crore (0.0001% of turnover).
        """
        if turnover <= 0.0:
            return 0.0
        return round(turnover * (10.0 / 10_000_000.0), 4)

    @classmethod
    def calculate_total_charges(
        cls,
        turnover: float,
        trade_type: str = "INTRADAY",
        segment: str = "EQUITY",
        is_sell: bool = True,
    ) -> float:
        """
        Calculates total statutory & transactional charges for a trade.
        """
        if turnover <= 0.0:
            return 0.0

        brokerage = cls.calculate_brokerage(turnover, trade_type, segment)
        stt = cls.calculate_stt(turnover, trade_type, segment, is_sell)
        exchange = cls.calculate_exchange_charges(turnover, segment)
        gst = cls.calculate_gst(brokerage=brokerage, exchange_charges=exchange)
        sebi = cls.calculate_sebi_charges(turnover)

        return round(brokerage + stt + exchange + gst + sebi, 4)

    @classmethod
    def get_charges_breakdown(
        cls,
        turnover: float,
        trade_type: str = "INTRADAY",
        segment: str = "EQUITY",
        is_sell: bool = True,
    ) -> Dict[str, float]:
        """
        Returns full granular itemized breakdown of charges.
        """
        brokerage = cls.calculate_brokerage(turnover, trade_type, segment)
        stt = cls.calculate_stt(turnover, trade_type, segment, is_sell)
        exchange = cls.calculate_exchange_charges(turnover, segment)
        gst = cls.calculate_gst(brokerage=brokerage, exchange_charges=exchange)
        sebi = cls.calculate_sebi_charges(turnover)
        total = round(brokerage + stt + exchange + gst + sebi, 4)

        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange_charges": exchange,
            "gst": gst,
            "sebi_charges": sebi,
            "total_charges": total,
        }


# Global singleton instance
charge_calculator = ChargeCalculator()