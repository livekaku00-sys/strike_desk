"""
Strike Desk V4 - Strategy Setup Confidence Scoring Framework

CRITICAL DISCLAIMER & ARCHITECTURAL DIRECTIVE:
This score is a calibrated measure of how many entry criteria are met.
It is NOT a probability of profit. It does not predict future returns.
It measures setup quality only.
"""
import logging
from typing import Dict, Optional

from models.signal import ConfidenceTier

logger = logging.getLogger(__name__)


class ConfidenceScorer:
    """
    Evaluates rule-based technical and market setup quality across weighted checklists.

    CRITICAL NOTICE:
    This score is a calibrated measure of how many entry criteria are met.
    It is NOT a probability of profit. It does not predict future returns.
    It measures setup quality only.
    """

    @staticmethod
    def validate_criteria(
        criteria_results: Dict[str, bool],
        weights: Dict[str, float],
        raise_exception: bool = False,
    ) -> bool:
        """
        Validates criteria integrity:
        1. Weights must sum to 100 (within 1e-4 tolerance).
        2. All evaluated criteria must exist in the weight dictionary.
        """
        total_weight = sum(weights.values())
        if abs(total_weight - 100.0) > 1e-4:
            msg = f"Criteria weights must sum to 100.0, got {total_weight:.2f}"
            if raise_exception:
                raise ValueError(msg)
            return False

        missing_weights = [c for c in criteria_results if c not in weights]
        if missing_weights:
            msg = f"Criteria missing assigned weights: {missing_weights}"
            if raise_exception:
                raise ValueError(msg)
            return False

        return True

    @classmethod
    def calculate_score(
        cls,
        criteria_results: Dict[str, bool],
        weights: Dict[str, float],
        validate: bool = True,
    ) -> float:
        """
        Calculates a deterministic setup quality score between 0.0 and 100.0.
        Score = Sum(weight_i) for all criteria_i that evaluated to True.

        This score is a calibrated measure of how many entry criteria are met.
        It is NOT a probability of profit. It does not predict future returns.
        It measures setup quality only.
        """
        if validate:
            cls.validate_criteria(criteria_results, weights, raise_exception=True)

        score = sum(weights[k] for k, met in criteria_results.items() if met and k in weights)
        clamped = max(0.0, min(100.0, round(score, 2)))
        return clamped

    @staticmethod
    def get_tier(score: float) -> ConfidenceTier:
        """
        Maps a 0.0-100.0 confidence score to a formal ConfidenceTier:
        - A+ (A_PLUS): score >= 85.0
        - A: score >= 70.0
        - B: score >= 55.0
        - C: score < 55.0 (not actionable)
        """
        return ConfidenceTier.from_score(score)


# Global helper instance
confidence_scorer = ConfidenceScorer()