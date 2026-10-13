from typing import Dict, Tuple
from models.option_chain import OptionChain


class PCRCalculator:
    @staticmethod
    def calculate(chain: OptionChain) -> Tuple[float, Dict[float, float]]:
        total_call_oi = 0.0
        total_put_oi = 0.0
        strike_pcrs: Dict[float, float] = {}

        for strike in chain.strikes:
            call_oi = max(0.0, float(strike.call.oi or 0.0))
            put_oi = max(0.0, float(strike.put.oi or 0.0))

            total_call_oi += call_oi
            total_put_oi += put_oi

            strike_pcr = strike.calculate_pcr()
            strike_pcrs[strike.strike_price] = strike_pcr

        if total_call_oi > 0:
            overall_pcr = round(total_put_oi / total_call_oi, 4)
        else:
            overall_pcr = 0.0

        chain.total_call_oi = total_call_oi
        chain.total_put_oi = total_put_oi
        chain.pcr = overall_pcr

        return overall_pcr, strike_pcrs


pcr_calculator = PCRCalculator()
