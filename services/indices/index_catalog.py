from dataclasses import dataclass, field
from typing import Dict, List, Optional

from models.instrument import Exchange


@dataclass(slots=True)
class IndexDefinition:
    """
    Metadata specification for an exchange index.
    Zero hardcoded constituent symbols — only definitions and criteria.
    """
    name: str
    exchange: Exchange
    expected_count: int
    refresh_cadence_hours: int = 24
    description: str = ""
    tolerance_pct: float = 0.10  # ±10% tolerance for count verification
    tag: Optional[str] = None


class IndexCatalog:
    """
    Catalog of all supported benchmark and sectoral indices.
    """

    def __init__(self) -> None:
        self._indices: Dict[str, IndexDefinition] = {
            "NIFTY_50": IndexDefinition(
                name="NIFTY_50",
                exchange=Exchange.NSE,
                expected_count=50,
                refresh_cadence_hours=24,
                description="NIFTY 50 Benchmark Index",
                tolerance_pct=0.10,
                tag="NIFTY",
            ),
            "NIFTY_100": IndexDefinition(
                name="NIFTY_100",
                exchange=Exchange.NSE,
                expected_count=100,
                refresh_cadence_hours=24,
                description="NIFTY 100 Broad Market Index",
                tolerance_pct=0.10,
                tag="NIFTY100",
            ),
            "BANK_NIFTY": IndexDefinition(
                name="BANK_NIFTY",
                exchange=Exchange.NSE,
                expected_count=20,
                refresh_cadence_hours=24,
                description="NIFTY Bank Sectoral Index",
                tolerance_pct=0.20,
                tag="BANKNIFTY",
            ),
            "NIFTY_IT": IndexDefinition(
                name="NIFTY_IT",
                exchange=Exchange.NSE,
                expected_count=10,
                refresh_cadence_hours=24,
                description="NIFTY Information Technology Sectoral Index",
                tolerance_pct=0.10,
                tag="IT",
            ),
            "NIFTY_PHARMA": IndexDefinition(
                name="NIFTY_PHARMA",
                exchange=Exchange.NSE,
                expected_count=10,
                refresh_cadence_hours=24,
                description="NIFTY Pharmaceuticals Sectoral Index",
                tolerance_pct=0.10,
                tag="PHARMA",
            ),
            "NIFTY_FMCG": IndexDefinition(
                name="NIFTY_FMCG",
                exchange=Exchange.NSE,
                expected_count=15,
                refresh_cadence_hours=24,
                description="NIFTY Fast Moving Consumer Goods Sectoral Index",
                tolerance_pct=0.10,
                tag="FMCG",
            ),
            "BSE_SENSEX": IndexDefinition(
                name="BSE_SENSEX",
                exchange=Exchange.BSE,
                expected_count=30,
                refresh_cadence_hours=24,
                description="BSE SENSEX 30 Benchmark Index",
                tolerance_pct=0.10,
                tag="SENSEX",
            ),
            "BSE_100": IndexDefinition(
                name="BSE_100",
                exchange=Exchange.BSE,
                expected_count=100,
                refresh_cadence_hours=24,
                description="BSE 100 Broad Market Index",
                tolerance_pct=0.10,
                tag="BSE100",
            ),
        }

    def get_index(self, name: str) -> Optional[IndexDefinition]:
        return self._indices.get(name.upper())

    def list_indices(self) -> List[IndexDefinition]:
        return list(self._indices.values())

    def list_names(self) -> List[str]:
        return list(self._indices.keys())


index_catalog = IndexCatalog()
