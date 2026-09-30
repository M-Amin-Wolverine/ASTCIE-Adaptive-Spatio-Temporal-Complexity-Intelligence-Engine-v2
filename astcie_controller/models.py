#!/usr/bin/env python3
"""
ASTCIE Media Controller – Core Data Models
==========================================
ChannelContext, ChannelState and shared Enums.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OutputMode(Enum):
    FILE = "file"
    UDP  = "udp"
    RTP  = "rtp"
    SRT  = "srt"
    RIST = "rist"


class EncoderAction(Enum):
    KEEP       = "keep"
    INCREASE   = "increase"
    DECREASE   = "decrease"
    REALLOCATE = "reallocate"
    RECOVER    = "recover"


class EncoderStatus(Enum):
    IDLE      = "idle"
    STARTING  = "starting"
    RUNNING   = "running"
    UPDATING  = "updating"
    STOPPING  = "stopping"
    ERROR     = "error"
    STOPPED   = "stopped"


# ---------------------------------------------------------------------------
# ChannelState  – runtime + analysis metrics for one channel
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    # --- Analysis results (filled after Hybrid) ---
    complexity: float = 0.0
    bdi: float = 0.0
    risk: Optional[float] = None
    regime: Optional[str] = None
    adci_temporal: Optional[float] = None
    adci_spatial: Optional[float] = None
    spatial_score: Optional[float] = None          # from V8.1
    temporal_score: Optional[float] = None         # from V8.1
    motion_score: Optional[float] = None           # from V8.1

    # --- Allocation ---
    priority: float = 1.0                          # used only when --enable-priority
    target_bitrate: float = 0.0                    # Mbps (video only)
    actual_bitrate: float = 0.0                    # Mbps
    audio_bitrate: float = 0.096                   # Mbps (default 96 kbps)
    video_bitrate: float = 0.0                     # usually == target_bitrate

    # Runtime FFmpeg progress
    progress_bitrate: float = 0.0
    progress_fps: float = 0.0
    progress_speed: float = 0.0
    progress_out_time: float = 0.0
    progress_total_size: int = 0
    progress_drop_frames: int = 0
    progress_dup_frames: int = 0
    progress_frame: int = 0
    progress_time: float = 0.0
    progress_updated_at: float = 0.0

    # --- Runtime metrics ---
    quality_metric: Optional[float] = None         # light metric (not VMAF)
    encoder_status: EncoderStatus = EncoderStatus.IDLE
    buffer_state: Optional[str] = None             # "ok" | "low" | "high"
    frame_drops: int = 0
    last_action: EncoderAction = EncoderAction.KEEP

    # --- Timing ---
    timestamp: float = field(default_factory=time.time)
    last_update: float = 0.0
    last_action_time: float = 0.0

    def touch(self) -> None:
        """Update last_update timestamp."""
        self.last_update = time.time()
        self.timestamp = self.last_update


# ---------------------------------------------------------------------------
# ChannelContext – full identity + state of one logical channel
# ---------------------------------------------------------------------------

@dataclass
class ChannelContext:
    channel_id: int
    input_path: Path
    stem: str

    # Raw engine results
    v81: Dict[str, Any] = field(default_factory=dict)
    v9: Dict[str, Any] = field(default_factory=dict)
    hybrid: Dict[str, Any] = field(default_factory=dict)

    # Runtime state
    state: ChannelState = field(default_factory=ChannelState)

    # Paths & process handles
    encoded_path: Optional[Path] = None
    active_source_path: Optional[Path] = None
    analysis_dir: Optional[Path] = None            # unique per-channel analysis folder
    ffmpeg_proc: Any = None                        # subprocess.Popen | None
    ts_pid_video: Optional[int] = None
    ts_pid_audio: Optional[int] = None
    program_number: Optional[int] = None

    # Internal (not shown in repr)
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.analysis_dir is None:
            # Will be set by ChannelManager to a unique path
            self.analysis_dir = Path("analysis") / f"{self.channel_id:03d}_{self.stem}"


# ---------------------------------------------------------------------------
# Global Config (shared across modules)
# ---------------------------------------------------------------------------

@dataclass
class ControllerConfig:
    # Budgets
    link_budget: float = 30.0                      # Mbps
    audio_bitrate: float = 0.096                   # Mbps (96 kbps)
    audio_min: float = 0.064                       # 64 kbps
    audio_max: float = 0.128                       # 128 kbps
    bmin: float = 1.5                              # Mbps
    bmax: float = 8.0                              # Mbps

    # Engines
    v81_script: Path = Path("complexity7.py")
    v9_script: Path = Path("complexity8.py")
    weight_v81: float = 0.5
    weight_v9: float = 0.5

    # Encoding
    preset: str = "medium"
    no_encode: bool = False

    # Output
    output_mode: OutputMode = OutputMode.FILE
    udp_addr: str = "127.0.0.1:1234"
    rtp_addr: str = "127.0.0.1:5004"
    srt_addr: str = "srt://127.0.0.1:9000"
    rist_addr: str = "rist://127.0.0.1:5000"
    output_dir: Path = Path("controller_results")

    # Parallelism & Feedback
    max_workers: int = 4
    feedback_interval: float = 1.0                 # seconds
    hysteresis: float = 0.05                       # 5 %
    min_bitrate_delta: float = 0.05                # relative

    # Feature switches
    enable_priority: bool = False
    adaptive_audio: bool = False
    manual_psi: bool = False
    no_parallel_engines: bool = False

    # Analysis tuning (passed to engines)
    analysis_fps: float = 25.0
    analysis_width: int = 640
    segment_seconds: float = 5.0
