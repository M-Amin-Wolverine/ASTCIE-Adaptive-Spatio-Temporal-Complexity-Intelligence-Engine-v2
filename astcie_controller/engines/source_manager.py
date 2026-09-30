#!/usr/bin/env python3
"""
SourceManager – Advanced per-channel live/file source manager
with Color Wall fallback, live input support, metrics, circuit breaker,
text overlay, event callbacks and more.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from shutil import which
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from urllib.parse import urlparse

from ..models import ChannelContext, ControllerConfig

logger = logging.getLogger("astcie.source")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FallbackVideo(str, Enum):
    COLOR_WALL = "color_wall"
    BLACK = "black"
    TEST_PATTERN = "test_pattern"
    STILL = "still"


class FallbackAudio(str, Enum):
    BEEP = "beep"
    SILENCE = "silence"
    TONE = "tone"


class SourceStatus(str, Enum):
    UNKNOWN = "unknown"
    REAL = "real"
    FALLBACK = "fallback"
    RECOVERING = "recovering"
    FAILED = "failed"
    BLACKLISTED = "blacklisted"


# ---------------------------------------------------------------------------
# Config & State
# ---------------------------------------------------------------------------

@dataclass
class FallbackConfig:
    video: FallbackVideo = FallbackVideo.COLOR_WALL
    audio: FallbackAudio = FallbackAudio.BEEP
    width: int = 1280
    height: int = 720
    fps: int = 25
    duration_sec: float = 10.0
    video_bitrate: str = "1500k"
    audio_bitrate: str = "96k"
    still_image: Optional[Path] = None
    video_codec: str = "auto"  # auto | libx264 | mpeg2video | h264_nvenc | hevc_nvenc
    max_placeholder_retries: int = 3
    placeholder_retry_backoff_sec: float = 1.0
    recover_hold_sec: float = 1.5
    # Text overlay
    enable_text_overlay: bool = True
    overlay_text: str = "OFF AIR"
    overlay_fontsize: int = 48
    overlay_fontcolor: str = "white"
    overlay_box: bool = True
    overlay_boxcolor: str = "black@0.5"
    show_channel_name: bool = True
    show_timestamp: bool = True
    # Live source
    live_probe_timeout_sec: float = 4.0
    live_fail_threshold: int = 3
    # Circuit breaker
    blacklist_duration_sec: float = 60.0
    # Probe cache
    probe_cache_ttl_sec: float = 15.0

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"invalid resolution {self.width}x{self.height}")
        if self.fps <= 0:
            raise ValueError(f"invalid fps {self.fps}")
        if self.duration_sec <= 0:
            raise ValueError(f"invalid duration_sec {self.duration_sec}")
        if self.video == FallbackVideo.STILL:
            if self.still_image is None:
                raise ValueError("STILL mode requires still_image path")
            p = Path(self.still_image)
            if not p.exists() or not p.is_file():
                raise ValueError(f"still_image not found: {p}")


@dataclass
class SourceMetrics:
    switches: int = 0
    recoveries: int = 0
    fallback_entries: int = 0
    total_fallback_time_sec: float = 0.0
    last_fallback_start: float = 0.0
    probe_failures: int = 0
    placeholder_builds: int = 0
    placeholder_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "switches": self.switches,
            "recoveries": self.recoveries,
            "fallback_entries": self.fallback_entries,
            "total_fallback_time_sec": round(self.total_fallback_time_sec, 2),
            "probe_failures": self.probe_failures,
            "placeholder_builds": self.placeholder_builds,
            "placeholder_failures": self.placeholder_failures,
        }


@dataclass
class SourceState:
    channel_id: int
    status: SourceStatus = SourceStatus.UNKNOWN
    real_path: Optional[Path | str] = None
    active_path: Optional[Path | str] = None
    fallback_path: Optional[Path] = None
    last_error: str = ""
    last_error_time: float = 0.0
    last_change: float = field(default_factory=time.time)
    recovering_since: float = 0.0
    blacklisted_until: float = 0.0
    consecutive_failures: int = 0
    metrics: SourceMetrics = field(default_factory=SourceMetrics)
    history: deque = field(default_factory=lambda: deque(maxlen=20))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "status": self.status.value,
            "real_path": str(self.real_path) if self.real_path else None,
            "active_path": str(self.active_path) if self.active_path else None,
            "fallback_path": str(self.fallback_path) if self.fallback_path else None,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "last_change": self.last_change,
            "blacklisted_until": self.blacklisted_until,
            "consecutive_failures": self.consecutive_failures,
            "metrics": self.metrics.to_dict(),
            "history": list(self.history),
        }


StatusChangeCallback = Callable[[int, SourceStatus, SourceStatus, str], None]


class SourceManager:
    """
    Advanced Source Manager with:
    - File + Live (RTSP/SRT/UDP/HTTP/RTMP) support
    - Circuit breaker / blacklist
    - Probe cache
    - Text overlay on placeholders
    - Metrics + history
    - Event callbacks
    - Force fallback / recover
    - GPU encoder auto-detect
    - Automatic cleanup
    """

    LIVE_SCHEMES = {"rtsp", "rtsps", "srt", "udp", "http", "https", "rtmp", "rtmps"}

    def __init__(
        self,
        config: ControllerConfig,
        fallback: Optional[FallbackConfig] = None,
        on_status_change: Optional[StatusChangeCallback] = None,
    ):
        self.config = config
        self.fallback = fallback or FallbackConfig()
        self.on_status_change = on_status_change

        try:
            self.fallback.validate()
        except ValueError as exc:
            if self.fallback.video == FallbackVideo.STILL:
                logger.warning("FallbackConfig invalid (%s) – forcing COLOR_WALL", exc)
                self.fallback.video = FallbackVideo.COLOR_WALL
                self.fallback.still_image = None
            else:
                raise

        self.logger = logging.getLogger("astcie.source")
        self._states: Dict[int, SourceState] = {}
        self._lock = threading.RLock()
        self._placeholder_paths: Set[Path] = set()
        self._resolved_vcodec: Optional[str] = None
        self._probe_cache: Dict[str, Tuple[bool, float]] = {}

        out = Path(config.output_dir) if not isinstance(config.output_dir, Path) else config.output_dir
        self._placeholder_dir = out / "placeholders"
        try:
            self._placeholder_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.error("Cannot create placeholder dir %s: %s", self._placeholder_dir, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_channel(self, ch: ChannelContext) -> SourceState:
        with self._lock:
            st = self._states.get(ch.channel_id)
            if st is None:
                inp = getattr(ch, "input_path", None)
                st = SourceState(channel_id=ch.channel_id, real_path=inp)
                self._states[ch.channel_id] = st
            else:
                inp = getattr(ch, "input_path", None)
                if inp:
                    st.real_path = inp
            return st

    def probe(self, channel_id: int) -> SourceStatus:
        with self._lock:
            st = self._states.get(channel_id)
            return st.status if st else SourceStatus.UNKNOWN

    def refresh(self, channels: List[ChannelContext]) -> List[SourceState]:
        with self._lock:
            active_ids = {ch.channel_id for ch in channels}
            for dead in list(self._states.keys()):
                if dead not in active_ids:
                    self.logger.debug("Pruning SourceState CH%03d", dead)
                    del self._states[dead]

            out: List[SourceState] = []
            for ch in channels:
                st = self.ensure_channel(ch)
                prev = st.status

                if st.blacklisted_until > time.time():
                    self._apply_fallback(ch, st, prev, reason="blacklisted")
                    out.append(st)
                    continue

                real = self._pick_real(ch, st)
                if real is not None:
                    self._apply_real(ch, st, real, prev)
                else:
                    self._apply_fallback(ch, st, prev)
                out.append(st)
            return list(out)

    def force_fallback(self, channel_id: int, reason: str = "manual") -> bool:
        with self._lock:
            st = self._states.get(channel_id)
            if not st:
                return False
            prev = st.status

            class FakeCh:
                def __init__(self, cid: int) -> None:
                    self.channel_id = cid
                    self.encoded_path = None
                    self.input_path = st.real_path

            self._apply_fallback(FakeCh(channel_id), st, prev, reason=reason)
            return True

    def force_recover(self, channel_id: int) -> bool:
        with self._lock:
            st = self._states.get(channel_id)
            if not st:
                return False
            st.blacklisted_until = 0.0
            st.consecutive_failures = 0
            st.last_error = ""
            self.logger.info("CH%03d force recover requested", channel_id)
            return True

    def status_summary(self) -> str:
        lines = ["Source Manager (Advanced)", "─" * 60]
        with self._lock:
            items = [s.to_dict() for s in self._states.values()]
        if not items:
            lines.append("  (no channels)")
            return "\n".join(lines)
        for d in sorted(items, key=lambda x: x["channel_id"]):
            m = d["metrics"]
            lines.append(
                f"  CH{d['channel_id']:03d}  {d['status']:12}  "
                f"active={d['active_path'] or '-'}  "
                f"sw={m['switches']} rec={m['recoveries']} "
                f"fb_time={m['total_fallback_time_sec']}s"
            )
            if d["last_error"]:
                lines.append(f"         err: {d['last_error'][:100]}")
            if d["blacklisted_until"] > time.time():
                remain = int(d["blacklisted_until"] - time.time())
                lines.append(f"         BLACKLISTED for {remain}s more")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "channels": [s.to_dict() for s in self._states.values()],
                "placeholder_dir": str(self._placeholder_dir),
                "fallback": {
                    "video": self.fallback.video.value,
                    "audio": self.fallback.audio.value,
                    "width": self.fallback.width,
                    "height": self.fallback.height,
                    "fps": self.fallback.fps,
                    "duration_sec": self.fallback.duration_sec,
                    "video_codec": self._video_codec(),
                    "text_overlay": self.fallback.enable_text_overlay,
                },
                "probe_cache_size": len(self._probe_cache),
            }

    def cleanup_placeholders(self, max_age_sec: float = 3600.0) -> int:
        deleted = 0
        now = time.time()
        try:
            for f in self._placeholder_dir.glob("*.ts"):
                try:
                    if now - f.stat().st_mtime > max_age_sec:
                        f.unlink()
                        self._placeholder_paths.discard(f.resolve())
                        deleted += 1
                except OSError:
                    pass
        except Exception as exc:
            self.logger.warning("cleanup_placeholders failed: %s", exc)
        return deleted

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def _set_status(self, st: SourceState, new: SourceStatus, reason: str = "") -> None:
        old = st.status
        if old == new:
            return
        st.status = new
        st.last_change = time.time()
        st.history.append({
            "from": old.value,
            "to": new.value,
            "ts": st.last_change,
            "reason": reason,
        })
        self.logger.debug("CH%03d status → %s (%s)", st.channel_id, new.value, reason)

        if new == SourceStatus.FALLBACK:
            st.metrics.fallback_entries += 1
            st.metrics.last_fallback_start = time.time()
            st.metrics.switches += 1
        elif old == SourceStatus.FALLBACK and new in (SourceStatus.RECOVERING, SourceStatus.REAL):
            if st.metrics.last_fallback_start:
                st.metrics.total_fallback_time_sec += time.time() - st.metrics.last_fallback_start
            st.metrics.recoveries += 1
            st.metrics.switches += 1

        if self.on_status_change:
            try:
                self.on_status_change(st.channel_id, old, new, reason)
            except Exception as exc:
                self.logger.warning("on_status_change callback error: %s", exc)

    def _apply_real(
        self,
        ch: ChannelContext,
        st: SourceState,
        real: Path | str,
        prev: SourceStatus,
    ) -> None:
        st.real_path = real
        st.active_path = real
        st.last_error = ""
        st.consecutive_failures = 0
        ch.active_source_path = real

        if prev == SourceStatus.FALLBACK:
            self._set_status(st, SourceStatus.RECOVERING, reason="source recovered")
            st.recovering_since = time.time()
        elif prev == SourceStatus.RECOVERING:
            if time.time() - st.recovering_since >= self.fallback.recover_hold_sec:
                self._set_status(st, SourceStatus.REAL, reason="recover hold elapsed")
        elif prev != SourceStatus.REAL:
            self._set_status(st, SourceStatus.REAL, reason="source available")

    def _apply_fallback(
        self,
        ch: ChannelContext,
        st: SourceState,
        prev: SourceStatus,
        reason: str = "no real source",
    ) -> None:
        fb = self._ensure_placeholder(ch)
        if fb is not None:
            self._set_status(st, SourceStatus.FALLBACK, reason=reason)
            st.fallback_path = fb
            st.active_path = fb
            ch.active_source_path = fb
        else:
            self._set_status(st, SourceStatus.FAILED, reason="placeholder generation failed")
            st.active_path = None
            st.last_error = "no real source and placeholder failed"
            st.last_error_time = time.time()

    # ------------------------------------------------------------------
    # Real source detection (file + live)
    # ------------------------------------------------------------------

    def _is_live_url(self, path: str | Path) -> bool:
        s = str(path)
        try:
            parsed = urlparse(s)
            return parsed.scheme.lower() in self.LIVE_SCHEMES
        except Exception:
            return False

    def _pick_real(self, ch: ChannelContext, st: SourceState) -> Optional[Path | str]:
        candidates: List[str | Path] = []
        #enc = getattr(ch, "encoded_path", None)
        #if enc:
        #    candidates.append(enc)
        inp = getattr(ch, "input_path", None)
        if inp:
            candidates.append(inp)

        for cand in candidates:
            key = str(cand)
            if self._is_live_url(key):
                if self._check_live_source(key, st):
                    return key
                continue
            try:
                p = Path(cand)
                if not p.exists() or p.stat().st_size <= 1024:
                    continue
                resolved = p.resolve()
                if resolved in self._placeholder_paths:
                    continue
                try:
                    resolved.relative_to(self._placeholder_dir.resolve())
                    continue
                except ValueError:
                    pass
                if self._validate_media_file(p):
                    return p
            except OSError:
                continue
        return None

    def _check_live_source(self, url: str, st: SourceState) -> bool:
        now = time.time()
        cached = self._probe_cache.get(url)
        if cached and (now - cached[1]) < self.fallback.probe_cache_ttl_sec:
            return cached[0]

        ok = self._ffprobe_live(url)
        self._probe_cache[url] = (ok, now)

        if ok:
            st.consecutive_failures = 0
            return True

        st.consecutive_failures += 1
        st.metrics.probe_failures += 1
        st.last_error = f"live probe failed ({st.consecutive_failures})"
        st.last_error_time = now

        if st.consecutive_failures >= self.fallback.live_fail_threshold:
            st.blacklisted_until = now + self.fallback.blacklist_duration_sec
            self._set_status(st, SourceStatus.BLACKLISTED, reason="circuit breaker")
            self.logger.warning(
                "CH%03d blacklisted for %.0fs after %d failures",
                st.channel_id, self.fallback.blacklist_duration_sec, st.consecutive_failures,
            )
        return False

    def _ffprobe_live(self, url: str) -> bool:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-rtsp_transport", "tcp",
                "-timeout", str(int(self.fallback.live_probe_timeout_sec * 1_000_000)),
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                url,
            ]
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.fallback.live_probe_timeout_sec + 1,
                stdin=subprocess.DEVNULL,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _validate_media_file(self, path: Path) -> bool:
        return True

    # ------------------------------------------------------------------
    # Placeholder generation
    # ------------------------------------------------------------------

    def _video_codec(self) -> str:
        if self._resolved_vcodec:
            return self._resolved_vcodec
        pref = (self.fallback.video_codec or "auto").lower()
        if pref != "auto":
            self._resolved_vcodec = pref
            return pref
        # Prefer software first for reliability; NVENC only if CUDA works.
        try:
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=8,
                stdin=subprocess.DEVNULL,
            )
            text = (p.stdout or "") + (p.stderr or "")
            if "libx264" in text:
                self._resolved_vcodec = "libx264"
            elif "mpeg2video" in text:
                self._resolved_vcodec = "mpeg2video"
            elif "h264_nvenc" in text:
                self._resolved_vcodec = "h264_nvenc"
            else:
                self._resolved_vcodec = "libx264"
        except Exception:
            self._resolved_vcodec = "libx264"
        return self._resolved_vcodec

    def _min_placeholder_bytes(self) -> int:
        raw = (self.fallback.video_bitrate or "1500k").strip().lower()
        try:
            if raw.endswith("m"):
                bps = float(raw[:-1]) * 1_000_000
            elif raw.endswith("k"):
                bps = float(raw[:-1]) * 1_000
            else:
                bps = float("".join(c for c in raw if c.isdigit() or c == "."))
        except Exception:
            bps = 1_500_000.0
        return max(2000, int(self.fallback.duration_sec * bps * 0.04 / 8))

    def _placeholder_valid(self, path: Path) -> bool:
        try:
            if not path.exists() or path.stat().st_size < self._min_placeholder_bytes():
                return False
        except OSError:
            return False
        if not which("ffprobe"):
            return True
        try:
            r = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=12,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                return False
            dur = float((r.stdout or "0").strip() or 0)
            return dur >= max(0.5, self.fallback.duration_sec * 0.35)
        except Exception:
            return True

    def _build_overlay_filter(self, ch: ChannelContext) -> str:
        if not self.fallback.enable_text_overlay:
            return ""
        texts: List[str] = []
        if self.fallback.show_channel_name:
            texts.append(f"CH{ch.channel_id:03d}")
        if self.fallback.overlay_text:
            texts.append(self.fallback.overlay_text)
        if self.fallback.show_timestamp:
            texts.append("%{localtime}")
        if not texts:
            return ""
        fontcolor = self.fallback.overlay_fontcolor
        fontsize = self.fallback.overlay_fontsize
        box = "1" if self.fallback.overlay_box else "0"
        boxcolor = self.fallback.overlay_boxcolor
        main = texts[0]
        filters = [
            f"drawtext=text='{main}':fontsize={fontsize}:fontcolor={fontcolor}:"
            f"box={box}:boxcolor={boxcolor}:x=(w-text_w)/2:y=(h-text_h)/2"
        ]
        for i, t in enumerate(texts[1:], 1):
            y_offset = (fontsize + 12) * i
            filters.append(
                f"drawtext=text='{t}':fontsize={int(fontsize * 0.7)}:fontcolor={fontcolor}:"
                f"box={box}:boxcolor={boxcolor}:x=(w-text_w)/2:y=(h-text_h)/2+{y_offset}"
            )
        return ",".join(filters)

    def _ensure_placeholder(self, ch: ChannelContext) -> Optional[Path]:
        out = self._placeholder_dir / f"ch{ch.channel_id:03d}_{self.fallback.video.value}.ts"
        if self._placeholder_valid(out):
            self._placeholder_paths.add(out.resolve())
            return out
        if out.exists():
            try:
                out.unlink()
            except OSError:
                pass

        vcodec = self._video_codec()
        v_args = ["-c:v", vcodec, "-b:v", self.fallback.video_bitrate]
        if vcodec in ("libx264", "h264_nvenc", "hevc_nvenc"):
            v_args += ["-pix_fmt", "yuv420p"]
            if vcodec == "libx264":
                v_args += ["-preset", "veryfast"]
            elif "nvenc" in vcodec:
                v_args += ["-preset", "p4", "-tune", "ll"]
        elif vcodec == "mpeg2video":
            v_args += ["-pix_fmt", "yuv420p"]

        a_in = self._audio_lavfi()
        overlay = self._build_overlay_filter(ch)
        tmp = out.with_suffix(".ts.tmp")
        retries = max(1, self.fallback.max_placeholder_retries)
        last_err = ""

        for attempt in range(retries):
            if self.fallback.video == FallbackVideo.STILL and self.fallback.still_image:
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                    "-loop", "1", "-i", str(self.fallback.still_image),
                    "-f", "lavfi", "-i", a_in,
                    "-t", str(self.fallback.duration_sec),
                ]
                if overlay:
                    cmd += ["-vf", overlay]
                cmd += [
                    *v_args,
                    "-c:a", "aac", "-b:a", self.fallback.audio_bitrate,
                    "-shortest",
                    "-f", "mpegts", str(tmp),
                ]
            else:
                v_in = self._video_lavfi()
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                    "-f", "lavfi", "-i", v_in,
                    "-f", "lavfi", "-i", a_in,
                    "-t", str(self.fallback.duration_sec),
                ]
                if overlay:
                    cmd += ["-vf", overlay]
                cmd += [
                    *v_args,
                    "-c:a", "aac", "-b:a", self.fallback.audio_bitrate,
                    "-f", "mpegts", str(tmp),
                ]

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    stdin=subprocess.DEVNULL,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
                    last_err = err
                    self.logger.error(
                        "Placeholder CH%03d attempt %d failed: %s",
                        ch.channel_id, attempt + 1, err[:400],
                    )
                    st = self._states.get(ch.channel_id)
                    if st:
                        st.metrics.placeholder_failures += 1
                    # If GPU encoder failed, switch to software for next attempts
                    if "nvenc" in vcodec or "cuda" in err.lower():
                        self._resolved_vcodec = "libx264"
                        vcodec = "libx264"
                        v_args = ["-c:v", "libx264", "-b:v", self.fallback.video_bitrate,
                                  "-pix_fmt", "yuv420p", "-preset", "veryfast"]
                elif self._placeholder_valid(tmp):
                    os.replace(tmp, out)
                    self._placeholder_paths.add(out.resolve())
                    st = self._states.get(ch.channel_id)
                    if st:
                        st.metrics.placeholder_builds += 1
                    self.logger.info("Placeholder ready CH%03d → %s", ch.channel_id, out)
                    return out
                else:
                    last_err = "validation failed"
            except subprocess.TimeoutExpired as exc:
                last_err = f"timeout: {exc}"
            except Exception as exc:
                last_err = str(exc)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

            if attempt + 1 < retries:
                time.sleep(self.fallback.placeholder_retry_backoff_sec * (attempt + 1))

        with self._lock:
            st = self._states.get(ch.channel_id)
            if st:
                st.last_error = last_err
                st.last_error_time = time.time()
                st.metrics.placeholder_failures += 1
        return None

    def _video_lavfi(self) -> str:
        w, h, fps = self.fallback.width, self.fallback.height, self.fallback.fps
        size = f"{w}x{h}"
        if self.fallback.video == FallbackVideo.BLACK:
            return f"color=c=black:s={size}:r={fps}"
        if self.fallback.video == FallbackVideo.TEST_PATTERN:
            return f"testsrc2=size={size}:rate={fps}"
        if self.fallback.video == FallbackVideo.STILL:
            return f"color=c=black:s={size}:r={fps}"
        return f"smptebars=size={size}:rate={fps}"

    def _audio_lavfi(self) -> str:
        if self.fallback.audio == FallbackAudio.SILENCE:
            return "anullsrc=channel_layout=stereo:sample_rate=48000"
        if self.fallback.audio == FallbackAudio.TONE:
            return "sine=frequency=440:sample_rate=48000"
        return "sine=frequency=1000:sample_rate=48000"
