#!/usr/bin/env python3
"""
HybridEngine – 50/50 fusion of V8.1 + V9 results.
"""

from __future__ import annotations

import logging
from typing import List

from ..models import ChannelContext, ControllerConfig

logger = logging.getLogger("astcie.hybrid")


class HybridEngine:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.hybrid")

    def compute(self, channels: List[ChannelContext]) -> None:
        w81 = self.config.weight_v81
        w9 = self.config.weight_v9

        for ch in channels:
            v81 = ch.v81
            v9 = ch.v9

            if not v81 or not v9:
                raise RuntimeError(f"Channel {ch.channel_id} missing engine results")

            complexity = w81 * (v81["complexity"] or 0.0) + w9 * (v9["complexity"] or 0.0)
            bdi = w81 * (v81["bdi"] or 0.0) + w9 * (v9["bdi"] or 0.0)

            ch.hybrid = {
                "weight_v81": w81,
                "weight_v9": w9,
                "complexity": complexity,
                "bdi": bdi,
            }

            # Populate ChannelState with the fused values + extra signals
            st = ch.state
            st.complexity = complexity
            st.bdi = bdi
            st.risk = v9.get("risk")
            st.regime = v9.get("regime") or v81.get("regime")
            st.adci_temporal = v9.get("adci_temporal")
            st.adci_spatial = v9.get("adci_spatial")
            st.spatial_score = v81.get("spatial_score")
            st.temporal_score = v81.get("temporal_score")
            st.motion_score = v81.get("motion_score")
            st.touch()

            self.logger.debug(
                "Channel %03d Hybrid → C=%.5f  BDI=%.5f  regime=%s",
                ch.channel_id, complexity, bdi, st.regime,
            )

        self.logger.info("Hybrid fusion completed for %d channel(s)", len(channels))
