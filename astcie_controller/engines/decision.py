#!/usr/bin/env python3
"""
DecisionEngine – final policy layer (Phase 11).

Pipeline:
  QualityEngine → State (quality_metric, quality_confidence)
        ↓
  FeedbackController → List[FeedbackDecision]  (proposal only)
        ↓
  DecisionEngine.decide(channels, feedback_decisions=...)
        ↓
  List[EncoderAction]  final policy
        ↓
  Allocator  (owns ch.state.target_bitrate; applies suggested rates)
        ↓
  Encoder

Rules
-----
  • Feedback interprets quality; Decision does NOT re-interpret quality.
  • Decision accepts / rejects / overrides Feedback proposals.
  • RECOVER has absolute priority (encoder ERROR, critical drops).
  • Confidence gate and headroom checks are safety nets only.
  • Decision may set ch.state.last_action for telemetry; never target_bitrate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import ChannelContext, ControllerConfig, EncoderAction, EncoderStatus

logger = logging.getLogger("astcie.decision")

try:
    from .feedback import FeedbackDecision
except Exception:  # pragma: no cover
    FeedbackDecision = None  # type: ignore


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class DecisionConfig:
    # Used only in standalone mode (no FeedbackDecision supplied)
    q_low: float = 0.40
    q_high: float = 0.75
    min_confidence: float = 0.60

    pressure_high: float = 0.70
    pressure_low: float = 0.30
    complexity_weight: float = 0.60
    bdi_weight: float = 0.40

    drops_recover_threshold: int = 10

    headroom_up: float = 0.95
    headroom_down: float = 1.05

    global_budget_pressure: float = 0.92


# ---------------------------------------------------------------------------
# Detail record
# ---------------------------------------------------------------------------

@dataclass
class DecisionDetail:
    channel_id: int
    action: EncoderAction
    reason: str
    quality: Optional[float] = None
    confidence: Optional[float] = None
    pressure: float = 0.0
    suggested_bitrate: Optional[float] = None
    from_feedback: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "action": getattr(self.action, "value", str(self.action)),
            "reason": self.reason,
            "quality": self.quality,
            "confidence": self.confidence,
            "pressure": self.pressure,
            "suggested_bitrate": self.suggested_bitrate,
            "from_feedback": self.from_feedback,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DecisionEngine:
    def __init__(
        self,
        config: ControllerConfig,
        dcfg: Optional[DecisionConfig] = None,
    ) -> None:
        self.config = config
        self.dcfg = dcfg or DecisionConfig()
        self.logger = logging.getLogger("astcie.decision")
        self._last_details: List[DecisionDetail] = []
        self._last_fb_map: Dict[int, "FeedbackDecision"] = {}

        self._has_reallocate = any(
            getattr(a, "name", str(a)) == "REALLOCATE"
            or str(getattr(a, "value", "")).lower() == "reallocate"
            for a in EncoderAction
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(
        self,
        channels: List[ChannelContext],
        feedback_decisions: Optional[Sequence["FeedbackDecision"]] = None,
    ) -> List[EncoderAction]:
        """
        Final policy pass.

        Pass feedback_decisions from FeedbackController.evaluate() so
        Decision acts as policy layer over quality proposals.

        Without feedback_decisions, falls back to standalone heuristics
        (quality + complexity/BDI from State) for backward compatibility.
        """
        actions, _ = self.decide_with_details(channels, feedback_decisions)
        return actions

    def decide_with_details(
        self,
        channels: List[ChannelContext],
        feedback_decisions: Optional[Sequence["FeedbackDecision"]] = None,
    ) -> Tuple[List[EncoderAction], List[DecisionDetail]]:
        fb_map: Dict[int, "FeedbackDecision"] = {}
        if feedback_decisions:
            for fd in feedback_decisions:
                fb_map[int(fd.channel_id)] = fd
        self._last_fb_map = fb_map

        actions: List[EncoderAction] = []
        details: List[DecisionDetail] = []
        global_pressure = self._global_budget_pressure(channels)

        for ch in channels:
            fd = fb_map.get(ch.channel_id)
            action, detail = self._decide_one(ch, fd, global_pressure)
            actions.append(action)
            details.append(detail)

            # Telemetry only — Allocator owns target_bitrate
            try:
                ch.state.last_action = action
            except Exception:
                pass

            self.logger.debug(
                "CH%03d decision → %s  (%s)  q=%s c=%s P=%.3f fb=%s",
                ch.channel_id,
                getattr(action, "value", action),
                detail.reason,
                f"{detail.quality:.2f}" if detail.quality is not None else "-",
                f"{detail.confidence:.2f}" if detail.confidence is not None else "-",
                detail.pressure,
                detail.from_feedback,
            )

        self._last_details = details
        return actions, details

    @property
    def last_details(self) -> List[DecisionDetail]:
        return list(self._last_details)

    def suggested_bitrate(self, channel_id: int) -> Optional[float]:
        """Suggested bitrate from the last FeedbackDecision for this channel."""
        fd = self._last_fb_map.get(int(channel_id))
        if fd is None:
            return None
        return float(fd.new_bitrate)

    def feedback_map(self) -> Dict[int, "FeedbackDecision"]:
        return dict(self._last_fb_map)

    # ------------------------------------------------------------------
    # Per-channel policy
    # ------------------------------------------------------------------

    def _decide_one(
        self,
        ch: ChannelContext,
        fd: Optional["FeedbackDecision"],
        global_pressure: float,
    ) -> Tuple[EncoderAction, DecisionDetail]:
        st = ch.state
        cid = int(ch.channel_id)

        q = self._opt_float(getattr(st, "quality_metric", None))
        conf = self._opt_float(getattr(st, "quality_confidence", None)) or 0.0
        complexity = float(getattr(st, "complexity", 0.0) or 0.0)
        bdi = float(getattr(st, "bdi", 0.0) or 0.0)
        drops = int(getattr(st, "frame_drops", 0) or 0)
        target = float(getattr(st, "target_bitrate", 0.0) or 0.0)
        pressure = (
            self.dcfg.complexity_weight * complexity
            + self.dcfg.bdi_weight * bdi
        )

        suggested_br: Optional[float] = None
        if fd is not None:
            q = fd.quality if fd.quality is not None else q
            conf = fd.confidence if fd.confidence is not None else conf
            if fd.pressure:
                pressure = fd.pressure
            suggested_br = float(fd.new_bitrate)

        enc_status = getattr(st, "encoder_status", None)

        # ==============================================================
        # Priority 1 — RECOVER
        # ==============================================================
        if enc_status == EncoderStatus.ERROR:
            return self._result(
                cid, EncoderAction.RECOVER, "encoder_status=ERROR",
                q, conf, pressure, suggested_br, from_feedback=False,
            )

        if drops >= self.dcfg.drops_recover_threshold:
            return self._result(
                cid, EncoderAction.RECOVER, f"frame_drops={drops}",
                q, conf, pressure, suggested_br, from_feedback=False,
            )

        if fd is not None and fd.action == EncoderAction.RECOVER:
            return self._result(
                cid, EncoderAction.RECOVER, f"feedback:{fd.reason}",
                q, conf, pressure, suggested_br, from_feedback=True,
            )

        # ==============================================================
        # Priority 2 — global budget (optional REALLOCATE)
        # ==============================================================
        if (
            self._has_reallocate
            and global_pressure >= self.dcfg.global_budget_pressure
            and target > self._bmin() * 1.1
        ):
            reallocate = self._action_reallocate()
            if reallocate is not None:
                return self._result(
                    cid, reallocate,
                    f"global budget pressure {global_pressure:.2f}",
                    q, conf, pressure, suggested_br, from_feedback=False,
                )

        # ==============================================================
        # Priority 3 — Feedback proposal (quality path)
        # ==============================================================
        if fd is not None:
            result = self._apply_feedback(ch, fd, q, conf, pressure, target, suggested_br)
            if result is not None:
                return result

        # ==============================================================
        # Priority 4 — standalone fallback
        # ==============================================================
        return self._standalone(
            cid, q, conf, complexity, bdi, pressure, target,
        )

    def _apply_feedback(
        self,
        ch: ChannelContext,
        fd: "FeedbackDecision",
        q: Optional[float],
        conf: float,
        pressure: float,
        target: float,
        suggested_br: Optional[float],
    ) -> Optional[Tuple[EncoderAction, DecisionDetail]]:
        """
        Accept Feedback action unless it violates safety bounds.
        Does not re-run quality logic — only policy / headroom checks.
        """
        cid = int(ch.channel_id)
        action = fd.action

        if action == EncoderAction.KEEP:
            return self._result(
                cid, EncoderAction.KEEP, f"feedback:{fd.reason}",
                q, conf, pressure, suggested_br, from_feedback=True,
            )

        if action == EncoderAction.INCREASE:
            if target >= self._bmax() * self.dcfg.headroom_up:
                return self._result(
                    cid, EncoderAction.KEEP,
                    "increase blocked (at bmax headroom)",
                    q, conf, pressure, suggested_br, from_feedback=True,
                )
            if conf < self.dcfg.min_confidence and "low confidence" not in (fd.reason or ""):
                return self._result(
                    cid, EncoderAction.KEEP,
                    f"increase blocked (confidence {conf:.2f})",
                    q, conf, pressure, suggested_br, from_feedback=True,
                )
            return self._result(
                cid, EncoderAction.INCREASE, f"feedback:{fd.reason}",
                q, conf, pressure, suggested_br, from_feedback=True,
            )

        if action == EncoderAction.DECREASE:
            if target <= self._bmin() * self.dcfg.headroom_down:
                return self._result(
                    cid, EncoderAction.KEEP,
                    "decrease blocked (at bmin headroom)",
                    q, conf, pressure, suggested_br, from_feedback=True,
                )
            return self._result(
                cid, EncoderAction.DECREASE, f"feedback:{fd.reason}",
                q, conf, pressure, suggested_br, from_feedback=True,
            )

        return self._result(
            cid, action, f"feedback:{fd.reason}",
            q, conf, pressure, suggested_br, from_feedback=True,
        )

    def _standalone(
        self,
        cid: int,
        q: Optional[float],
        conf: float,
        complexity: float,
        bdi: float,
        pressure: float,
        target: float,
    ) -> Tuple[EncoderAction, DecisionDetail]:
        if q is not None and conf < self.dcfg.min_confidence:
            return self._result(
                cid, EncoderAction.KEEP,
                f"low confidence ({conf:.2f})",
                q, conf, pressure, None, from_feedback=False,
            )

        if q is not None:
            if q < self.dcfg.q_low and target < self._bmax() * self.dcfg.headroom_up:
                return self._result(
                    cid, EncoderAction.INCREASE,
                    f"quality low ({q:.2f} < {self.dcfg.q_low})",
                    q, conf, pressure, None, from_feedback=False,
                )
            if (
                q > self.dcfg.q_high
                and complexity < 0.60
                and target > self._bmin() * self.dcfg.headroom_down
            ):
                return self._result(
                    cid, EncoderAction.DECREASE,
                    f"quality high ({q:.2f} > {self.dcfg.q_high})",
                    q, conf, pressure, None, from_feedback=False,
                )

        if (
            pressure > self.dcfg.pressure_high
            and target < self._bmax() * self.dcfg.headroom_up
        ):
            return self._result(
                cid, EncoderAction.INCREASE,
                f"pressure high ({pressure:.3f})",
                q, conf, pressure, None, from_feedback=False,
            )

        if (
            pressure < self.dcfg.pressure_low
            and target > self._bmin() * self.dcfg.headroom_down
        ):
            return self._result(
                cid, EncoderAction.DECREASE,
                f"pressure low ({pressure:.3f})",
                q, conf, pressure, None, from_feedback=False,
            )

        return self._result(
            cid, EncoderAction.KEEP, "within band",
            q, conf, pressure, None, from_feedback=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _result(
        self,
        cid: int,
        action: EncoderAction,
        reason: str,
        q: Optional[float],
        conf: float,
        pressure: float,
        suggested_br: Optional[float],
        from_feedback: bool,
    ) -> Tuple[EncoderAction, DecisionDetail]:
        detail = DecisionDetail(
            channel_id=cid,
            action=action,
            reason=reason,
            quality=q,
            confidence=conf,
            pressure=pressure,
            suggested_bitrate=suggested_br,
            from_feedback=from_feedback,
        )
        return action, detail

    def _bmin(self) -> float:
        return float(getattr(self.config, "bmin", 0.5) or 0.5)

    def _bmax(self) -> float:
        return float(getattr(self.config, "bmax", 20.0) or 20.0)

    def _budget(self) -> Optional[float]:
        for name in ("total_budget", "budget", "global_budget", "bitrate_budget"):
            v = getattr(self.config, name, None)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    def _global_budget_pressure(self, channels: Sequence[ChannelContext]) -> float:
        budget = self._budget()
        if not budget or budget <= 0:
            return 0.0
        total = 0.0
        for ch in channels:
            total += float(getattr(ch.state, "target_bitrate", 0.0) or 0.0)
        return total / budget

    def _action_reallocate(self) -> Optional[EncoderAction]:
        if not self._has_reallocate:
            return None
        for a in EncoderAction:
            if (
                getattr(a, "name", "") == "REALLOCATE"
                or str(getattr(a, "value", "")).lower() == "reallocate"
            ):
                return a
        return None

    @staticmethod
    def _opt_float(v) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f:
            return None
        return f
