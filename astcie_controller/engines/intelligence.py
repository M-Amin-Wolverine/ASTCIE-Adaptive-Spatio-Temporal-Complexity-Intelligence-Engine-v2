#!/usr/bin/env python3
"""
GlobalIntelligence – global budget calculation and weight computation.

Responsibilities:
    Link Budget
        ↓
    Audio Reservation
        ↓
    Video Budget
        ↓
    Feasibility Check
        ↓
    Channel Weights
"""

from __future__ import annotations

import logging
from typing import List

from ..models import ChannelContext, ControllerConfig

logger = logging.getLogger("astcie.intelligence")


class GlobalIntelligence:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.intelligence")

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def calculate_budget(self, channels: List[ChannelContext]) -> float:
        """
        Calculate the available video bitrate budget in Mbps.

        Formula:

            audio_total = N × audio_bitrate

            video_budget = link_budget - audio_total

        Hard feasibility constraint:

            N × bmin <= video_budget

        If this condition is not satisfied, the system fails before
        bitrate allocation and encoding.
        """

        n = len(channels)

        if n == 0:
            self.logger.warning("No channels available for budget calculation.")
            return 0.0

        # --------------------------------------------------------------
        # Audio reservation
        # --------------------------------------------------------------

        audio_per = self.config.audio_bitrate

        if self.config.adaptive_audio:
            # Future extension:
            # audio bitrate may become channel-specific.
            # For now, use the configured nominal bitrate.
            audio_per = self.config.audio_bitrate

        audio_total = n * audio_per

        # --------------------------------------------------------------
        # Video budget
        # --------------------------------------------------------------

        video_budget = self.config.link_budget - audio_total

        # --------------------------------------------------------------
        # Minimum required video budget
        # --------------------------------------------------------------

        required_minimum = n * self.config.bmin

        # --------------------------------------------------------------
        # Logging
        # --------------------------------------------------------------

        self.logger.info(
            "Link Budget      = %.3f Mbps",
            self.config.link_budget,
        )

        self.logger.info(
            "Audio Reservation = %.3f Mbps × %d = %.3f Mbps",
            audio_per,
            n,
            audio_total,
        )

        self.logger.info(
            "Video Budget      = %.3f Mbps",
            video_budget,
        )

        self.logger.info(
            "Minimum Required  = %.3f Mbps "
            "(%d × %.3f)",
            required_minimum,
            n,
            self.config.bmin,
        )

        # --------------------------------------------------------------
        # HARD FEASIBILITY CHECK
        # --------------------------------------------------------------

        if video_budget < required_minimum:
            self.logger.error(
                "BITRATE BUDGET INFEASIBLE: "
                "available %.3f Mbps < required %.3f Mbps",
                video_budget,
                required_minimum,
            )

            raise ValueError(
                "Infeasible video bitrate budget: "
                f"{n} channel(s) require at least "
                f"{required_minimum:.3f} Mbps "
                f"({self.config.bmin:.3f} Mbps/channel), "
                f"but only {video_budget:.3f} Mbps is available "
                f"after audio reservation."
            )

        self.logger.info(
            "Budget feasibility check: PASS "
            "(%.3f >= %.3f Mbps)",
            video_budget,
            required_minimum,
        )

        return video_budget

    # ------------------------------------------------------------------
    # Weights
    # ------------------------------------------------------------------

    def compute_weights(
        self,
        channels: List[ChannelContext],
    ) -> List[float]:
        """
        Compute normalized allocation weights.

        Base weight:
            complexity

        Optional:
            complexity × priority

        Final invariant:

            SUM(weights) = 1
        """

        if not channels:
            return []

        scores: List[float] = []

        for ch in channels:
            complexity = max(0.0, ch.state.complexity)

            if self.config.enable_priority:
                priority = max(0.01, ch.state.priority)
                score = complexity * priority
            else:
                score = complexity

            scores.append(max(0.0, score))

        total = sum(scores)

        # --------------------------------------------------------------
        # Equal-share fallback
        # --------------------------------------------------------------

        if total <= 0.0:
            n = len(channels)
            weights = [1.0 / n] * n

            self.logger.warning(
                "All channel scores are zero; using equal weights: %s",
                [f"{w:.4f}" for w in weights],
            )

            return weights

        # --------------------------------------------------------------
        # Normalize
        # --------------------------------------------------------------

        weights = [score / total for score in scores]

        # Floating-point normalization guard
        weight_sum = sum(weights)

        if weight_sum > 0.0:
            weights = [w / weight_sum for w in weights]

        self.logger.debug(
            "Weights: %s",
            [f"{w:.4f}" for w in weights],
        )

        return weights
