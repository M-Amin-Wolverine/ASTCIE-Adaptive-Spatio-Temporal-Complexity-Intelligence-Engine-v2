#!/usr/bin/env python3
"""
BitrateAllocator – Single Owner of Final Bitrate

Ownership contract:

    Feedback
        ↓
      Suggestion
        ↓
     Decision
        ↓
      Policy
        ↓
     Allocator
        ↓
   target_bitrate
        ↓
      Encoder

Important:
    Feedback and Decision must NOT directly modify:

        ch.state.target_bitrate

    Only BitrateAllocator is allowed to determine the final
    target bitrate.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from ..models import (
    ChannelContext,
    ControllerConfig,
    EncoderAction,
)

logger = logging.getLogger("astcie.allocator")


class BitrateAllocator:
    """
    Global owner of final video bitrate allocation.

    Responsibilities:
        1. Initial allocation
        2. Runtime reallocation
        3. Enforce bmin / bmax
        4. Enforce global video budget
        5. Apply policy actions
        6. Write final target_bitrate

    Non-responsibilities:
        - Feedback measurement
        - Quality measurement
        - Decision generation
        - Encoder execution
    """

    def __init__(
        self,
        config: ControllerConfig,
        intelligence,
    ):
        self.config = config
        self.intelligence = intelligence
        self.logger = logging.getLogger("astcie.allocator")

        # Last globally approved video budget.
        self._video_budget: Optional[float] = None

    # ==================================================================
    # Budget registration
    # ==================================================================

    def set_video_budget(self, video_budget: float) -> None:
        """
        Register the globally available video bitrate budget.

        Allocator becomes the enforcement point for this budget.
        """

        if video_budget < 0.0:
            raise ValueError(
                f"Invalid video budget: {video_budget:.6f} Mbps"
            )

        self._video_budget = float(video_budget)

        self.logger.info(
            "Allocator video budget set to %.4f Mbps",
            self._video_budget,
        )

    # ==================================================================
    # Initial allocation
    # ==================================================================

    def initial_allocate(
        self,
        channels: List[ChannelContext],
        video_budget: float,
    ) -> None:
        """
        Perform the first global bitrate allocation.

        Allocation pipeline:

            Complexity / Priority
                    ↓
                 Weights
                    ↓
             Raw allocation
                    ↓
              [bmin,bmax]
                    ↓
              Redistribution
                    ↓
            Final validation
                    ↓
             target_bitrate

        Hard invariants:

            bmin <= target_bitrate <= bmax

            SUM(target_bitrate) <= video_budget
        """

        if not channels:
            self.logger.warning(
                "No channels available for initial allocation."
            )
            return

        self.set_video_budget(video_budget)

        n = len(channels)
        bmin = self.config.bmin
        bmax = self.config.bmax

        # --------------------------------------------------------------
        # Configuration validation
        # --------------------------------------------------------------

        if bmin < 0.0:
            raise ValueError(
                f"Invalid bmin={bmin}. Must be >= 0."
            )

        if bmax < bmin:
            raise ValueError(
                f"Invalid bitrate range: "
                f"bmax={bmax} < bmin={bmin}"
            )

        # --------------------------------------------------------------
        # Global feasibility
        # --------------------------------------------------------------

        required_minimum = n * bmin

        if video_budget < required_minimum - 1e-9:
            raise ValueError(
                "Infeasible bitrate allocation: "
                f"{n} channels × bmin={bmin:.4f} Mbps = "
                f"{required_minimum:.4f} Mbps, "
                f"but video budget is only "
                f"{video_budget:.4f} Mbps."
            )

        self.logger.info(
            "Allocation feasibility PASS | "
            "minimum=%.4f Mbps | budget=%.4f Mbps",
            required_minimum,
            video_budget,
        )

        # --------------------------------------------------------------
        # Get policy/complexity weights
        # --------------------------------------------------------------

        weights = self.intelligence.compute_weights(channels)

        if len(weights) != n:
            raise RuntimeError(
                "Weight count mismatch: "
                f"{len(weights)} weights for {n} channels."
            )

        # --------------------------------------------------------------
        # Raw weighted allocation
        # --------------------------------------------------------------

        raw = [
            video_budget * weight
            for weight in weights
        ]

        # --------------------------------------------------------------
        # Initial clipping
        # --------------------------------------------------------------

        allocated = [
            max(
                bmin,
                min(bmax, bitrate),
            )
            for bitrate in raw
        ]

        # --------------------------------------------------------------
        # If clipping created unused budget, redistribute it.
        # --------------------------------------------------------------

        total = sum(allocated)

        if total < video_budget - 1e-9:
            self._redistribute_up(
                allocated=allocated,
                budget=video_budget,
                bmax=bmax,
            )

        # --------------------------------------------------------------
        # Final normalization / safety
        # --------------------------------------------------------------

        allocated = [
            min(
                bmax,
                max(bmin, bitrate),
            )
            for bitrate in allocated
        ]

        # --------------------------------------------------------------
        # Final global invariant
        # --------------------------------------------------------------

        total = sum(allocated)

        if total > video_budget + 1e-6:
            raise RuntimeError(
                "Initial allocation violated global budget: "
                f"allocated={total:.6f} Mbps > "
                f"budget={video_budget:.6f} Mbps"
            )

        # --------------------------------------------------------------
        # Commit ownership
        #
        # THIS is where target_bitrate is officially written.
        # --------------------------------------------------------------

        self._commit_allocation(
            channels,
            allocated,
            weights=weights,
        )

        self.logger.info(
            "Initial allocation complete | "
            "video=%.4f / %.4f Mbps | "
            "headroom=%.4f Mbps",
            total,
            video_budget,
            video_budget - total,
        )

    # ==================================================================
    # Runtime allocation
    # ==================================================================

    def reallocate(
        self,
        channels: List[ChannelContext],
        actions: List[EncoderAction],
        suggestions: Optional[dict] = None,
        hysteresis: float | None = None,
        min_interval: float | None = None,
        video_budget: float | None = None,
    ) -> None:
        """
        Apply Decision-layer actions and produce the final bitrate.

        IMPORTANT:
            actions are INPUT.
            suggestions are OPTIONAL INPUT from Feedback
                {channel_id: suggested_bitrate_mbps}.
            target_bitrate is OUTPUT — only Allocator writes it.

        Supported actions:
            KEEP, INCREASE, DECREASE, REALLOCATE, RECOVER
        """
        if not channels:
            return

        if len(actions) != len(channels):
            raise ValueError(
                "Action count does not match channel count: "
                f"{len(actions)} != {len(channels)}"
            )

        if video_budget is not None:
            self.set_video_budget(video_budget)

        if self._video_budget is None:
            raise RuntimeError(
                "Allocator has no video budget. "
                "Call initial_allocate() or set_video_budget() first."
            )

        budget = self._video_budget
        suggestions = suggestions or {}

        hysteresis = (
            hysteresis
            if hysteresis is not None
            else self.config.hysteresis
        )
        min_interval = (
            min_interval
            if min_interval is not None
            else self.config.feedback_interval
        )

        now = time.time()

        # Start from CURRENT allocation (previous allocator outputs only).
        proposed = [
            float(ch.state.target_bitrate or 0.0)
            for ch in channels
        ]

        # Apply policy actions to a temporary proposal (no State write yet).
        for i, (ch, action) in enumerate(zip(channels, actions)):
            st = ch.state

            if now - getattr(st, "last_action_time", 0.0) < min_interval:
                continue

            old = proposed[i]
            if old <= 0.0:
                old = self.config.bmin

            if action == EncoderAction.KEEP:
                continue

            suggested = suggestions.get(ch.channel_id)
            if suggested is not None:
                try:
                    suggested = float(suggested)
                except (TypeError, ValueError):
                    suggested = None

            if action == EncoderAction.INCREASE:
                if suggested is not None and suggested > old:
                    proposed[i] = min(self.config.bmax, suggested)
                else:
                    delta = max(old * hysteresis, old * 0.05)
                    proposed[i] = min(self.config.bmax, old + delta)

            elif action == EncoderAction.DECREASE:
                if suggested is not None and suggested < old:
                    proposed[i] = max(self.config.bmin, suggested)
                else:
                    delta = max(old * hysteresis, old * 0.05)
                    proposed[i] = max(self.config.bmin, old - delta)

            elif action == EncoderAction.RECOVER:
                if suggested is not None:
                    proposed[i] = min(
                        self.config.bmax,
                        max(self.config.bmin, suggested),
                    )
                else:
                    proposed[i] = min(
                        self.config.bmax,
                        max(self.config.bmin, old),
                    )

            elif action == EncoderAction.REALLOCATE:
                self.logger.info(
                    "CH%03d requested global REALLOCATE",
                    ch.channel_id,
                )
                continue

        # Clamp every proposal.
        proposed = [
            min(self.config.bmax, max(self.config.bmin, bitrate))
            for bitrate in proposed
        ]

        # Enforce global budget BEFORE commit.
        proposed = self._fit_to_budget(channels, proposed, budget)

        # Commit final allocation — ONLY Allocator writes target_bitrate.
        for ch, bitrate, action in zip(channels, proposed, actions):
            old = float(ch.state.target_bitrate or 0.0)
            new = round(bitrate, 4)

            if old > 0.0:
                relative_change = abs(new - old) / max(old, 1e-6)
                min_delta = float(
                    getattr(self.config, "min_bitrate_delta", 0.01) or 0.01
                )
                if relative_change < min_delta:
                    new = round(old, 4)

            ch.state.target_bitrate = new
            ch.state.video_bitrate = new

            if action != EncoderAction.KEEP:
                ch.state.last_action = action
                ch.state.last_action_time = now

            if hasattr(ch.state, "touch"):
                ch.state.touch()

            if abs(new - old) > 1e-9:
                self.logger.info(
                    "CH%03d | %s | %.4f → %.4f Mbps",
                    ch.channel_id,
                    getattr(action, "value", action),
                    old,
                    new,
                )

        self._validate_global_budget(channels, budget)

    # ==================================================================
    # Budget fitting
    # ==================================================================

    def _fit_to_budget(
        self,
        channels: List[ChannelContext],
        proposed: List[float],
        budget: float,
    ) -> List[float]:
        """
        Fit proposed bitrate values into the global budget.

        If proposed total <= budget:
            keep proposals.

        If proposed total > budget:
            reduce channels proportionally according to the amount
            they can safely surrender above bmin.

        Never goes below bmin.
        """

        if not proposed:
            return proposed

        total = sum(proposed)

        if total <= budget + 1e-9:
            return proposed

        excess = total - budget

        self.logger.warning(
            "Proposed allocation exceeds budget: "
            "%.4f > %.4f Mbps. "
            "Reducing allocation by %.4f Mbps.",
            total,
            budget,
            excess,
        )

        result = list(proposed)

        # --------------------------------------------------------------
        # Feasibility must still hold.
        # --------------------------------------------------------------

        minimum_total = (
            len(channels) * self.config.bmin
        )

        if budget < minimum_total - 1e-9:
            raise RuntimeError(
                "Global budget became infeasible during runtime: "
                f"budget={budget:.4f} Mbps < "
                f"minimum={minimum_total:.4f} Mbps."
            )

        # --------------------------------------------------------------
        # Proportional reduction.
        # --------------------------------------------------------------

        remaining_excess = excess

        while remaining_excess > 1e-9:

            reducible = [
                max(
                    0.0,
                    bitrate - self.config.bmin,
                )
                for bitrate in result
            ]

            total_reducible = sum(reducible)

            if total_reducible <= 1e-12:
                break

            reduced_this_round = 0.0

            for i, room in enumerate(reducible):

                if room <= 0.0:
                    continue

                share = (
                    remaining_excess
                    * room
                    / total_reducible
                )

                reduction = min(
                    share,
                    room,
                )

                result[i] -= reduction
                reduced_this_round += reduction

            if reduced_this_round <= 1e-12:
                break

            remaining_excess -= reduced_this_round

        # --------------------------------------------------------------
        # Final clamp.
        # --------------------------------------------------------------

        result = [
            min(
                self.config.bmax,
                max(
                    self.config.bmin,
                    bitrate,
                ),
            )
            for bitrate in result
        ]

        return result

    # ==================================================================
    # Upward redistribution
    # ==================================================================

    def _redistribute_up(
        self,
        allocated: List[float],
        budget: float,
        bmax: float,
    ) -> None:
        """
        Distribute unused budget among channels that are below bmax.

        This function mutates the temporary allocation only.
        It does NOT touch ChannelState.
        """

        remaining = (
            budget - sum(allocated)
        )

        while remaining > 1e-9:

            rooms = [
                max(
                    0.0,
                    bmax - bitrate,
                )
                for bitrate in allocated
            ]

            total_room = sum(rooms)

            if total_room <= 1e-12:
                break

            distributed = 0.0

            for i, room in enumerate(rooms):

                if room <= 0.0:
                    continue

                share = (
                    remaining
                    * room
                    / total_room
                )

                extra = min(
                    share,
                    room,
                )

                allocated[i] += extra
                distributed += extra

            if distributed <= 1e-12:
                break

            remaining -= distributed

    # ==================================================================
    # Commit
    # ==================================================================

    def _commit_allocation(
        self,
        channels: List[ChannelContext],
        allocated: List[float],
        weights: List[float] | None = None,
    ) -> None:
        """
        Commit the allocator's final decisions to ChannelState.

        This is the main ownership boundary.

        Only this method writes target_bitrate during initial allocation.
        """

        if len(channels) != len(allocated):
            raise ValueError(
                "Channel/allocation count mismatch."
            )

        for index, (ch, bitrate) in enumerate(
            zip(channels, allocated)
        ):
            bitrate = round(
                min(
                    self.config.bmax,
                    max(
                        self.config.bmin,
                        bitrate,
                    ),
                ),
                4,
            )

            ch.state.target_bitrate = bitrate
            ch.state.video_bitrate = bitrate
            ch.state.audio_bitrate = (
                self.config.audio_bitrate
            )
            ch.state.touch()

            if weights is not None:
                self.logger.info(
                    "CH%03d | weight=%.4f | "
                    "video=%.4f Mbps | audio=%.3f Mbps",
                    ch.channel_id,
                    weights[index],
                    bitrate,
                    ch.state.audio_bitrate,
                )
            else:
                self.logger.info(
                    "CH%03d | video=%.4f Mbps | audio=%.3f Mbps",
                    ch.channel_id,
                    bitrate,
                    ch.state.audio_bitrate,
                )

    # ==================================================================
    # Validation
    # ==================================================================

    def _validate_global_budget(
        self,
        channels: List[ChannelContext],
        video_budget: float,
    ) -> None:
        """
        Validate the final allocator state.

        Invariants:

            bmin <= bitrate_i <= bmax

            SUM(bitrate_i) <= video_budget
        """

        total = 0.0

        for ch in channels:
            bitrate = ch.state.target_bitrate

            if bitrate < self.config.bmin - 1e-6:
                raise RuntimeError(
                    f"CH{ch.channel_id:03d} bitrate "
                    f"{bitrate:.6f} Mbps is below bmin "
                    f"{self.config.bmin:.6f} Mbps."
                )

            if bitrate > self.config.bmax + 1e-6:
                raise RuntimeError(
                    f"CH{ch.channel_id:03d} bitrate "
                    f"{bitrate:.6f} Mbps exceeds bmax "
                    f"{self.config.bmax:.6f} Mbps."
                )

            total += bitrate

        if total > video_budget + 1e-6:
            raise RuntimeError(
                "GLOBAL BITRATE INVARIANT VIOLATED: "
                f"{total:.6f} Mbps > "
                f"{video_budget:.6f} Mbps."
            )

        self.logger.debug(
            "Global bitrate invariant PASS | "
            "%.4f / %.4f Mbps",
            total,
            video_budget,
        )

    # ==================================================================
    # Introspection
    # ==================================================================

    @property
    def video_budget(self) -> Optional[float]:
        """
        Current globally registered video budget.
        """
        return self._video_budget
