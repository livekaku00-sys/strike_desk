"""
Seeds the option chain cache for strategy unit tests.

The option strategies resolve their entry price from the live chain, so tests that
exercise scoring/direction logic need a chain present or every signal is suppressed.
"""
from models.option_chain import OptionChain, OptionContract, StrikeData
from utils.cache_manager import option_chain_cache

FIXTURE_EXPIRY = "2026-09-29"
FIXTURE_CE_PREMIUM = 150.0
FIXTURE_PE_PREMIUM = 120.0


def seed_chain(
    symbol: str,
    spot: float,
    ce_premium: float = FIXTURE_CE_PREMIUM,
    pe_premium: float = FIXTURE_PE_PREMIUM,
    expiry: str = FIXTURE_EXPIRY,
) -> OptionChain:
    """Caches a 5-strike ladder centred on `spot` so ATM resolution lands on `spot`."""
    step = max(round(spot * 0.01), 1)
    strikes = [
        StrikeData(
            strike_price=float(spot + i * step),
            call=OptionContract(ltp=ce_premium, oi=1000.0, volume=5000.0, security_id=f"CE{i}"),
            put=OptionContract(ltp=pe_premium, oi=1200.0, volume=6000.0, security_id=f"PE{i}"),
        )
        for i in (-2, -1, 0, 1, 2)
    ]
    for s in strikes:
        s.calculate_pcr()

    chain = OptionChain(
        underlying=symbol.upper(),
        expiry_date=expiry,
        strikes=strikes,
        underlying_ltp=float(spot),
    )
    option_chain_cache.put(symbol, expiry, chain)
    return chain


def seed_many(symbol_spots: dict) -> None:
    for sym, spot in symbol_spots.items():
        seed_chain(sym, spot)
