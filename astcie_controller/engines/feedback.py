#!/usr/bin/env python3
"""
FeedbackController – quality-aware interpretation layer (Phase 11).

Pipeline:
  QualityEngine → hybrid + confidence → ChannelState / QualitySample
        ↓
  FeedbackController.evaluate()
        ↓
  FeedbackDecision  (action + suggested bitrate + reason)
        ↓
  DecisionEngine    (final policy – does NOT change target here)
        ↓
  Allocator         (owns ch.state.target_bitrate)
        ↓
  Encoder

Rules
-----
  • Feedback ONLY proposes. It never writes target_bitrate.
  • Low confidence → KEEP (never thrash on noisy measurements).
  • Short history (moving mean + consecutive low/high) reduces noise.
  • RECOVER is suggested on encoder ERROR / heavy frame drops;
    Decision still has absolute priority on RECOVER.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from ..models import ChannelContext, ControllerConfig, EncoderAction, EncoderStatus
from .quality import QualitySample

logger = logging.getLogger("astcie.feedback")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class FeedbackConfig:
    # Quality band on hybrid scale [0, 1]
    q_low: float = 0.40
    q_high: float = 0.75
    q_target: float = 0.55

    # Confidence gate — below this, never INCREASE/DECREASE on quality alone
    min_confidence: float = 0.60

    # Relative bitrate steps (suggestions only)
    step_up: float = 0.15
    step_down: float = 0.10
    step_up_soft: float = 0.08
    step_down_soft: float = 0.06
    complexity_boost: float = 0.05

    # Temporal
    history_len: int = 5
    consecutive_low: int = 2
    consecutive_high: int = 2
    cooldown_sec: float = 2.0
    min_rel_change: float = 0.05

    # Runtime pressure
    drops_recover_threshold: int = 10
    complexity_high: float = 0.80
    complexity_low: float = 0.35
    bdi_high: float = 0.70

    def __post_init__(self) -> None:
        self.q_low = max(0.0, min(1.0, float(self.q_low)))
        self.q_high = max(self.q_low, min(1.0, float(self.q_high)))
        self.min_confidence = max(0.0, min(1.0, float(self.min_confidence)))
        self.history_len = max(1, int(self.history_len))
        self.consecutive_low = max(1, int(self.consecutive_low))
        self.consecutive_high = max(1, int(self.consecutive_high))
        self.cooldown_sec = max(0.0, float(self.cooldown_sec))
        self.min_rel_change = max(0.0, float(self.min_rel_change))


# ---------------------------------------------------------------------------
# Decision payload (proposal only)
# ---------------------------------------------------------------------------

@dataclass
class FeedbackDecision:
    channel_id: int
    action: EncoderAction
    old_bitrate: float
    new_bitrate: float          # suggested — Allocator applies this
    reason: str
    quality: float
    confidence: float = 0.0
    pressure: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "action": getattr(self.action, "value", str(self.action)),
            "old_bitrate": self.old_bitrate,
            "new_bitrate": self.new_bitrate,
            "reason": self.reason,
            "quality": self.quality,
            "confidence": self.confidence,
            "pressure": self.pressure,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Per-channel history
# ---------------------------------------------------------------------------

@dataclass
class _ChannelHistory:
    quality: Deque[float] = field(default_factory=lambda: deque(maxlen=5))
    confidence: Deque[float] = field(default_factory=lambda: deque(maxlen=5))

    def push(self, q: float, c: float) -> None:
        self.quality.append(q)
        self.confidence.append(c)

    def mean_quality(self) -> float:
        if not self.quality:
            return 0.5
        return sum(self.quality) / len(self.quality)

    def mean_confidence(self) -> float:
        if not self.confidence:
            return 0.0
        return sum(self.confidence) / len(self.confidence)

    def consecutive_below(self, threshold: float) -> int:
        n = 0
        for v in reversed(self.quality):
            if v < threshold:
                n += 1
            else:
                break
        return n

    def consecutive_above(self, threshold: float) -> int:
        n = 0
        for v in reversed(self.quality):
            if v > threshold:
                n += 1
            else:
                break
        return n

    def trend(self) -> float:
        if len(self.quality) < 2:
            return 0.0
        qs = list(self.quality)
        return qs[-1] - qs[0]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class FeedbackController:
    def __init__(
        self,
        config: ControllerConfig,
        fb: Optional[FeedbackConfig] = None,
    ) -> None:
        self.config = config
        self.fb = fb or self._default_fb_config(config)
        self.logger = logging.getLogger("astcie.feedback")
        self._last_decision_ts: Dict[int, float] = {}
        self._last_suggested_bitrate: Dict[int, float] = {}
        self._history: Dict[int, _ChannelHistory] = {}

    @staticmethod
    def _default_fb_config(config: ControllerConfig) -> FeedbackConfig:
        cooldown = float(getattr(config, "feedback_interval", 2.0) or 2.0)
        hyst = float(getattr(config, "hysteresis", 0.05) or 0.05)
        return FeedbackConfig(
            cooldown_sec=max(cooldown, 1.0),
            min_rel_change=max(hyst, 0.01),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        channels: List[ChannelContext],
        quality_samples: Optional[List[QualitySample]] = None,
    ) -> List[FeedbackDecision]:
        """
        Produce FeedbackDecision proposals for channels that may act
        (cooldown expired and valid target bitrate present).

        Side effects (telemetry only — does NOT own target_bitrate):
          • records cooldown timestamps and history
          • does NOT write ch.state.target_bitrate
          • does NOT write ch.state.last_action  (Decision / Allocator own those)
        """
        qmap: Dict[int, QualitySample] = {}
        if quality_samples:
            for s in quality_samples:
                qmap[int(s.channel_id)] = s

        decisions: List[FeedbackDecision] = []
        now = time.time()

        for ch in channels:
            d = self._eval_one(ch, qmap.get(ch.channel_id), now)
            if d is None:
                continue
            decisions.append(d)
            self._last_decision_ts[ch.channel_id] = now
            self._last_suggested_bitrate[ch.channel_id] = d.new_bitrate
            # Intentionally NOT writing:
            #   ch.state.target_bitrate = ...
            #   ch.state.last_action = ...
            # Allocator owns target; Decision owns final action.

        return decisions

    def reset_channel(self, channel_id: int) -> None:
        self._last_decision_ts.pop(channel_id, None)
        self._last_suggested_bitrate.pop(channel_id, None)
        self._history.pop(channel_id, None)

    def reset_all(self) -> None:
        self._last_decision_ts.clear()
        self._last_suggested_bitrate.clear()
        self._history.clear()

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def _eval_one(
        self,
        ch: ChannelContext,
        sample: Optional[QualitySample],
        now: float,
    ) -> Optional[FeedbackDecision]:
        cid = int(ch.channel_id)
        st = ch.state

        last = self._last_decision_ts.get(cid, 0.0)
        if now - last < self.fb.cooldown_sec:
            return None

        target = float(getattr(st, "target_bitrate", 0.0) or 0.0)
        if target <= 0:
            return None

        q, conf, _ = self._read_quality(ch, sample)

        hist = self._history.setdefault(
            cid,
            _ChannelHistory(
                quality=deque(maxlen=self.fb.history_len),
                confidence=deque(maxlen=self.fb.history_len),
            ),
        )
        hist.push(q, conf)

        q_mean = hist.mean_quality()
        c_mean = hist.mean_confidence()
        conf_eff = max(conf, c_mean * 0.5)

        complexity = float(getattr(st, "complexity", 0.0) or 0.0)
        bdi = float(getattr(st, "bdi", 0.0) or 0.0)
        drops = int(getattr(st, "frame_drops", 0) or 0)
        pressure = 0.55 * complexity + 0.45 * bdi

        action = EncoderAction.KEEP
        reason = "within band"
        new_br = target

        # ---- hard runtime signals ------------------------------------
        enc_status = getattr(st, "encoder_status", None)
        if enc_status == EncoderStatus.ERROR:
            action = EncoderAction.RECOVER
            new_br = max(self._bmin(), target * 0.90)
            reason = "encoder_status=ERROR"
            return self._finish(cid, action, target, new_br, reason, q, conf, pressure)

        if drops >= self.fb.drops_recover_threshold:
            action = EncoderAction.RECOVER
            new_br = max(self._bmin(), target * 0.90)
            reason = f"frame_drops={drops}"
            return self._finish(cid, action, target, new_br, reason, q, conf, pressure)

        # ---- confidence gate -----------------------------------------
        if conf_eff < self.fb.min_confidence:
            action = EncoderAction.KEEP
            reason = (
                f"low confidence ({conf_eff:.2f} < {self.fb.min_confidence:.2f})"
            )
            return self._finish(cid, action, target, target, reason, q, conf, pressure)

        # ---- quality-driven (with history) ---------------------------
        n_low = hist.consecutive_below(self.fb.q_low)
        n_high = hist.consecutive_above(self.fb.q_high)
        trend = hist.trend()

        if n_low >= self.fb.consecutive_low and q_mean < self.fb.q_low:
            action = EncoderAction.INCREASE
            new_br = target * (1.0 + self.fb.step_up)
            reason = (
                f"sustained low quality "
                f"(mean={q_mean:.2f} < {self.fb.q_low}, n={n_low})"
            )

        elif q < self.fb.q_low and conf >= self.fb.min_confidence:
            action = EncoderAction.INCREASE
            new_br = target * (1.0 + self.fb.step_up_soft)
            reason = f"quality low ({q:.2f} < {self.fb.q_low})"

        elif (
            n_high >= self.fb.consecutive_high
            and q_mean > self.fb.q_high
            and complexity < 0.65
        ):
            action = EncoderAction.DECREASE
            new_br = target * (1.0 - self.fb.step_down)
            reason = (
                f"sustained high quality "
                f"(mean={q_mean:.2f} > {self.fb.q_high}, n={n_high})"
            )

        elif q > self.fb.q_high and complexity < 0.55:
            action = EncoderAction.DECREASE
            new_br = target * (1.0 - self.fb.step_down_soft)
            reason = (
                f"quality high ({q:.2f} > {self.fb.q_high}) "
                f"and complexity low ({complexity:.2f})"
            )

        elif complexity >= self.fb.complexity_high and q < 0.65:
            action = EncoderAction.INCREASE
            new_br = target * (1.0 + self.fb.complexity_boost)
            reason = (
                f"high complexity ({complexity:.2f}) with mid quality ({q:.2f})"
            )

        elif (
            bdi >= self.fb.bdi_high
            and target < self._bmax() * 0.95
            and q < self.fb.q_high
        ):
            action = EncoderAction.INCREASE
            new_br = target * (1.0 + self.fb.step_up_soft)
            reason = f"high BDI pressure ({bdi:.2f})"

        elif (
            complexity <= self.fb.complexity_low
            and q_mean >= self.fb.q_target
            and target > self._bmin() * 1.05
        ):
            action = EncoderAction.DECREASE
            new_br = target * (1.0 - self.fb.step_down_soft)
            reason = (
                f"low complexity ({complexity:.2f}) "
                f"quality ok (mean={q_mean:.2f})"
            )

        elif trend < -0.08 and q < self.fb.q_target and conf >= self.fb.min_confidence:
            action = EncoderAction.INCREASE
            new_br = target * (1.0 + self.fb.step_up_soft * 0.7)
            reason = f"falling quality trend ({trend:+.2f})"

        # ---- clip + hysteresis ---------------------------------------
        new_br = max(self._bmin(), min(self._bmax(), new_br))
        rel = abs(new_br - target) / max(target, 1e-6)
        if action != EncoderAction.KEEP and rel < self.fb.min_rel_change:
            action = EncoderAction.KEEP
            new_br = target
            reason = f"change {rel:.3f} < hysteresis {self.fb.min_rel_change}"

        return self._finish(cid, action, target, new_br, reason, q, conf, pressure)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_quality(
        self,
        ch: ChannelContext,
        sample: Optional[QualitySample],
    ) -> Tuple[float, float, bool]:
        st = ch.state
        if sample is not None and getattr(sample, "ok", True):
            q = float(sample.hybrid)
            c = float(getattr(sample, "confidence", 0.0) or 0.0)
            return q, c, True

        q = getattr(st, "quality_metric", None)
        c = getattr(st, "quality_confidence", None)
        if q is not None:
            return float(q), float(c if c is not None else 0.0), False

        return 0.5, 0.0, False

    def _bmin(self) -> float:
        return float(getattr(self.config, "bmin", 0.5) or 0.5)

    def _bmax(self) -> float:
        return float(getattr(self.config, "bmax", 20.0) or 20.0)

    def _finish(
        self,
        cid: int,
        action: EncoderAction,
        old_br: float,
        new_br: float,
        reason: str,
        q: float,
        conf: float,
        pressure: float,
    ) -> FeedbackDecision:
        if action != EncoderAction.KEEP:
            self.logger.info(
                "FB CH%03d propose %s  %.3f → %.3f Mbps  q=%.2f c=%.2f  (%s)",
                cid,
                getattr(action, "value", action),
                old_br,
                new_br,
                q,
                conf,
                reason,
            )
        else:
            self.logger.debug(
                "FB CH%03d propose KEEP  q=%.2f c=%.2f  (%s)",
                cid, q, conf, reason,
            )
        return FeedbackDecision(
            channel_id=cid,
            action=action,
            old_bitrate=old_br,
            new_bitrate=new_br,
            reason=reason,
            quality=q,
            confidence=conf,
            pressure=pressure,
        )
