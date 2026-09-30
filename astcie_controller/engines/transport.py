#!/usr/bin/env python3
"""
ASTCIE Transport — Badass Edition v3.0
======================================

Complete production transport layer for the ASTCIE/VECTRA pipeline.

Highlights (v3.0)
-----------------
* HTTP control plane with bearer / basic / IP-allow authentication
* Real multi-variant HLS with encoder ladder and master playlist
* Graceful drain for HLS/DASH (closes playlist cleanly)
* Resource limits via cgroups v2 and/or systemd-run scopes
* Config schema validation (pydantic if available, fallback to built-in)
* WHIP/WebRTC publishing using ffmpeg's whip muxer
* Watchdog to revive dead/stalled supervisors
* Input failover with priority-ordered sources
* SRTP AES-128/192/256 (192/256 downgrade with warning)
* Progress-pipe byte counting, output pacing (-muxrate)
* Structured JSON logging, Prometheus metrics
* ~5000 line self-contained module; fully typed

Sections
--------
   1. Imports & constants
   2. Enumerations
   3. Exception hierarchy
   4. Data models
   5. Utilities
   6. Structured logging
   7. Event bus
   8. Metrics registry
   9. Backoff & circuit breaker
  10. Health checker
  11. Process supervisor (with graceful drain)
  12. Resource limits (cgroups v2 / systemd-run / ulimit)
  13. FFmpeg command builders
  14. Manifest writers
  15. Input failover
  16. Multi-variant HLS helper
  17. SRTP helpers
  18. WHIP / WebRTC helper
  19. Watchdog
  20. Config schema validator
  21. Control auth
  22. HTTP control server
  23. TransportManager
  24. Factories & diagnostics
  25. CLI
  26. Tests (in a companion file, shown at the end)

Author: ASTCIE Team
License: MIT
"""

from __future__ import annotations

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================

import abc
import argparse
import base64
import concurrent.futures as _fut
import contextlib
import dataclasses
import errno
import functools
import hashlib
import http.server
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import platform
import queue
import random
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import typing as t
import urllib.parse
import urllib.request
import uuid
import weakref
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field, fields as _dc_fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any, Callable, Deque, Dict, Iterable, Iterator, List, Mapping,
    MutableMapping, NamedTuple, Optional as Opt, Sequence, Set, Tuple,
    Type, TypeVar, Union,
)
from urllib.parse import quote, urlencode, urlparse, urlunparse

try:  # pragma: no cover
    import yaml  # type: ignore
    _HAVE_YAML = True
except Exception:  # pragma: no cover
    yaml = None  # type: ignore
    _HAVE_YAML = False

try:  # pragma: no cover
    import pydantic  # type: ignore
    _HAVE_PYDANTIC = True
except Exception:  # pragma: no cover
    pydantic = None  # type: ignore
    _HAVE_PYDANTIC = False

try:  # pragma: no cover
    from ..models import ControllerConfig, OutputMode as _LegacyOutputMode
    _HAVE_MODELS = True
except Exception:  # pragma: no cover
    ControllerConfig = None  # type: ignore
    _LegacyOutputMode = None  # type: ignore
    _HAVE_MODELS = False


__version__ = "3.0.0"
__all__ = [
    "TransportManager", "TransportEndpoint", "TransportStats",
    "TransportEvent", "HealthReport", "OutputMode", "TransportState",
    "ProtocolFamily", "ReconnectPolicy", "EncryptionMode", "EventType",
    "HealthStatus", "ManagerConfig", "ControlServer", "ControlAuth",
    "EventBus", "MetricsRegistry", "Backoff", "CircuitBreaker",
    "HealthChecker", "ProcessSupervisor", "CommandBuilder",
    "ManifestWriter", "InputFailover", "Watchdog", "SRTPHelper",
    "WHIPHelper", "ResourceLimits", "ResourceLimiter", "ConfigSchema",
    "MultiVariantHLS",
    "build_endpoint", "endpoint_from_dict", "endpoint_to_dict",
    "make_transport_from_config", "attach_control_server",
    "configure_json_logging", "configure_plain_logging",
    "build_multivariant_hls_endpoint",
    "validate_endpoint_dict", "validate_manager_dict",
]

LOGGER_NAME = "astcie.transport"
DEFAULT_STDERR_BUFFER_LINES = 256
DEFAULT_MAX_RESTARTS = 20
DEFAULT_BASE_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 30.0
DEFAULT_HEALTH_INTERVAL = 5.0
DEFAULT_SUPERVISOR_TICK = 1.0
DEFAULT_WATCHDOG_INTERVAL = 10.0
DEFAULT_STATS_WINDOW = 60
DEFAULT_FFMPEG_BIN = "ffmpeg"
DEFAULT_FFPROBE_BIN = "ffprobe"
DEFAULT_TS_PACKET_SIZE = 188
DEFAULT_UDP_PKT_SIZE = 1316
DEFAULT_UDP_BUFFER = 65_535
DEFAULT_SRT_LATENCY_MS = 120
DEFAULT_RTP_PAYLOAD_TYPE = 33
DEFAULT_RTP_CLOCK = 90_000
DEFAULT_HLS_SEGMENT_SEC = 6
DEFAULT_DASH_SEGMENT_SEC = 4
DEFAULT_WATCHDOG_STALL_SEC = 10.0
DEFAULT_SHUTDOWN_GRACE = 5.0
DEFAULT_DRAIN_TIMEOUT = 8.0
DEFAULT_MAX_CPUS = 4

_RE_HOST_PORT = re.compile(r"^(?P<host>\[[^\]]+\]|[^:]+)(?::(?P<port>\d+))?$")
_RE_SCHEME = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://")
_RE_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_RE_IPV6 = re.compile(r"^\[?[0-9a-fA-F:]+\]?$")
_RE_TEMPLATE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
_RE_HEX = re.compile(r"^[0-9a-fA-F]+$")

PROTOCOL_SCHEMES = (
    "rtmps", "rtmp", "srt", "rist", "rtp", "srtp", "udp", "http",
    "https", "icecast", "unix", "tcp", "whip",
)

RESERVED_KEYS_FOR_PROGRESS = frozenset({
    "frame", "fps", "stream_0_0_q", "bitrate", "total_size",
    "out_time_us", "out_time_ms", "out_time", "dup_frames",
    "drop_frames", "speed", "progress",
})


# ============================================================================
# 2. ENUMERATIONS
# ============================================================================

class OutputMode(str, Enum):
    FILE = "file"
    UDP = "udp"
    RTP = "rtp"
    SRTP = "srtp"
    SRT = "srt"
    RIST = "rist"
    RTMP = "rtmp"
    RTMPS = "rtmps"
    HLS = "hls"
    DASH = "dash"
    HTTP = "http"
    HTTPS = "https"
    TCP = "tcp"
    UNIX = "unix"
    ICECAST = "icecast"
    WHIP = "whip"
    NULL = "null"

    @classmethod
    def from_str(cls, value: str) -> "OutputMode":
        v = (value or "").strip().lower()
        for m in cls:
            if m.value == v:
                return m
        aliases = {
            "udp_ts": cls.UDP, "rtp_mpegts": cls.RTP,
            "srt_caller": cls.SRT, "srt_listener": cls.SRT,
            "rist_simple": cls.RIST, "rist_main": cls.RIST,
            "dash_live": cls.DASH, "hls_live": cls.HLS,
            "webrtc": cls.WHIP, "file_ts": cls.FILE,
        }
        if v in aliases:
            return aliases[v]
        raise ValidationError(f"Unknown OutputMode: {value!r}")

    @property
    def is_file(self) -> bool:
        return self in (OutputMode.FILE, OutputMode.HLS, OutputMode.DASH)

    @property
    def is_streaming(self) -> bool:
        return not self.is_file and self != OutputMode.NULL

    @property
    def is_ts_mux(self) -> bool:
        return self in (
            OutputMode.FILE, OutputMode.UDP, OutputMode.RTP,
            OutputMode.SRTP, OutputMode.SRT, OutputMode.RIST,
            OutputMode.TCP, OutputMode.UNIX, OutputMode.HTTP,
            OutputMode.HTTPS,
        )

    @property
    def needs_graceful_drain(self) -> bool:
        return self in (OutputMode.HLS, OutputMode.DASH)


class TransportState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    DISABLED = "disabled"

    @property
    def terminal(self) -> bool:
        return self in (TransportState.STOPPED, TransportState.ERROR,
                        TransportState.DISABLED)


class ProtocolFamily(str, Enum):
    LOCAL = "local"
    UNICAST = "unicast"
    MULTICAST = "multicast"
    BROADCAST = "broadcast"
    RELIABLE = "reliable"
    PULL = "pull"
    PUSH = "push"


class EncryptionMode(str, Enum):
    NONE = "none"
    AES128 = "aes128"
    AES192 = "aes192"
    AES256 = "aes256"
    AES_GCM = "aes-gcm"

    @property
    def key_len_bytes(self) -> int:
        return {"aes128": 16, "aes192": 24, "aes256": 32,
                "aes-gcm": 16, "none": 0}[self.value]

    @property
    def salt_len_bytes(self) -> int:
        return {"aes128": 14, "aes192": 14, "aes256": 14,
                "aes-gcm": 12, "none": 0}[self.value]

    @property
    def srtp_suite(self) -> str:
        """Map to ffmpeg SRTP suite name. AES-192/256 downgrade to AES-128."""
        return {
            "aes128": "AES_CM_128_HMAC_SHA1_80",
            "aes192": "AES_CM_128_HMAC_SHA1_80",
            "aes256": "AES_CM_128_HMAC_SHA1_80",
            "aes-gcm": "AEAD_AES_128_GCM",
            "none": "AES_CM_128_HMAC_SHA1_80",
        }[self.value]

    @property
    def srtp_downgraded(self) -> bool:
        return self in (EncryptionMode.AES192, EncryptionMode.AES256)


class ReconnectPolicy(str, Enum):
    NEVER = "never"
    ALWAYS = "always"
    ON_FAILURE = "on_failure"
    ON_TIMEOUT = "on_timeout"
    CIRCUIT_BREAK = "circuit_break"


class EventType(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    STARTED = "started"
    STATS = "stats"
    HEALTH = "health"
    WARN = "warn"
    ERROR = "error"
    RESTARTING = "restarting"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    SUPERVISOR_TICK = "supervisor_tick"
    WATCHDOG_TICK = "watchdog_tick"
    BREAKER_OPEN = "breaker_open"
    BREAKER_CLOSED = "breaker_closed"
    FAILOVER = "failover"
    CONFIG = "config"
    SCHEMA = "schema"
    CUSTOM = "custom"


class HealthStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    DEAD = "dead"

    @property
    def numeric(self) -> float:
        return {"ok": 1.0, "degraded": 0.5, "unknown": 0.25,
                "failing": 0.0, "dead": 0.0}[self.value]


class ResourceLimitKind(str, Enum):
    NONE = "none"
    CGROUP_V2 = "cgroup_v2"
    SYSTEMD_RUN = "systemd_run"
    RLIMIT = "rlimit"


# ============================================================================
# 3. EXCEPTION HIERARCHY
# ============================================================================

class TransportError(Exception):
    def __init__(self, message: str, *,
                 endpoint: Opt[str] = None,
                 cause: Opt[BaseException] = None):
        super().__init__(message)
        self.endpoint = endpoint
        self.cause = cause

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} [endpoint={self.endpoint}]" if self.endpoint else base


class EndpointError(TransportError): pass
class ProcessError(TransportError): pass
class ValidationError(TransportError): pass
class CircuitOpenError(TransportError): pass
class TimeoutError_(TransportError): pass
class UnsupportedProtocolError(TransportError): pass
class WatchdogError(TransportError): pass
class AuthError(TransportError): pass
class ResourceLimitError(TransportError): pass
class SchemaError(ValidationError): pass


# ============================================================================
# 4. DATA MODELS
# ============================================================================

@dataclass
class ResourceLimits:
    """Per-child resource limits."""
    memory_mb: int = 0                # 0 = unlimited
    cpu_quota_pct: int = 0            # 0 = unlimited (100 = 1 core)
    cpu_affinity: List[int] = field(default_factory=list)
    tasks_max: int = 0                # 0 = unlimited
    io_weight: int = 0                # 0 = default (cgroup v2 io.weight)
    nice: int = 0                     # -20..19
    ionice_class: int = 0             # 0..3
    ionice_priority: int = 0          # 0..7
    nofile: int = 0                   # 0 = inherit
    kind: ResourceLimitKind = ResourceLimitKind.NONE

    def is_active(self) -> bool:
        return any((self.memory_mb, self.cpu_quota_pct, self.cpu_affinity,
                    self.tasks_max, self.io_weight, self.nice,
                    self.nofile))


@dataclass
class TransportEndpoint:
    """A single destination.  Carries every option for every protocol."""
    mode: OutputMode
    name: str = ""
    target: str = ""
    output_path: Opt[Path] = None

    # Common
    enabled: bool = True
    priority: int = 100
    family: Opt[ProtocolFamily] = None

    # UDP / RTP
    pkt_size: int = DEFAULT_UDP_PKT_SIZE
    buffer_size: int = DEFAULT_UDP_BUFFER
    localaddr: str = ""
    multicast: bool = False
    ttl: int = 1
    interface: str = ""
    tos: int = 0

    # RTP / SRTP
    sdp_path: Opt[Path] = None
    payload_type: int = DEFAULT_RTP_PAYLOAD_TYPE
    clock_rate: int = DEFAULT_RTP_CLOCK
    ssrc: Opt[int] = None
    srtp_key: str = ""
    srtp_salt: str = ""

    # SRT
    srt_mode: str = "caller"
    srt_latency_ms: int = DEFAULT_SRT_LATENCY_MS
    srt_passphrase: str = ""
    srt_pbkeylen: int = 0
    srt_streamid: str = ""
    srt_maxbw: int = -1

    # RIST
    rist_profile: int = 1
    rist_secret: str = ""
    rist_buffer_ms: int = 1000
    rist_recovery_maxbitrate: int = 0

    # RTMP(S)
    rtmp_app: str = "live"
    rtmp_stream_key: str = ""
    rtmp_user: str = ""
    rtmp_password: str = ""

    # HTTP(S) / Icecast
    http_method: str = "PUT"
    http_headers: Dict[str, str] = field(default_factory=dict)
    http_user: str = ""
    http_password: str = ""
    http_token: str = ""
    icecast_mount: str = "/stream"
    icecast_name: str = "ASTCIE"
    icecast_genre: str = "various"
    icecast_description: str = ""
    icecast_public: bool = False

    # HLS / DASH
    segment_sec: float = DEFAULT_HLS_SEGMENT_SEC
    playlist_size: int = 6
    hls_variant: str = ""
    hls_master: bool = False
    hls_variants: List[Dict[str, Any]] = field(default_factory=list)
    hls_drain_timeout_sec: float = DEFAULT_DRAIN_TIMEOUT
    dash_manifest: Opt[Path] = None
    dash_drain_timeout_sec: float = DEFAULT_DRAIN_TIMEOUT

    # Encryption
    encryption: EncryptionMode = EncryptionMode.NONE
    encryption_key: str = ""
    encryption_iv: str = ""
    encryption_url: str = ""

    # Reliability
    reconnect_policy: ReconnectPolicy = ReconnectPolicy.ON_FAILURE
    max_restarts: int = DEFAULT_MAX_RESTARTS
    base_backoff_sec: float = DEFAULT_BASE_BACKOFF
    max_backoff_sec: float = DEFAULT_MAX_BACKOFF
    restart_jitter: float = 0.25
    breaker_threshold: int = 5
    breaker_cooldown_sec: float = 60.0

    # Pacing
    rate_limit_kbps: int = 0
    read_rate: bool = True
    extra_muxrate_pct: float = 1.05

    # Health
    health_enabled: bool = True
    health_interval_sec: float = DEFAULT_HEALTH_INTERVAL
    health_timeout_sec: float = 3.0
    health_probe: str = "process"

    # Failover
    failover_inputs: List[str] = field(default_factory=list)
    failover_probe_sec: float = 2.0

    # Resource limits
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    # WHIP / WebRTC
    whip_bearer: str = ""
    whip_stun: str = "stun:stun.l.google.com:19302"
    whip_ice_server_extra: List[str] = field(default_factory=list)
    whip_timeout_sec: float = 20.0

    # Misc
    extra_ffmpeg_args: List[str] = field(default_factory=list)
    extra_input_args: List[str] = field(default_factory=list)
    tags: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for f in _dc_fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Enum):
                v = v.value
            elif isinstance(v, Path):
                v = str(v)
            elif isinstance(v, ResourceLimits):
                v = {kk: (vv.value if isinstance(vv, Enum) else vv)
                     for kk, vv in v.to_dict().items()}
            elif isinstance(v, dict):
                v = dict(v)
            elif isinstance(v, list):
                v = [dict(x) if isinstance(x, dict) else x for x in v]
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TransportEndpoint":
        kwargs: Dict[str, Any] = {}
        for f in _dc_fields(cls):
            if f.name not in data:
                continue
            v = data[f.name]
            if f.name == "mode":
                v = v if isinstance(v, OutputMode) else OutputMode.from_str(v)
            elif f.name in ("output_path", "sdp_path", "dash_manifest") and v:
                v = Path(v)
            elif f.name == "encryption":
                v = v if isinstance(v, EncryptionMode) else EncryptionMode(v)
            elif f.name == "reconnect_policy":
                v = v if isinstance(v, ReconnectPolicy) else ReconnectPolicy(v)
            elif f.name == "family" and v:
                v = v if isinstance(v, ProtocolFamily) else ProtocolFamily(v)
            elif f.name == "resource_limits" and v:
                v = (v if isinstance(v, ResourceLimits)
                     else ResourceLimits(**v))
            kwargs[f.name] = v
        return cls(**kwargs)

    @property
    def key(self) -> str:
        base = self.target or (str(self.output_path) if self.output_path else "")
        return f"{self.mode.value}:{base or self.name or 'default'}"

    @property
    def display(self) -> str:
        return self.name or self.target or str(self.output_path or self.mode.value)

    def resolved_family(self) -> ProtocolFamily:
        if self.family is not None:
            return self.family
        if self.mode in (OutputMode.FILE, OutputMode.UNIX, OutputMode.NULL):
            return ProtocolFamily.LOCAL
        if self.mode in (OutputMode.SRT, OutputMode.RIST):
            return ProtocolFamily.RELIABLE
        if self.mode == OutputMode.UDP:
            host, _ = split_host_port(self.target, 0)
            return (ProtocolFamily.MULTICAST if is_multicast(host)
                    else ProtocolFamily.UNICAST)
        if self.mode in (OutputMode.RTP, OutputMode.SRTP, OutputMode.TCP):
            return ProtocolFamily.UNICAST
        return ProtocolFamily.PUSH

    def validate(self) -> None:
        if self.mode == OutputMode.FILE:
            if not self.output_path:
                raise ValidationError("FILE endpoint requires output_path",
                                      endpoint=self.key)
        elif self.mode in (OutputMode.HLS, OutputMode.DASH):
            if not self.output_path:
                raise ValidationError(
                    f"{self.mode.value.upper()} requires output_path",
                    endpoint=self.key)
        elif self.mode != OutputMode.NULL and not self.target:
            raise ValidationError(
                f"{self.mode.value.upper()} requires target",
                endpoint=self.key)

        if self.pkt_size <= 0:
            raise ValidationError("pkt_size must be > 0", endpoint=self.key)
        if self.max_restarts < 0:
            raise ValidationError("max_restarts must be >= 0", endpoint=self.key)
        if self.segment_sec <= 0:
            raise ValidationError("segment_sec must be > 0", endpoint=self.key)
        if self.encryption != EncryptionMode.NONE and not self.encryption_key:
            raise ValidationError(
                "encryption_key required when encryption != none",
                endpoint=self.key)
        if self.mode == OutputMode.SRTP:
            if not self.srtp_key:
                raise ValidationError(
                    "SRTP requires srtp_key (hex)", endpoint=self.key)
            if not _RE_HEX.match(self.srtp_key):
                raise ValidationError("srtp_key must be hex", endpoint=self.key)
        if self.mode == OutputMode.HLS and self.hls_variants and not self.hls_master:
            raise ValidationError(
                "hls_variants provided but hls_master is False — "
                "set hls_master=True to enable multi-variant encoding",
                endpoint=self.key)


# --- Stats ----------------------------------------------------------------

@dataclass
class TransportStats:
    mode: str = ""
    target: str = ""
    state: str = TransportState.IDLE.value
    start_time: float = 0.0
    last_change: float = 0.0
    uptime_sec: float = 0.0
    restarts: int = 0
    total_restarts: int = 0
    last_error: str = ""
    last_stderr_line: str = ""
    last_stdout_line: str = ""
    last_stderr_ts: float = 0.0
    last_stdout_ts: float = 0.0
    last_progress_ts: float = 0.0
    bytes_sent: int = 0
    bytes_hint: int = 0
    out_time_us: int = 0
    fps: float = 0.0
    speed: float = 0.0
    bitrate_kbps: float = 0.0
    packets_hint: int = 0
    samples: Deque[Tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_STATS_WINDOW))
    consecutive_failures: int = 0
    last_probe_latency_ms: float = 0.0
    drain_count: int = 0
    drain_last_dur_sec: float = 0.0

    def mark(self, state: TransportState) -> None:
        self.state = state.value
        self.last_change = time.time()
        if state == TransportState.RUNNING and not self.start_time:
            self.start_time = time.time()

    def add_bytes(self, n: int) -> None:
        self.bytes_sent += n
        self.samples.append((time.time(), n))

    def compute_bitrate(self, window_sec: float = 5.0) -> float:
        if not self.samples:
            return self.bitrate_kbps
        now = time.time()
        cutoff = now - window_sec
        total = 0
        count = 0
        for ts, n in self.samples:
            if ts >= cutoff:
                total += n
                count += 1
        if count == 0:
            return self.bitrate_kbps
        return (total * 8.0) / window_sec / 1000.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "target": self.target,
            "state": self.state,
            "uptime_sec": round(self.uptime_sec, 1),
            "restarts": self.restarts,
            "total_restarts": self.total_restarts,
            "last_error": self.last_error[:160] if self.last_error else "",
            "bitrate_kbps": round(self.bitrate_kbps, 2),
            "bytes_sent": self.bytes_sent,
            "fps": self.fps,
            "speed": self.speed,
            "consecutive_failures": self.consecutive_failures,
            "drain_count": self.drain_count,
            "drain_last_dur_sec": self.drain_last_dur_sec,
        }


@dataclass
class HealthReport:
    status: HealthStatus = HealthStatus.UNKNOWN
    latency_ms: float = 0.0
    message: str = ""
    checked_at: float = 0.0
    probe: str = ""

    def is_ok(self) -> bool:
        return self.status in (HealthStatus.OK, HealthStatus.DEGRADED)


@dataclass
class TransportEvent:
    type: EventType
    key: str = ""
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "key": self.key,
            "message": self.message,
            "payload": self.payload,
            "ts": self.ts,
            "iso": datetime.fromtimestamp(
                self.ts, tz=timezone.utc).isoformat(),
        }


@dataclass
class _OutputRuntime:
    endpoint: TransportEndpoint
    proc: Opt[subprocess.Popen] = None
    stderr_thread: Opt[threading.Thread] = None
    stdout_thread: Opt[threading.Thread] = None
    health_thread: Opt[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    restart_lock: threading.RLock = field(default_factory=threading.RLock)
    restart_pending: bool = False
    breaker: Opt["CircuitBreaker"] = None
    backoff: Opt["Backoff"] = None
    stats: TransportStats = field(default_factory=TransportStats)
    health: HealthReport = field(default_factory=HealthReport)
    created_at: float = field(default_factory=time.time)
    last_start_attempt: float = 0.0
    generation: int = 0
    stderr_buffer: Deque[str] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_STDERR_BUFFER_LINES))
    env_overrides: Dict[str, str] = field(default_factory=dict)
    last_bytes_snapshot: int = 0
    current_input_index: int = 0
    failover_since: float = 0.0
    _health_checker: Any = None
    _failover: Any = None
    _cgroup_path: Opt[Path] = None
    _systemd_unit: Opt[str] = None
    drain_started_at: float = 0.0


@dataclass
class ManagerConfig:
    """Global configuration."""
    ffmpeg_bin: str = DEFAULT_FFMPEG_BIN
    ffprobe_bin: str = DEFAULT_FFPROBE_BIN
    work_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "out")
    log_level: int = logging.INFO
    supervisor_tick_sec: float = DEFAULT_SUPERVISOR_TICK
    watchdog_interval_sec: float = DEFAULT_WATCHDOG_INTERVAL
    watchdog_stall_sec: float = DEFAULT_WATCHDOG_STALL_SEC
    shutdown_grace_sec: float = DEFAULT_SHUTDOWN_GRACE
    drain_timeout_sec: float = DEFAULT_DRAIN_TIMEOUT
    default_max_restarts: int = DEFAULT_MAX_RESTARTS
    default_backoff_base: float = DEFAULT_BASE_BACKOFF
    default_backoff_max: float = DEFAULT_MAX_BACKOFF
    global_rate_limit_kbps: int = 0
    enable_health_checks: bool = True
    enable_metrics: bool = True
    enable_events: bool = True
    enable_watchdog: bool = True
    enable_resource_limits: bool = True
    json_logging: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    extra_ffmpeg_global_args: List[str] = field(default_factory=list)
    max_concurrent_processes: int = 64
    cgroup_root: Path = field(default_factory=lambda: Path("/sys/fs/cgroup"))

    # Legacy
    udp_addr: str = "239.0.0.1:1234"
    rtp_addr: str = "127.0.0.1:5004"
    srt_addr: str = "srt://127.0.0.1:9000"
    rist_addr: str = "rist://127.0.0.1:5000"
    rtmp_addr: str = "rtmp://127.0.0.1/live/stream"
    http_addr: str = "http://127.0.0.1:8080/feed"
    no_encode: bool = False


# ============================================================================
# 5. UTILITIES
# ============================================================================

def split_host_port(target: str, default_port: int) -> Tuple[str, int]:
    t = (target or "").strip()
    m = _RE_SCHEME.match(t)
    if m:
        t = t[m.end():]
    t = t.split("/", 1)[0].split("?", 1)[0]
    if t.startswith("[") and "]" in t:
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    if ":" in t:
        host, _, port_s = t.rpartition(":")
        if port_s.isdigit():
            return (host or "127.0.0.1"), int(port_s)
        return t, default_port
    return (t or "127.0.0.1"), default_port


def is_multicast(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_multicast
    except Exception:
        return False


def is_broadcast(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")) == \
            ipaddress.ip_address("255.255.255.255")
    except Exception:
        return host == "255.255.255.255"


def is_ipv6(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host.strip("[]"))
        return True
    except Exception:
        return False


def normalize_host(host: str) -> str:
    h = host.strip()
    if is_ipv6(h) and not h.startswith("["):
        return f"[{h}]"
    return h


def add_query(url: str, params: Mapping[str, Any]) -> str:
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode({k: v for k, v in params.items()
                                  if v is not None and v != ""})


def build_url(scheme: str, host: str, port: int,
              path: str = "", query: Opt[Mapping[str, Any]] = None) -> str:
    host = normalize_host(host)
    base = f"{scheme}://{host}"
    if port:
        base += f":{port}"
    if path:
        if not path.startswith("/"):
            path = "/" + path
        base += path
    return add_query(base, query or {})


def redact_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":***@")
            return urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass
    return url


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0  # type: ignore
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}PB"


def human_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{sec/60:.1f}m"
    if sec < 86400:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def shorten(text: str, n: int = 120) -> str:
    if len(text) <= n:
        return text
    return text[:n - 1] + "…"


def validate_port(p: int) -> None:
    if not (0 < p < 65536):
        raise ValidationError(f"port out of range: {p}")


def validate_path_writable(p: Path) -> None:
    p = Path(p)
    parent = p.parent if p.suffix else p
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise ValidationError(f"cannot create directory: {parent} ({exc})")


def ensure_ffmpeg(binary: str = DEFAULT_FFMPEG_BIN) -> str:
    path = shutil.which(binary)
    if not path:
        raise ProcessError(f"ffmpeg not found on PATH ({binary!r})")
    return path


def expand_template(template: str, ctx: Mapping[str, Any]) -> str:
    now = datetime.now()
    builtins = {
        "timestamp": str(int(time.time())),
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "uuid": uuid.uuid4().hex,
        "pid": str(os.getpid()),
        "hostname": socket.gethostname(),
    }

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        if key in builtins:
            return builtins[key]
        return m.group(0)

    return _RE_TEMPLATE.sub(_sub, template)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
    return h.hexdigest()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_kv_list(items: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


def hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.strip())


def bytes_to_hex(b: bytes) -> str:
    return b.hex()


def random_hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


def redact_hex(s: str, keep: int = 4) -> str:
    if not s or len(s) <= keep * 2:
        return "***"
    return f"{s[:keep]}…{s[-keep:]}"


def constant_time_eq(a: str, b: str) -> bool:
    try:
        return secrets.compare_digest(a.encode("utf-8"),
                                      b.encode("utf-8"))
    except Exception:
        return False


def parse_size(s: Union[str, int]) -> int:
    """Parse '64M', '2G', '512k', '1024' into bytes."""
    if isinstance(s, int):
        return s
    m = re.match(r"^(\d+)([kKmMgG]?)$", str(s).strip())
    if not m:
        raise ValidationError(f"bad size: {s!r}")
    val = int(m.group(1))
    unit = (m.group(2) or "").lower()
    return val * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]


# ============================================================================
# 6. STRUCTURED LOGGING
# ============================================================================

class _JSONFormatter(logging.Formatter):
    _RESERVED = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    })

    def __init__(self, service: str = "astcie.transport"):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
            "pid": record.process,
            "thread": record.threadName,
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                try:
                    json.dumps(v)
                    entry[k] = v
                except (TypeError, ValueError):
                    entry[k] = str(v)
        return json.dumps(entry, ensure_ascii=False)


def configure_json_logging(
    logger: Opt[logging.Logger] = None,
    level: int = logging.INFO,
    service: str = "astcie.transport",
) -> logging.Logger:
    lg = logger or logging.getLogger(LOGGER_NAME)
    lg.setLevel(level)
    for h in list(lg.handlers):
        if getattr(h, "_astcie_json", False):
            lg.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JSONFormatter(service=service))
    handler._astcie_json = True  # type: ignore[attr-defined]
    lg.addHandler(handler)
    lg.propagate = False
    return lg


def configure_plain_logging(
    logger: Opt[logging.Logger] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    lg = logger or logging.getLogger(LOGGER_NAME)
    lg.setLevel(level)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
        lg.addHandler(h)
    return lg


# ============================================================================
# 7. EVENT BUS
# ============================================================================

class EventBus:
    def __init__(self, *, history: int = 256, name: str = "transport-events"):
        self._lock = threading.RLock()
        self._subs: "weakref.WeakSet[_EventSubscriber]" = weakref.WeakSet()
        self._history: Deque[TransportEvent] = deque(maxlen=history)
        self._closed = False
        self.name = name

    def subscribe(self, maxsize: int = 512) -> "_EventSubscriber":
        sub = _EventSubscriber(self, maxsize=maxsize)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: "_EventSubscriber") -> None:
        with self._lock:
            self._subs.discard(sub)

    def publish(self, event: TransportEvent) -> None:
        if self._closed:
            return
        with self._lock:
            self._history.append(event)
            subs = list(self._subs)
        for s in subs:
            s._offer(event)

    def emit(self, type_: EventType, *, key: str = "",
             message: str = "", **payload: Any) -> TransportEvent:
        ev = TransportEvent(type=type_, key=key, message=message,
                            payload=dict(payload))
        self.publish(ev)
        return ev

    def history(self, limit: int = 64) -> List[TransportEvent]:
        with self._lock:
            h = list(self._history)
        return h[-limit:]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for s in subs:
            s.close()


class _EventSubscriber:
    def __init__(self, bus: EventBus, maxsize: int = 512):
        self._bus = bus
        self._q: "queue.Queue[Opt[TransportEvent]]" = queue.Queue(maxsize=maxsize)
        self._closed = False

    def _offer(self, ev: TransportEvent) -> None:
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(ev)
            except Exception:
                pass

    def get(self, timeout: Opt[float] = None) -> Opt[TransportEvent]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> List[TransportEvent]:
        out: List[TransportEvent] = []
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            if ev is None:
                break
            out.append(ev)
        return out

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            self._q.put_nowait(None)

    def __iter__(self) -> Iterator[TransportEvent]:
        while not self._closed:
            ev = self.get(timeout=1.0)
            if ev is not None:
                yield ev


# ============================================================================
# 8. METRICS REGISTRY
# ============================================================================

class _Counter:
    __slots__ = ("name", "help", "labels", "_values", "_lock")

    def __init__(self, name: str, help_: str = "", labels: Sequence[str] = ()):
        self.name = name
        self.help = help_
        self.labels = tuple(labels)
        self._values: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        with self._lock:
            self._values[key] += amount

    def get(self, **label_values: str) -> float:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} counter"]
        with self._lock:
            for key, val in self._values.items():
                if self.labels:
                    lbl = ",".join(
                        f'{l}="{v}"' for l, v in zip(self.labels, key))
                    lines.append(f"{self.name}{{{lbl}}} {val}")
                else:
                    lines.append(f"{self.name} {val}")
        return "\n".join(lines)


class _Gauge:
    __slots__ = ("name", "help", "labels", "_values", "_lock")

    def __init__(self, name: str, help_: str = "", labels: Sequence[str] = ()):
        self.name = name
        self.help = help_
        self.labels = tuple(labels)
        self._values: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **label_values: str) -> None:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        with self._lock:
            self._values[key] = value

    def get(self, **label_values: str) -> float:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} gauge"]
        with self._lock:
            for key, val in self._values.items():
                if self.labels:
                    lbl = ",".join(
                        f'{l}="{v}"' for l, v in zip(self.labels, key))
                    lines.append(f"{self.name}{{{lbl}}} {val}")
                else:
                    lines.append(f"{self.name} {val}")
        return "\n".join(lines)


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: Dict[str, _Counter] = {}
        self._gauges: Dict[str, _Gauge] = {}
        self._lock = threading.RLock()

        self.transport_starts = self.counter(
            "astcie_transport_starts_total",
            "Total process starts", ["mode", "key"])
        self.transport_stops = self.counter(
            "astcie_transport_stops_total", "Total stops", ["mode", "key"])
        self.transport_errors = self.counter(
            "astcie_transport_errors_total", "Total errors",
            ["mode", "key", "kind"])
        self.transport_restarts = self.counter(
            "astcie_transport_restarts_total", "Total restarts",
            ["mode", "key"])
        self.transport_failovers = self.counter(
            "astcie_transport_failovers_total",
            "Total input failovers", ["mode", "key"])
        self.transport_drains = self.counter(
            "astcie_transport_drains_total",
            "Total graceful drains", ["mode", "key"])
        self.transport_auth_failures = self.counter(
            "astcie_transport_auth_failures_total",
            "Control server auth failures", ["reason"])
        self.transport_schema_failures = self.counter(
            "astcie_transport_schema_failures_total",
            "Config schema validation failures", ["kind"])
        self.transport_running = self.gauge(
            "astcie_transport_running",
            "1 if running else 0", ["mode", "key"])
        self.transport_uptime = self.gauge(
            "astcie_transport_uptime_seconds",
            "Endpoint uptime in seconds", ["mode", "key"])
        self.transport_bitrate = self.gauge(
            "astcie_transport_bitrate_kbps",
            "Current bitrate kbps", ["mode", "key"])
        self.transport_bytes_total = self.counter(
            "astcie_transport_bytes_total", "Total bytes sent",
            ["mode", "key"])
        self.transport_breaker_open = self.gauge(
            "astcie_transport_breaker_open",
            "1 if breaker open else 0", ["mode", "key"])
        self.transport_health = self.gauge(
            "astcie_transport_health",
            "Health status (1=ok, 0.5=degraded, 0=failing)", ["mode", "key"])
        self.watchdog_respawns = self.counter(
            "astcie_transport_watchdog_respawns_total",
            "Times the watchdog respawned a supervisor", [])
        self.active_processes = self.gauge(
            "astcie_transport_active_processes",
            "Number of ffmpeg children alive", [])

    def counter(self, name: str, help_: str = "",
                labels: Sequence[str] = ()) -> _Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = _Counter(name, help_, labels)
            return self._counters[name]

    def gauge(self, name: str, help_: str = "",
              labels: Sequence[str] = ()) -> _Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = _Gauge(name, help_, labels)
            return self._gauges[name]

    def render(self) -> str:
        with self._lock:
            counters = list(self._counters.values())
            gauges = list(self._gauges.values())
        chunks = [c.render() for c in counters] + [g.render() for g in gauges]
        return "\n".join(chunks) + "\n"

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": {n: dict(c._values) for n, c in self._counters.items()},
                "gauges": {n: dict(g._values) for n, g in self._gauges.items()},
            }

    def write_to_file(self, path: Path) -> None:
        Path(path).write_text(self.render(), encoding="utf-8")


# ============================================================================
# 9. BACKOFF & CIRCUIT BREAKER
# ============================================================================

class Backoff:
    def __init__(self, base: float = 1.0, factor: float = 2.0,
                 max_: float = 30.0, jitter: float = 0.25):
        self.base = max(0.01, base)
        self.factor = max(1.0, factor)
        self.max = max(self.base, max_)
        self.jitter = max(0.0, min(1.0, jitter))
        self._attempts = 0
        self._lock = threading.Lock()

    def next_delay(self) -> float:
        with self._lock:
            self._attempts += 1
            attempt = self._attempts
        raw = min(self.max, self.base * (self.factor ** (attempt - 1)))
        if self.jitter:
            delta = raw * self.jitter
            raw += random.uniform(-delta, delta)
        return max(0.0, raw)

    def reset(self) -> None:
        with self._lock:
            self._attempts = 0

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 5, cooldown: float = 60.0,
                 name: str = ""):
        self.threshold = max(1, threshold)
        self.cooldown = max(0.1, cooldown)
        self.name = name
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.cooldown:
                    self._state = self.HALF_OPEN
                    return True
                return False
            return True

    def on_success(self) -> None:
        with self._lock:
            self._state = self.CLOSED
            self._failures = 0
            self._opened_at = 0.0

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN:
                self._state = self.OPEN
                self._opened_at = time.time()
                return
            if self._failures >= self.threshold and self._state != self.OPEN:
                self._state = self.OPEN
                self._opened_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._state = self.CLOSED
            self._failures = 0
            self._opened_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "opened_at": self._opened_at,
            }


# ============================================================================
# 10. HEALTH CHECKER
# ============================================================================

class HealthChecker:
    def __init__(self, endpoint: TransportEndpoint,
                 probe_fn: Opt[Callable[[], HealthReport]] = None):
        self.endpoint = endpoint
        self._probe_fn = probe_fn
        self._stop = threading.Event()
        self._thread: Opt[threading.Thread] = None
        self._report = HealthReport(
            status=HealthStatus.UNKNOWN, probe=endpoint.health_probe)
        self._lock = threading.RLock()
        self._on_report: Opt[Callable[[HealthReport], None]] = None

    def set_callback(self, cb: Callable[[HealthReport], None]) -> None:
        self._on_report = cb

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="health-" + self.endpoint.key,
            daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def report(self) -> HealthReport:
        with self._lock:
            return dataclasses.replace(self._report)

    def _loop(self) -> None:
        while not self._stop.wait(self.endpoint.health_interval_sec):
            try:
                rep = (self._probe_fn() if self._probe_fn
                       else self._legacy_probe())
            except Exception as exc:
                rep = HealthReport(
                    status=HealthStatus.FAILING,
                    message=f"probe error: {exc}",
                    probe=self.endpoint.health_probe,
                    checked_at=time.time())
            with self._lock:
                self._report = rep
            if self._on_report:
                with contextlib.suppress(Exception):
                    self._on_report(rep)

    def _legacy_probe(self) -> HealthReport:
        mode = self.endpoint.health_probe
        t0 = time.time()
        if mode == "none":
            return HealthReport(HealthStatus.OK, message="disabled",
                                probe=mode, checked_at=time.time())
        if mode == "tcp":
            host, port = split_host_port(self.endpoint.target, 0)
            if not port:
                return HealthReport(HealthStatus.UNKNOWN,
                                    message="no port for tcp probe",
                                    probe=mode, checked_at=time.time())
            ok, _, msg = self._tcp_probe(host, port)
            return HealthReport(
                HealthStatus.OK if ok else HealthStatus.FAILING,
                latency_ms=(time.time() - t0) * 1000.0,
                message=msg, probe=mode, checked_at=time.time())
        return HealthReport(HealthStatus.OK, message="assumed ok",
                            probe=mode, checked_at=time.time())

    def _tcp_probe(self, host: str, port: int,
                   timeout: Opt[float] = None) -> Tuple[bool, float, str]:
        timeout = timeout or self.endpoint.health_timeout_sec
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, time.time() - t0, "connected"
        except Exception as exc:
            return False, time.time() - t0, f"tcp failed: {exc}"


# ============================================================================
# 11. PROCESS SUPERVISOR (with graceful drain)
# ============================================================================

class ProcessSupervisor:
    def __init__(self, runtime: _OutputRuntime, *,
                 manager: "TransportManager"):
        self.rt = runtime
        self.mgr = manager
        self.ep = runtime.endpoint
        self._spawn_lock = threading.RLock()

    def _stdin_pipe(self) -> int:
        # HLS/DASH need stdin for graceful 'q'
        if self.ep.mode.needs_graceful_drain:
            return subprocess.PIPE  # type: ignore[return-value]
        return subprocess.DEVNULL  # type: ignore[return-value]

    def spawn(self, cmd: Sequence[str]) -> subprocess.Popen:
        env = os.environ.copy()
        env.update(self.mgr._config.env)
        env.update(self.rt.env_overrides)
        with self._spawn_lock:
            self.rt.last_start_attempt = time.time()
            try:
                proc = subprocess.Popen(
                    list(cmd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=self._stdin_pipe(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ProcessError(
                    f"ffmpeg not found: {cmd[0]}", endpoint=self.ep.key,
                    cause=exc) from exc
            except Exception as exc:
                raise ProcessError(
                    f"spawn failed: {exc}", endpoint=self.ep.key,
                    cause=exc) from exc
        self.rt.proc = proc
        self.rt.generation += 1
        return proc

    def start_reader_threads(self) -> None:
        proc = self.rt.proc
        if not proc:
            return
        self.rt.stderr_thread = threading.Thread(
            target=self._reader_loop,
            args=(proc.stderr, self._on_stderr),
            name=f"transport-stderr-{self.ep.key}", daemon=True)
        self.rt.stderr_thread.start()
        self.rt.stdout_thread = threading.Thread(
            target=self._reader_loop,
            args=(proc.stdout, self._on_stdout),
            name=f"transport-stdout-{self.ep.key}", daemon=True)
        self.rt.stdout_thread.start()

    def _reader_loop(self, stream, sink: Callable[[str], None]) -> None:
        if stream is None:
            return
        try:
            for raw in stream:
                if self.rt.stop_event.is_set():
                    break
                line = raw.rstrip("\n\r")
                if line:
                    sink(line)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _on_stderr(self, line: str) -> None:
        now = time.time()
        self.rt.stats.last_stderr_ts = now
        self.rt.stats.last_stderr_line = line
        self.rt.stderr_buffer.append(line)
        low = line.lower()
        if any(k in low for k in ("error", "invalid", "failed", "fatal",
                                  "cannot", "no such")):
            self.rt.stats.last_error = line
            self.mgr._emit(EventType.ERROR, key=self.ep.key, message=line)
            self.mgr._metrics.transport_errors.inc(
                mode=self.ep.mode.value, key=self.ep.key, kind="stderr")
            self.mgr.logger.warning("[ffmpeg %s] %s", self.ep.display, line)
        else:
            self.mgr.logger.debug("[ffmpeg %s] %s", self.ep.display, line)
        self._ingest_progress(line)

    def _on_stdout(self, line: str) -> None:
        now = time.time()
        self.rt.stats.last_stdout_ts = now
        self.rt.stats.last_stdout_line = line
        self._ingest_progress(line)

    def _ingest_progress(self, line: str) -> None:
        if not line:
            return
        now = time.time()
        if "=" in line and line.count("=") == 1:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if k in RESERVED_KEYS_FOR_PROGRESS:
                self._apply_progress_kv(k, v, now)
                return
        size_m = re.search(r"size=\s*(\d+)\s*kB", line)
        br_m = re.search(r"bitrate=\s*([\d.]+)\s*kbits/s", line)
        if size_m:
            self._bump_bytes(safe_int(size_m.group(1)) * 1024, now)
        if br_m:
            self.rt.stats.bitrate_kbps = safe_float(br_m.group(1))

    def _apply_progress_kv(self, k: str, v: str, now: float) -> None:
        st = self.rt.stats
        st.last_progress_ts = now
        if k == "total_size":
            try:
                n = int(v)
                delta = max(0, n - st.bytes_sent)
                st.bytes_sent = n
                if delta:
                    st.samples.append((now, delta))
            except ValueError:
                pass
        elif k == "out_time_us":
            st.out_time_us = safe_int(v)
        elif k == "bitrate":
            m = re.match(r"([\d.]+)kbits/s", v)
            if m:
                st.bitrate_kbps = safe_float(m.group(1))
        elif k == "speed":
            m = re.match(r"([\d.]+)x", v)
            if m:
                st.speed = safe_float(m.group(1))
        elif k == "fps":
            st.fps = safe_float(v)
        elif k == "progress":
            if v == "end":
                st.last_stderr_line = "progress: end"

    def _bump_bytes(self, total: int, now: float) -> None:
        st = self.rt.stats
        delta = max(0, total - st.bytes_sent)
        st.bytes_sent = total
        st.last_progress_ts = now
        if delta:
            st.samples.append((now, delta))

    # ------------------------------------------------------------------
    def graceful_finish(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> bool:
        """Send 'q' to ffmpeg to close HLS/DASH playlists cleanly."""
        proc = self.rt.proc
        if proc is None or proc.poll() is not None:
            return False
        if proc.stdin is None:
            return False
        t0 = time.time()
        self.rt.drain_started_at = t0
        try:
            proc.stdin.write("q\n")
            proc.stdin.flush()
            with contextlib.suppress(Exception):
                proc.stdin.close()
        except Exception as exc:
            self.mgr.logger.debug("graceful_finish: write failed: %s", exc)
            return False
        try:
            proc.wait(timeout=timeout)
            dur = time.time() - t0
            self.rt.stats.drain_count += 1
            self.rt.stats.drain_last_dur_sec = dur
            self.mgr._metrics.transport_drains.inc(
                mode=self.ep.mode.value, key=self.ep.key)
            self.mgr.logger.info(
                "Graceful drain complete for %s in %.2fs", self.ep.key, dur)
            return True
        except subprocess.TimeoutExpired:
            self.mgr.logger.warning(
                "Graceful drain timed out for %s after %.1fs",
                self.ep.key, timeout)
            return False

    def terminate(self, grace: float = DEFAULT_SHUTDOWN_GRACE) -> None:
        proc = self.rt.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=2)
        finally:
            self.rt.proc = None
        self._join_threads()

    def _join_threads(self) -> None:
        for th in (self.rt.stderr_thread, self.rt.stdout_thread):
            if th and th.is_alive():
                th.join(timeout=2)
        self.rt.stderr_thread = None
        self.rt.stdout_thread = None


# ============================================================================
# 12. RESOURCE LIMITS (cgroups v2 / systemd-run / rlimit)
# ============================================================================

class ResourceLimiter:
    """
    Enforce per-child limits.

    Strategies (chosen automatically):
      1. cgroup v2 direct — needs write access to /sys/fs/cgroup
      2. systemd-run --scope — needs user session / systemd
      3. rlimit only — via preexec_fn (setrlimit, nice, affinity)
    """

    def __init__(self, root: Path = Path("/sys/fs/cgroup"),
                 enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self.logger = logging.getLogger(LOGGER_NAME + ".limits")
        self._cgroup2 = self._detect_cgroup2()

    def _detect_cgroup2(self) -> bool:
        try:
            return (self.root / "cgroup.controllers").exists()
        except Exception:
            return False

    # ------------------------------------------------------------------
    def choose_kind(self, limits: ResourceLimits) -> ResourceLimitKind:
        if not self.enabled or not limits.is_active():
            return ResourceLimitKind.NONE
        if limits.kind != ResourceLimitKind.NONE:
            return limits.kind
        if self._cgroup2 and os.access(self.root, os.W_OK):
            return ResourceLimitKind.CGROUP_V2
        if shutil.which("systemd-run"):
            return ResourceLimitKind.SYSTEMD_RUN
        return ResourceLimitKind.RLIMIT

    # ------------------------------------------------------------------
    def prefix_cmd(self, cmd: Sequence[str],
                   limits: ResourceLimits,
                   unit_name: str) -> Tuple[List[str], ResourceLimitKind]:
        """Wrap cmd with systemd-run if chosen.  Returns (cmd, chosen_kind)."""
        kind = self.choose_kind(limits)
        if kind == ResourceLimitKind.SYSTEMD_RUN:
            args = ["systemd-run", "--scope", "--quiet",
                    f"--unit={unit_name}"]
            if limits.memory_mb:
                args += [f"--property=MemoryMax={limits.memory_mb}M"]
            if limits.cpu_quota_pct:
                # CPUQuota=150% means 1.5 cores
                args += [f"--property=CPUQuota={limits.cpu_quota_pct}%"]
            if limits.tasks_max:
                args += [f"--property=TasksMax={limits.tasks_max}"]
            if limits.io_weight:
                args += [f"--property=IOWeight={limits.io_weight}"]
            if limits.cpu_affinity:
                args += [f"--property=AllowedCPUs="
                         + ",".join(str(c) for c in limits.cpu_affinity)]
            args += ["--"]
            return args + list(cmd), kind
        return list(cmd), kind

    # ------------------------------------------------------------------
    def preexec(self, limits: ResourceLimits) -> Opt[Callable[[], None]]:
        """Build a preexec_fn that applies rlimits, nice, affinity."""
        if limits.kind not in (ResourceLimitKind.RLIMIT,
                               ResourceLimitKind.NONE):
            # cgroup/systemd-run handle their own
            if not (limits.nice or limits.nofile or limits.cpu_affinity):
                return None
        if not (limits.nice or limits.nofile or limits.cpu_affinity):
            return None

        def _setup() -> None:
            with contextlib.suppress(Exception):
                if limits.nice:
                    os.nice(limits.nice)
            with contextlib.suppress(Exception):
                if limits.nofile:
                    import resource
                    resource.setrlimit(resource.RLIMIT_NOFILE,
                                       (limits.nofile, limits.nofile))
            with contextlib.suppress(Exception):
                if limits.cpu_affinity:
                    os.sched_setaffinity(0, set(limits.cpu_affinity))
        return _setup

    # ------------------------------------------------------------------
    def create_cgroup(self, name: str, limits: ResourceLimits,
                      pid: int) -> Opt[Path]:
        """Create cgroup v2 subtree and attach pid."""
        if not self._cgroup2 or not self.enabled:
            return None
        if limits.kind not in (ResourceLimitKind.CGROUP_V2,
                               ResourceLimitKind.NONE):
            return None
        path = self.root / "astcie-transport" / name
        try:
            path.mkdir(parents=True, exist_ok=True)
            if limits.memory_mb:
                (path / "memory.max").write_text(
                    str(limits.memory_mb * 1024 * 1024))
            if limits.cpu_quota_pct:
                # CPUQuota=100% -> 1 core => cpu.max "100000 100000"
                period = 100_000
                quota = int(period * limits.cpu_quota_pct / 100)
                (path / "cpu.max").write_text(f"{quota} {period}")
            if limits.tasks_max:
                (path / "pids.max").write_text(str(limits.tasks_max))
            (path / "cgroup.procs").write_text(str(pid))
            return path
        except Exception as exc:
            self.logger.warning("cgroup v2 create failed: %s", exc)
            return None

    def remove_cgroup(self, path: Opt[Path]) -> None:
        if path is None:
            return
        try:
            with contextlib.suppress(Exception):
                (path / "cgroup.kill").write_text("1")
            with contextlib.suppress(Exception):
                path.rmdir()
        except Exception:
            pass


# ============================================================================
# 13. FFMPEG COMMAND BUILDERS
# ============================================================================

class CommandBuilder:
    def __init__(self, endpoint: TransportEndpoint, *,
                 ffmpeg_bin: str = DEFAULT_FFMPEG_BIN,
                 source_ts: Opt[Path] = None,
                 source_url: Opt[str] = None,
                 global_args: Sequence[str] = (),
                 stats_period: float = 1.0,
                 progress_pipe: Opt[str] = None,
                 global_rate_limit_kbps: int = 0):
        self.ep = endpoint
        self.ffmpeg = ffmpeg_bin
        self.source_ts = source_ts
        self.source_url = source_url
        self.global_args = list(global_args)
        self.stats_period = stats_period
        self.progress_pipe = progress_pipe
        self.global_rate_limit_kbps = global_rate_limit_kbps

    def build(self) -> List[str]:
        ep = self.ep
        cmd: List[str] = [self.ffmpeg]
        cmd += ["-hide_banner", "-nostdin", "-loglevel", "warning"]
        cmd += ["-stats_period", str(self.stats_period)]
        if self.progress_pipe:
            cmd += ["-progress", self.progress_pipe]
        cmd += self.global_args
        cmd += self._input_args()

        if self._is_multivariant_hls():
            cmd += self._multivariant_hls_args()
        else:
            cmd += ["-map", "0", "-c", "copy"]
            cmd += self._output_args()

        if ep.extra_ffmpeg_args:
            cmd += list(ep.extra_ffmpeg_args)

        dest = self._destination()
        if dest:
            cmd.append(dest)
        return cmd

    def _input_args(self) -> List[str]:
        ep = self.ep
        args: List[str] = []
        if ep.read_rate:
            args += ["-re"]
        if ep.rate_limit_kbps > 0:
            args += ["-readrate", f"{ep.rate_limit_kbps / 1000.0:.3f}"]
        if ep.extra_input_args:
            args += list(ep.extra_input_args)
        src = self.source_url or (str(self.source_ts) if self.source_ts else "-")
        args += ["-i", src]
        return args

    def _output_args(self) -> List[str]:
        return self._apply_pacing(self._output_args_raw())

    def _output_args_raw(self) -> List[str]:
        ep = self.ep
        mode = ep.mode
        if mode == OutputMode.FILE:
            return ["-f", "mpegts", "-y"]
        if mode == OutputMode.UDP:
            return ["-f", "mpegts", self._udp_url()]
        if mode == OutputMode.RTP:
            return ["-f", "rtp_mpegts", self._rtp_url()]
        if mode == OutputMode.SRTP:
            return self._srtp_args()
        if mode == OutputMode.SRT:
            return ["-f", "mpegts", self._srt_url()]
        if mode == OutputMode.RIST:
            return ["-f", "mpegts", self._rist_url()]
        if mode in (OutputMode.RTMP, OutputMode.RTMPS):
            return ["-f", "flv", self._rtmp_url()]
        if mode == OutputMode.HLS:
            return self._hls_args()
        if mode == OutputMode.DASH:
            return self._dash_args()
        if mode in (OutputMode.HTTP, OutputMode.HTTPS):
            return ["-f", "mpegts", "-method", ep.http_method,
                    self._http_url()]
        if mode == OutputMode.TCP:
            return ["-f", "mpegts", self._tcp_url()]
        if mode == OutputMode.UNIX:
            return ["-f", "mpegts", self._unix_url()]
        if mode == OutputMode.ICECAST:
            return self._icecast_args()
        if mode == OutputMode.WHIP:
            return self._whip_args()
        if mode == OutputMode.NULL:
            return ["-f", "null", "-"]
        raise UnsupportedProtocolError(
            f"No output args for mode {mode}", endpoint=ep.key)

    def _apply_pacing(self, args: List[str]) -> List[str]:
        ep = self.ep
        kbps = ep.rate_limit_kbps or self.global_rate_limit_kbps
        if kbps <= 0:
            return args
        padded = int(kbps * ep.extra_muxrate_pct)
        if ep.mode in (OutputMode.UDP, OutputMode.RTP, OutputMode.SRTP,
                       OutputMode.SRT, OutputMode.RIST, OutputMode.TCP,
                       OutputMode.UNIX, OutputMode.FILE,
                       OutputMode.RTMP, OutputMode.RTMPS):
            args += ["-muxrate", f"{padded}k"]
        return args

    def _destination(self) -> str:
        ep = self.ep
        if ep.mode == OutputMode.HLS:
            return str(self._hls_playlist_path())
        if ep.mode == OutputMode.DASH:
            return str(self._dash_manifest_path())
        if ep.mode == OutputMode.FILE:
            return str(ep.output_path)
        return ""

    def _is_multivariant_hls(self) -> bool:
        ep = self.ep
        return (ep.mode == OutputMode.HLS
                and ep.hls_master
                and bool(ep.hls_variants))

    def _multivariant_hls_args(self) -> List[str]:
        ep = self.ep
        mv = MultiVariantHLS(ep.hls_variants)
        base = mv.build_args()
        out_dir = Path(self._hls_playlist_path()).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        args: List[str] = []
        skip = False
        for i, a in enumerate(base):
            if skip:
                skip = False
                continue
            if a == "-hls_time":
                args += ["-hls_time", str(ep.segment_sec)]; skip = True
            elif a == "-hls_list_size":
                args += ["-hls_list_size", str(ep.playlist_size)]; skip = True
            elif a == "%v/seg_%05d.ts":
                args.append(str(out_dir / "%v" / "seg_%05d.ts"))
            else:
                args.append(a)
        return args

    # ------------------------------------------------------------------
    def _udp_url(self) -> str:
        ep = self.ep
        host, port = split_host_port(ep.target, default_port=1234)
        validate_port(port)
        q: Dict[str, Any] = {
            "pkt_size": ep.pkt_size,
            "buffer_size": ep.buffer_size,
        }
        if ep.localaddr:
            q["localaddr"] = ep.localaddr
        if ep.multicast or is_multicast(host):
            q["ttl"] = ep.ttl
        if ep.interface:
            q["interface"] = ep.interface
        if ep.tos:
            q["tos"] = ep.tos
        return add_query(f"udp://{normalize_host(host)}:{port}", q)

    def _rtp_url(self) -> str:
        ep = self.ep
        host, port = split_host_port(ep.target, default_port=5004)
        validate_port(port)
        q: Dict[str, Any] = {"payload_type": ep.payload_type}
        if ep.localaddr:
            q["localaddr"] = ep.localaddr
        if ep.ssrc is not None:
            q["ssrc"] = ep.ssrc
        return add_query(f"rtp://{normalize_host(host)}:{port}", q)

    def _srtp_args(self) -> List[str]:
        ep = self.ep
        host, port = split_host_port(ep.target, default_port=5004)
        validate_port(port)

        suite = ep.encryption.srtp_suite
        if ep.encryption.srtp_downgraded:
            logging.getLogger(LOGGER_NAME).warning(
                "SRTP %s is not a standard SRTP suite in ffmpeg; "
                "downgrading to AES_CM_128_HMAC_SHA1_80", ep.encryption.value)

        try:
            key = hex_to_bytes(ep.srtp_key) if ep.srtp_key else b""
            salt = hex_to_bytes(ep.srtp_salt) if ep.srtp_salt else b""
            want_key = 32 if suite == "AEAD_AES_256_GCM" else 16
            want_salt = 12 if "GCM" in suite else 14
            if not key:
                key = secrets.token_bytes(want_key)
            if not salt:
                salt = secrets.token_bytes(want_salt)
            if len(key) < want_key:
                key = key.ljust(want_key, b"\x00")
            if len(salt) < want_salt:
                salt = salt.ljust(want_salt, b"\x00")
            blob = base64.b64encode(key[:want_key] + salt[:want_salt]).decode("ascii")
        except Exception as exc:
            raise ValidationError(
                f"invalid srtp_key/salt: {exc}", endpoint=ep.key) from exc

        return [
            "-f", "rtp",
            "-payload_type", str(ep.payload_type),
            "-ssrc", str(ep.ssrc or 0),
            "-srtp_out_suite", suite,
            "-srtp_out_params", blob,
            f"rtp://{normalize_host(host)}:{port}",
        ]

    def _srt_url(self) -> str:
        ep = self.ep
        target = ep.target
        if not target.startswith("srt://"):
            target = f"srt://{target}"
        q: Dict[str, Any] = {}
        if "mode=" not in target:
            q["mode"] = ep.srt_mode
        if "latency=" not in target:
            q["latency"] = ep.srt_latency_ms
        if ep.srt_passphrase and "passphrase=" not in target:
            q["passphrase"] = ep.srt_passphrase
            if ep.srt_pbkeylen:
                q["pbkeylen"] = ep.srt_pbkeylen
        if ep.srt_streamid and "streamid=" not in target:
            q["streamid"] = ep.srt_streamid
        if ep.srt_maxbw > 0 and "maxbw=" not in target:
            q["maxbw"] = ep.srt_maxbw
        return add_query(target, q)

    def _rist_url(self) -> str:
        ep = self.ep
        target = ep.target
        if not target.startswith("rist://"):
            target = f"rist://{target}"
        q: Dict[str, Any] = {}
        if ep.rist_profile:
            q["profile"] = ep.rist_profile
        if ep.rist_secret:
            q["secret"] = ep.rist_secret
        if ep.rist_buffer_ms:
            q["buffer"] = ep.rist_buffer_ms
        if ep.rist_recovery_maxbitrate:
            q["recovery_maxbitrate"] = ep.rist_recovery_maxbitrate
        return add_query(target, q)

    def _rtmp_url(self) -> str:
        ep = self.ep
        target = ep.target
        if target.startswith("rtmp://") or target.startswith("rtmps://"):
            return target
        host, port = split_host_port(target, default_port=1935)
        path = f"/{ep.rtmp_app.strip('/')}/{ep.rtmp_stream_key}"
        url = build_url("rtmp", host, port, path)
        if ep.rtmp_user:
            auth = f"{quote(ep.rtmp_user)}:{quote(ep.rtmp_password)}@"
            url = url.replace("rtmp://", f"rtmp://{auth}", 1)
        return url

    def _http_url(self) -> str:
        ep = self.ep
        target = ep.target
        if not (target.startswith("http://") or target.startswith("https://")):
            target = "http://" + target
        return target

    def _tcp_url(self) -> str:
        ep = self.ep
        host, port = split_host_port(ep.target, default_port=0)
        validate_port(port)
        q = {"listen": 0}
        if ep.localaddr:
            q["localaddr"] = ep.localaddr
        return add_query(f"tcp://{normalize_host(host)}:{port}", q)

    def _unix_url(self) -> str:
        ep = self.ep
        target = ep.target or "unix"
        if target.startswith("unix://"):
            return target
        return f"unix://{target}"

    def _icecast_args(self) -> List[str]:
        ep = self.ep
        target = ep.target
        if not (target.startswith("icecast://") or target.startswith("http")):
            host, port = split_host_port(target, default_port=8000)
            auth = ""
            if ep.http_user:
                auth = f"{quote(ep.http_user)}:{quote(ep.http_password)}@"
            url = (f"icecast://{auth}{normalize_host(host)}:{port}"
                   f"{ep.icecast_mount}")
        else:
            url = target
        headers = [
            "-content_type", "audio/mpeg",
            "-user_agent", "ASTCIE/3.0",
        ]
        if ep.icecast_name:
            headers += ["-ice_name", ep.icecast_name]
        if ep.icecast_genre:
            headers += ["-ice_genre", ep.icecast_genre]
        if ep.icecast_description:
            headers += ["-ice_description", ep.icecast_description]
        return ["-f", "mp3",
                "-ice_public", "1" if ep.icecast_public else "0",
                *headers, url]

    def _whip_args(self) -> List[str]:
        ep = self.ep
        url = WHIPHelper.normalize_url(ep.target)
        args = ["-f", "whip"]
        if ep.whip_bearer:
            args += ["-authorization", ep.whip_bearer]
        # ffmpeg's whip muxer honors ice servers through env var WHIP_ICE_SERVERS
        return args + [url]

    def _hls_playlist_path(self) -> Path:
        ep = self.ep
        base = Path(ep.output_path) if ep.output_path else Path("out")
        if base.suffix.lower() == ".m3u8":
            return base
        return base / "index.m3u8"

    def _dash_manifest_path(self) -> Path:
        ep = self.ep
        if ep.dash_manifest:
            return Path(ep.dash_manifest)
        base = Path(ep.output_path) if ep.output_path else Path("out")
        if base.suffix.lower() == ".mpd":
            return base
        return base / "manifest.mpd"

    def _hls_args(self) -> List[str]:
        ep = self.ep
        playlist = self._hls_playlist_path()
        seg_dir = playlist.parent
        seg_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(seg_dir / "seg_%05d.ts")
        args = [
            "-f", "hls",
            "-hls_time", str(ep.segment_sec),
            "-hls_list_size", str(ep.playlist_size),
            "-hls_segment_filename", pattern,
            "-hls_flags", "delete_segments+append_list+omit_endlist",
        ]
        if ep.hls_variant == "fmp4":
            args += ["-hls_segment_type", "fmp4"]
        elif ep.hls_variant == "mpegts":
            args += ["-hls_segment_type", "mpegts"]
        if ep.encryption != EncryptionMode.NONE and ep.encryption_key:
            args += ["-hls_key_info_file", self._hls_keyinfo_path()]
        return args

    def _hls_keyinfo_path(self) -> str:
        ep = self.ep
        base = Path(self._hls_playlist_path()).parent
        keyfile = base / "enc.key"
        keyfile.write_bytes(hex_to_bytes(ep.encryption_key))
        info = base / "enc.keyinfo"
        iv = ep.encryption_iv or random_hex(16)
        url = ep.encryption_url or "enc.key"
        info.write_text(f"{url}\n{keyfile}\n{iv}\n", encoding="utf-8")
        return str(info)

    def _dash_args(self) -> List[str]:
        ep = self.ep
        manifest = self._dash_manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        return [
            "-f", "dash",
            "-seg_duration", str(ep.segment_sec),
            "-window_size", str(ep.playlist_size),
            "-use_timeline", "1",
            "-use_template", "1",
            "-init_seg_name", "init_$RepresentationID$.m4s",
            "-media_seg_name", "chunk_$RepresentationID$_$Number%05d$.m4s",
        ]


# ============================================================================
# 14. MANIFEST WRITERS
# ============================================================================

class ManifestWriter:
    @staticmethod
    def write_sdp(ep: TransportEndpoint,
                  rtp_host: Opt[str] = None,
                  rtp_port: Opt[int] = None) -> Opt[Path]:
        if not ep.sdp_path:
            return None
        host, port = split_host_port(ep.target, default_port=5004)
        if rtp_host:
            host = rtp_host
        if rtp_port:
            port = rtp_port

        c_host = host
        if host in ("0.0.0.0", "", "127.0.0.1"):
            try:
                c_host = socket.gethostbyname(socket.gethostname())
            except Exception:
                c_host = "127.0.0.1"

        sdp = "\r\n".join([
            "v=0",
            f"o=- {int(time.time())} 0 IN IP4 {c_host}",
            f"s=ASTCIE/VECTRA {ep.display}",
            f"c=IN IP4 {host}",
            "t=0 0",
            "a=tool:astcie-transport/3.0",
            "a=recvonly",
            f"m=video {port} RTP/AVP {ep.payload_type}",
            f"a=rtpmap:{ep.payload_type} MP2T/{ep.clock_rate}",
            "",
        ])
        try:
            Path(ep.sdp_path).parent.mkdir(parents=True, exist_ok=True)
            Path(ep.sdp_path).write_text(sdp, encoding="utf-8")
            return Path(ep.sdp_path)
        except Exception:
            return None

    @staticmethod
    def write_m3u8_stub(ep: TransportEndpoint) -> Opt[Path]:
        if ep.mode != OutputMode.HLS or not ep.output_path:
            return None
        playlist = Path(ep.output_path)
        if playlist.suffix.lower() != ".m3u8":
            playlist = playlist / "index.m3u8"
        try:
            playlist.parent.mkdir(parents=True, exist_ok=True)
            if not playlist.exists():
                playlist.write_text(
                    "#EXTM3U\n"
                    "#EXT-X-VERSION:3\n"
                    f"#EXT-X-TARGETDURATION:{int(ep.segment_sec)}\n"
                    f"#EXT-X-MEDIA-SEQUENCE:0\n",
                    encoding="utf-8")
            return playlist
        except Exception:
            return None

    @staticmethod
    def write_hls_master(ep: TransportEndpoint) -> Opt[Path]:
        if ep.mode != OutputMode.HLS or not ep.hls_master:
            return None
        if not ep.hls_variants:
            return None
        base = Path(ep.output_path) if ep.output_path else Path("out")
        master = base / "master.m3u8" if base.suffix != ".m3u8" else base
        try:
            master.parent.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
            for v in ep.hls_variants:
                bw = int(v.get("bandwidth", 0))
                res = v.get("resolution", "")
                codecs = v.get("codecs", "avc1.4d401f,mp4a.40.2")
                path = v.get("path", "")
                lines.append(
                    f'#EXT-X-STREAM-INF:BANDWIDTH={bw}'
                    + (f',RESOLUTION={res}' if res else '')
                    + f',CODECS="{codecs}"')
                lines.append(path)
            master.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return master
        except Exception:
            return None

    @staticmethod
    def write_dash_mpd_stub(ep: TransportEndpoint) -> Opt[Path]:
        if ep.mode != OutputMode.DASH:
            return None
        manifest = (ep.dash_manifest
                    or (Path(ep.output_path) if ep.output_path
                        else Path("out")) / "manifest.mpd")
        try:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            if not manifest.exists():
                manifest.write_text(
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
                    'type="dynamic" minimumUpdatePeriod="PT2S" '
                    'minBufferTime="PT4S" timeShiftBufferDepth="PT30S" '
                    'profiles="urn:mpeg:dash:profile:isoff-live:2011"/>\n',
                    encoding="utf-8")
            return manifest
        except Exception:
            return None


# ============================================================================
# 15. INPUT FAILOVER
# ============================================================================

@dataclass
class _FailoverInput:
    url: str
    priority: int = 100
    failures: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "priority": self.priority,
            "failures": self.failures,
            "last_failure_ts": self.last_failure_ts,
            "last_success_ts": self.last_success_ts,
        }


class InputFailover:
    def __init__(self, inputs: Sequence[str],
                 probe_sec: float = 2.0):
        self._inputs: List[_FailoverInput] = [
            _FailoverInput(url=u, priority=i)
            for i, u in enumerate(inputs)
        ]
        self.probe_sec = probe_sec
        self._index = 0
        self._lock = threading.RLock()

    @property
    def size(self) -> int:
        return len(self._inputs)

    def current_index(self) -> int:
        with self._lock:
            return self._index

    def current(self) -> Opt[str]:
        with self._lock:
            if not self._inputs:
                return None
            return self._inputs[self._index].url

    def mark_failure(self) -> Opt[str]:
        with self._lock:
            if not self._inputs:
                return None
            self._inputs[self._index].failures += 1
            self._inputs[self._index].last_failure_ts = time.time()
            self._index = (self._index + 1) % len(self._inputs)
            return self._inputs[self._index].url

    def mark_success(self) -> None:
        with self._lock:
            if not self._inputs:
                return
            self._inputs[self._index].last_success_ts = time.time()

    def all_inputs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [i.snapshot() for i in self._inputs]


# ============================================================================
# 16. MULTI-VARIANT HLS HELPER
# ============================================================================

class MultiVariantHLS:
    def __init__(self, variants: Sequence[Mapping[str, Any]]):
        self.variants = [dict(v) for v in variants]

    def build_args(self) -> List[str]:
        if not self.variants:
            return []
        args: List[str] = []
        names: List[str] = []
        for i, v in enumerate(self.variants):
            name = v.get("name", f"v{i}")
            names.append(f"v:{name},a:{name},name:{name}")
            args += ["-map", "0:v", "-map", "0:a?"]     # audio optional
            args += ["-c:v", v.get("codec", "libx264")]
            args += ["-preset", v.get("preset", "veryfast")]
            args += ["-tune", v.get("tune", "zerolatency")]
            args += ["-b:v", str(v.get("video_bitrate", "2000k"))]
            args += ["-maxrate", str(v.get("maxrate",
                                           v.get("video_bitrate", "2000k")))]
            args += ["-bufsize", str(v.get("bufsize", "4000k"))]
            args += ["-s", str(v.get("resolution", "1280x720"))]
            gop = int(v.get("fps", 25) * 2)
            args += ["-g", str(gop), "-keyint_min", str(gop),
                     "-sc_threshold", "0"]
            args += ["-c:a", v.get("acodec", "aac")]
            args += ["-b:a", str(v.get("audio_bitrate", "128k"))]
            args += ["-ac", "2", "-ar", "48000"]
        var_map = " ".join(names)
        args += [
            "-f", "hls",
            "-hls_time", "6",
            "-hls_list_size", "6",
            "-hls_flags", "independent_segments+append_list+omit_endlist",
            "-master_pl_name", "master.m3u8",
            "-var_stream_map", var_map,
            "-hls_segment_filename", "%v/seg_%05d.ts",
        ]
        return args

    def manifest_entries(self,
                         segment_sec: float = 6.0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for i, v in enumerate(self.variants):
            name = v.get("name", f"v{i}")
            out.append({
                "path": f"{name}/index.m3u8",
                "bandwidth": int(v.get("bandwidth", 0)),
                "resolution": v.get("resolution", ""),
                "codecs": v.get("codecs", "avc1.4d401f,mp4a.40.2"),
            })
        return out


# ============================================================================
# 17. SRTP HELPERS
# ============================================================================

class SRTPHelper:
    @staticmethod
    def generate_key(mode: EncryptionMode = EncryptionMode.AES128) -> Dict[str, str]:
        klen = mode.key_len_bytes or 16
        slen = mode.salt_len_bytes or 14
        key_hex = secrets.token_hex(klen)
        salt_hex = secrets.token_hex(slen)
        blob = base64.b64encode(
            bytes.fromhex(key_hex) + bytes.fromhex(salt_hex)
        ).decode("ascii")
        return {
            "key_hex": key_hex,
            "salt_hex": salt_hex,
            "base64": blob,
            "mode": mode.value,
            "suite": mode.srtp_suite,
        }

    @staticmethod
    def from_hex(key_hex: str, salt_hex: str = "") -> str:
        k = bytes.fromhex(key_hex)
        s = bytes.fromhex(salt_hex) if salt_hex else b""
        return base64.b64encode(k + s).decode("ascii")

    @staticmethod
    def suite_name(mode: EncryptionMode) -> str:
        return mode.srtp_suite


# ============================================================================
# 18. WHIP / WEBRTC HELPER
# ============================================================================

class WHIPHelper:
    """
    WHIP (WebRTC-HTTP Ingestion Protocol) helper.

    ffmpeg >= 6.0 includes a native WHIP muxer (`-f whip`).
    For older ffmpeg or advanced ICE, use gst-launch with whipsink.
    """

    @staticmethod
    def normalize_url(target: str) -> str:
        t = (target or "").strip()
        if not t:
            return t
        if not (t.startswith("http://") or t.startswith("https://")):
            t = "https://" + t
        return t

    @staticmethod
    def ffmpeg_supports_whip(binary: str = DEFAULT_FFMPEG_BIN) -> bool:
        try:
            out = subprocess.run([binary, "-hide_banner", "-muxers"],
                                 capture_output=True, text=True, timeout=5)
            return "whip" in (out.stdout or "").lower()
        except Exception:
            return False

    @staticmethod
    def build_env(ep: TransportEndpoint) -> Dict[str, str]:
        """
        Build env vars consumed by ffmpeg's whip muxer.
        ffmpeg's whip muxer reads WHIP_ICE_SERVERS as comma-separated list.
        """
        servers = [ep.whip_stun] if ep.whip_stun else []
        servers += list(ep.whip_ice_server_extra)
        env: Dict[str, str] = {}
        if servers:
            env["WHIP_ICE_SERVERS"] = ",".join(s for s in servers if s)
        return env

    @staticmethod
    def build_gst_cmd(ep: TransportEndpoint, source: str = "pipe:0") -> List[str]:
        """
        Fallback: use GStreamer whipsink if ffmpeg lacks whip support.
        Requires `gst-launch-1.0` and `gst-plugins-rs` (whipsink).
        """
        target = WHIPHelper.normalize_url(ep.target)
        stun = ep.whip_stun or "stun://stun.l.google.com:19302"
        cmd = [
            "gst-launch-1.0", "-q",
            "fdsrc", "fd=0",
            "!", "tsdemux", "name=demux",
            "demux.", "!", "queue", "!", "rtph264pay",
            "!", "whipsink", f"signaller::whip-endpoint={target}",
            f"signaller::stun-server={stun}",
        ]
        if ep.whip_bearer:
            cmd.append(f"signaller::auth-token={ep.whip_bearer}")
        return cmd


# ============================================================================
# 19. WATCHDOG
# ============================================================================

class Watchdog:
    def __init__(self, manager: "TransportManager",
                 interval: float = DEFAULT_WATCHDOG_INTERVAL,
                 stall_sec: float = DEFAULT_WATCHDOG_STALL_SEC):
        self.manager = manager
        self.interval = interval
        self.stall_sec = stall_sec
        self._stop = threading.Event()
        self._thread: Opt[threading.Thread] = None
        self._last_supervisor_tick = 0.0
        self._last_watchdog_tick = 0.0
        self._respawns = 0
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_supervisor_tick = time.time()
        self._last_watchdog_tick = time.time()
        self._thread = threading.Thread(
            target=self._loop, name="transport-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def note_supervisor_tick(self) -> None:
        with self._lock:
            self._last_supervisor_tick = time.time()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._last_watchdog_tick = time.time()
            try:
                self._check()
            except Exception:
                self.manager.logger.exception("watchdog error")

    def _check(self) -> None:
        mgr = self.manager
        sup = mgr._supervisor_thread
        stalled = (time.time() - self._last_supervisor_tick) > self.stall_sec
        dead = (sup is None) or (not sup.is_alive())
        if not (dead or stalled):
            return
        mgr.logger.warning("Watchdog: supervisor %s — respawning",
                           "dead" if dead else "stalled")
        mgr._emit(EventType.WARN, key="",
                  message="watchdog respawning supervisor",
                  dead=dead, stalled=stalled)
        with contextlib.suppress(Exception):
            mgr._restart_supervisor()
        self._respawns += 1
        mgr._metrics.watchdog_respawns.inc()
        with self._lock:
            self._last_supervisor_tick = time.time()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "respawns": self._respawns,
            "last_supervisor_tick": self._last_supervisor_tick,
            "last_watchdog_tick": self._last_watchdog_tick,
        }


# ============================================================================
# 20. CONFIG SCHEMA VALIDATOR
# ============================================================================

class ConfigSchema:
    """
    Lightweight schema validator.  Uses pydantic if available; otherwise
    falls back to a built-in checker with the same field set.

    Usage
    -----
    ConfigSchema.validate_endpoint_dict({...}) -> TransportEndpoint
    ConfigSchema.validate_manager_dict({...})  -> ManagerConfig
    ConfigSchema.load_yaml(path)               -> dict
    """

    ENDPOINT_INT_FIELDS = (
        "priority", "pkt_size", "buffer_size", "ttl", "tos",
        "payload_type", "clock_rate", "srt_latency_ms", "srt_pbkeylen",
        "srt_maxbw", "rist_profile", "rist_buffer_ms",
        "rist_recovery_maxbitrate", "max_restarts", "breaker_threshold",
        "rate_limit_kbps", "playlist_size", "segment_sec",
    )
    ENDPOINT_FLOAT_FIELDS = (
        "base_backoff_sec", "max_backoff_sec", "restart_jitter",
        "breaker_cooldown_sec", "health_interval_sec", "health_timeout_sec",
        "failover_probe_sec", "extra_muxrate_pct",
        "hls_drain_timeout_sec", "dash_drain_timeout_sec", "whip_timeout_sec",
    )
    ENDPOINT_BOOL_FIELDS = (
        "enabled", "multicast", "read_rate", "icecast_public",
        "health_enabled", "hls_master",
    )
    ENDPOINT_STR_FIELDS = (
        "name", "target", "localaddr", "interface", "srt_mode",
        "srt_passphrase", "srt_streamid", "rist_secret", "rtmp_app",
        "rtmp_stream_key", "rtmp_user", "rtmp_password", "http_method",
        "http_user", "http_password", "http_token", "icecast_mount",
        "icecast_name", "icecast_genre", "icecast_description",
        "hls_variant", "encryption_key", "encryption_iv", "encryption_url",
        "srtp_key", "srtp_salt", "health_probe",
        "whip_bearer", "whip_stun",
    )

    # ------------------------------------------------------------------
    @classmethod
    def validate_endpoint_dict(cls, data: Mapping[str, Any],
                               *, strict: bool = True) -> TransportEndpoint:
        if not isinstance(data, Mapping):
            raise SchemaError("endpoint must be a mapping")
        # Coerce types
        clean: Dict[str, Any] = dict(data)
        for k in cls.ENDPOINT_INT_FIELDS:
            if k in clean and clean[k] not in (None, ""):
                clean[k] = safe_int(clean[k])
        for k in cls.ENDPOINT_FLOAT_FIELDS:
            if k in clean and clean[k] not in (None, ""):
                clean[k] = safe_float(clean[k])
        for k in cls.ENDPOINT_BOOL_FIELDS:
            if k in clean and isinstance(clean[k], str):
                clean[k] = clean[k].lower() in ("1", "true", "yes", "on")
        if "mode" not in clean:
            raise SchemaError("endpoint requires 'mode'")
        if "resource_limits" in clean and isinstance(clean["resource_limits"], dict):
            clean["resource_limits"] = ResourceLimits(**clean["resource_limits"])
        ep = TransportEndpoint.from_dict(clean)
        if strict:
            ep.validate()
        return ep

    @classmethod
    def validate_manager_dict(cls, data: Mapping[str, Any]) -> ManagerConfig:
        if not isinstance(data, Mapping):
            raise SchemaError("manager config must be a mapping")
        clean: Dict[str, Any] = {}
        for f in _dc_fields(ManagerConfig):
            if f.name in data:
                v = data[f.name]
                if f.name in ("work_dir", "output_dir", "cgroup_root"):
                    v = Path(v)
                clean[f.name] = v
        return ManagerConfig(**clean)

    @classmethod
    def load_yaml(cls, path: Union[str, Path]) -> Dict[str, Any]:
        if not _HAVE_YAML:
            raise SchemaError("PyYAML not installed")
        p = Path(path)
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    @classmethod
    def load_json(cls, path: Union[str, Path]) -> Dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_endpoint_dict(data: Mapping[str, Any]) -> TransportEndpoint:
    return ConfigSchema.validate_endpoint_dict(data)


def validate_manager_dict(data: Mapping[str, Any]) -> ManagerConfig:
    return ConfigSchema.validate_manager_dict(data)


# ============================================================================
# 21. CONTROL AUTH
# ============================================================================

class ControlAuth:
    """
    Authentication policy for the HTTP control plane.

    Supported modes (any combination):
      * ``token``        — Bearer token in ``Authorization`` header,
                           or ``X-Auth-Token`` header, or ``?token=`` query.
      * ``basic_users``  — HTTP Basic, mapping username -> password.
      * ``ip_allow``     — CIDR list; empty = allow all.
      * ``allow_anonymous`` — dev bypass (logged loudly).
    """

    def __init__(self,
                 token: str = "",
                 users: Opt[Mapping[str, str]] = None,
                 ip_allow: Sequence[str] = (),
                 allow_anonymous: bool = False,
                 realm: str = "astcie-transport"):
        self.token = token or ""
        self.users = dict(users or {})
        self.ip_allow = [ipaddress.ip_network(c, strict=False)
                         for c in ip_allow]
        self.allow_anonymous = allow_anonymous
        self.realm = realm

    @property
    def enabled(self) -> bool:
        if self.allow_anonymous:
            return False
        return bool(self.token or self.users or self.ip_allow)

    def check(self, handler: "http.server.BaseHTTPRequestHandler",
              query: Mapping[str, List[str]]) -> Tuple[bool, str]:
        peer = handler.client_address[0]

        if self.ip_allow:
            try:
                ip = ipaddress.ip_address(peer)
            except Exception:
                return False, f"bad peer ip {peer}"
            if not any(ip in net for net in self.ip_allow):
                return False, f"ip {peer} not in allow-list"

        if self.token:
            presented = self._extract_token(handler, query)
            if presented and constant_time_eq(presented, self.token):
                return True, "token ok"
            token_ok = False
        else:
            token_ok = True

        if self.users:
            auth = handler.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8")
                    user, _, pw = decoded.partition(":")
                except Exception:
                    return False, "invalid basic header"
                expected = self.users.get(user)
                if expected is not None and constant_time_eq(pw, expected):
                    return True, f"basic ok ({user})"
            return False, "basic auth required"

        if not self.token:
            return True, "ip ok"

        return (False, "bearer token required") if not token_ok else (True, "ok")

    @staticmethod
    def _extract_token(handler: "http.server.BaseHTTPRequestHandler",
                       query: Mapping[str, List[str]]) -> str:
        auth = handler.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        xt = handler.headers.get("X-Auth-Token", "")
        if xt:
            return xt.strip()
        vals = query.get("token") or []
        return vals[0] if vals else ""


# ============================================================================
# 22. HTTP CONTROL SERVER
# ============================================================================

class _ControlHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ASTCIE-Transport/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        with contextlib.suppress(Exception):
            self.server.manager.logger.debug("control: " + fmt, *args)

    def _send(self, code: int, body: bytes,
              content_type: str = "application/json",
              extra: Opt[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"))

    def _text(self, code: int, text: str,
              content_type: str = "text/plain; charset=utf-8") -> None:
        self._send(code, text.encode("utf-8"), content_type)

    def _read_body(self) -> bytes:
        n = safe_int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n) if n > 0 else b""

    def _read_json(self) -> Any:
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _path_parts(self) -> Tuple[str, Dict[str, List[str]]]:
        u = urllib.parse.urlparse(self.path)
        return (u.path.rstrip("/") or "/",
                urllib.parse.parse_qs(u.query or ""))

    def _check_auth(self) -> bool:
        auth: Opt[ControlAuth] = getattr(self.server, "auth", None)
        if auth is None or not auth.enabled:
            return True
        _path, qs = self._path_parts()
        ok, reason = auth.check(self, qs)
        if not ok:
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer realm="{auth.realm}", Basic realm="{auth.realm}"')
            body = json.dumps({"error": "unauthorized",
                               "reason": reason}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(Exception):
                self.wfile.write(body)
            self.server.manager.logger.warning(
                "control auth denied: %s %s -> %s",
                self.client_address[0], self.path, reason)
            with contextlib.suppress(Exception):
                self.server.manager.metrics.transport_auth_failures.inc(
                    reason=reason.split()[0] if reason else "unknown")
            return False
        return True

    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._check_auth():
            return
        path, qs = self._path_parts()
        mgr = self.server.manager
        try:
            if path == "/":
                return self._json(200, {
                    "name": "astcie.transport", "version": __version__,
                    "endpoints": len(mgr._outputs)})
            if path == "/metrics":
                return self._text(
                    200, mgr.metrics.render(),
                    "text/plain; version=0.0.4; charset=utf-8")
            if path == "/health":
                return self._json(200, {"ok": True})
            if path == "/ready":
                running = sum(1 for k in mgr._outputs if mgr.is_alive(k))
                total = len(mgr._outputs)
                ready = total == 0 or running > 0
                return self._json(200 if ready else 503,
                                  {"ready": ready, "running": running,
                                   "total": total})
            if path == "/status":
                return self._json(200, {
                    "summary": mgr.status_summary(),
                    "endpoints": [
                        {"key": rt.endpoint.key, "mode": rt.stats.mode,
                         "target": rt.stats.target, "state": rt.stats.state,
                         "uptime_sec": rt.stats.uptime_sec,
                         "restarts": rt.stats.restarts,
                         "bitrate_kbps": rt.stats.bitrate_kbps,
                         "bytes_sent": rt.stats.bytes_sent,
                         "last_error": rt.stats.last_error,
                         "health": dataclasses.asdict(rt.health),
                         "breaker": rt.breaker.snapshot() if rt.breaker else None}
                        for rt in mgr._outputs.values()
                    ]})
            if path == "/snapshot":
                return self._json(200, mgr.snapshot())
            if path == "/events":
                n = safe_int((qs.get("limit") or ["64"])[0], 64)
                return self._json(200, [e.to_dict()
                                        for e in mgr.events.history(n)])
            if path == "/endpoints":
                return self._json(200, [
                    {"key": rt.endpoint.key,
                     "endpoint": rt.endpoint.to_dict(),
                     "stats": rt.stats.snapshot()}
                    for rt in mgr._outputs.values()])
            if path.startswith("/endpoints/"):
                key = urllib.parse.unquote(path[len("/endpoints/"):])
                rt = mgr._outputs.get(key)
                if rt is None:
                    return self._json(404, {"error": "not found", "key": key})
                return self._json(200, {
                    "endpoint": rt.endpoint.to_dict(),
                    "stats": rt.stats.snapshot(),
                    "health": dataclasses.asdict(rt.health),
                    "breaker": rt.breaker.snapshot() if rt.breaker else None})
            return self._json(404, {"error": "unknown path", "path": path})
        except Exception as exc:
            self.server.manager.logger.exception("control GET error")
            return self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        path, _ = self._path_parts()
        mgr = self.server.manager
        try:
            if path == "/endpoints":
                data = self._read_json()
                ep = ConfigSchema.validate_endpoint_dict(data)
                key = mgr.add_endpoint(ep,
                                       auto_start=bool(data.get("start", True)))
                return self._json(201, {"key": key})
            if path.startswith("/endpoints/"):
                parts = path[len("/endpoints/"):].split("/")
                if len(parts) == 1:
                    return self._json(400, {"error": "missing action"})
                key = urllib.parse.unquote(parts[0])
                action = parts[1]
                if action == "start":
                    ok = mgr._start_endpoint(key)
                    return self._json(200 if ok else 409,
                                      {"ok": ok, "key": key})
                if action == "stop":
                    mgr.stop(key)
                    return self._json(200, {"ok": True, "key": key})
                if action == "restart":
                    mgr.stop(key); time.sleep(0.1)
                    ok = mgr._start_endpoint(key)
                    return self._json(200, {"ok": ok, "key": key})
                if action == "drain":
                    # graceful drain only, no restart
                    rt = mgr._outputs.get(key)
                    if rt is None:
                        return self._json(404, {"error": "not found"})
                    mgr._drain_one(rt)
                    return self._json(200, {"ok": True, "key": key})
                return self._json(404, {"error": "unknown action"})
            if path == "/stop_all":
                mgr.stop_all()
                return self._json(200, {"ok": True})
            if path == "/reload":
                data = self._read_json()
                mgr.stop_all()
                for item in data.get("endpoints", []):
                    ep = ConfigSchema.validate_endpoint_dict(item)
                    mgr.add_endpoint(ep, auto_start=False)
                if data.get("start", True):
                    mgr.start_all()
                return self._json(200, {"ok": True,
                                        "count": len(mgr._outputs)})
            return self._json(404, {"error": "unknown path", "path": path})
        except SchemaError as exc:
            with contextlib.suppress(Exception):
                mgr.metrics.transport_schema_failures.inc(kind="endpoint")
            return self._json(400, {"error": str(exc), "kind": "schema"})
        except ValidationError as exc:
            return self._json(400, {"error": str(exc), "kind": "validation"})
        except Exception as exc:
            self.server.manager.logger.exception("control POST error")
            return self._json(500, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self._check_auth():
            return
        path, _ = self._path_parts()
        mgr = self.server.manager
        try:
            if path.startswith("/endpoints/"):
                key = urllib.parse.unquote(path[len("/endpoints/"):])
                ok = mgr.remove_endpoint(key, stop=True)
                return self._json(200 if ok else 404,
                                  {"ok": ok, "key": key})
            return self._json(404, {"error": "unknown path"})
        except Exception as exc:
            return self._json(500, {"error": str(exc)})

    def do_HEAD(self) -> None:
        return self.do_GET()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ControlServer:
    def __init__(self, manager: "TransportManager",
                 host: str = "127.0.0.1", port: int = 9090,
                 auth: Opt[ControlAuth] = None):
        self.manager = manager
        self.host = host
        self.port = port
        self.auth = auth
        self._httpd: Opt[_ThreadingHTTPServer] = None
        self._thread: Opt[threading.Thread] = None
        self._lock = threading.RLock()

    def start(self, background: bool = True) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            httpd = _ThreadingHTTPServer((self.host, self.port),
                                         _ControlHandler)
            httpd.manager = self.manager   # type: ignore[attr-defined]
            httpd.auth = self.auth         # type: ignore[attr-defined]
            self._httpd = httpd
            if self.auth and self.auth.enabled:
                self.manager.logger.info(
                    "control auth enabled (token=%s users=%d ip_allow=%d)",
                    "yes" if self.auth.token else "no",
                    len(self.auth.users), len(self.auth.ip_allow))
            else:
                self.manager.logger.warning(
                    "control server running WITHOUT auth on %s:%d — "
                    "bind to 127.0.0.1 or provide ControlAuth",
                    self.host, self.port)
            if background:
                self._thread = threading.Thread(
                    target=httpd.serve_forever, name="transport-control",
                    kwargs={"poll_interval": 0.5}, daemon=True)
                self._thread.start()
                self.manager.logger.info(
                    "Control server listening on http://%s:%d",
                    self.host, self.port)

    def serve_forever(self) -> None:
        self.start(background=False)
        assert self._httpd is not None
        self._httpd.serve_forever()

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
        if httpd:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def bound_port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.port


def attach_control_server(manager: "TransportManager",
                          host: str = "127.0.0.1", port: int = 9090,
                          auth: Opt[ControlAuth] = None) -> ControlServer:
    if auth is None and host not in ("127.0.0.1", "::1", "localhost"):
        token = secrets.token_urlsafe(24)
        auth = ControlAuth(token=token)
        manager.logger.warning(
            "Non-loopback bind — generated control token: %s", token)
    srv = ControlServer(manager, host=host, port=port, auth=auth)
    srv.start(background=True)
    setattr(manager, "_control_server", srv)
    return srv


# ============================================================================
# 23. TRANSPORT MANAGER
# ============================================================================

class TransportManager:
    def __init__(self, config: Any = None, *,
                 manager_config: Opt[ManagerConfig] = None):
        self.logger = logging.getLogger(LOGGER_NAME)
        self.logger.setLevel(logging.INFO)

        self._config_legacy = config
        self._config = manager_config or self._derive_manager_config(config)

        self._outputs: "OrderedDict[str, _OutputRuntime]" = OrderedDict()
        self._lock = threading.RLock()
        self._supervisor_thread: Opt[threading.Thread] = None
        self._supervisor_stop = threading.Event()
        self._source_ts: Opt[Path] = None
        self._source_url: Opt[str] = None
        self._started_at: float = 0.0
        self._closed: bool = False
        self._last_supervisor_tick = 0.0

        self._events = EventBus()
        self._metrics = MetricsRegistry()
        self._watchdog: Opt[Watchdog] = None
        self._limiter = ResourceLimiter(
            root=self._config.cgroup_root,
            enabled=self._config.enable_resource_limits)

        if self._config.json_logging:
            configure_json_logging(self.logger, self._config.log_level)

        self._install_signal_handlers()

    # ------------------------------------------------------------------
    @staticmethod
    def _derive_manager_config(cfg: Any) -> ManagerConfig:
        mc = ManagerConfig()
        if cfg is None:
            return mc
        for attr in ("udp_addr", "rtp_addr", "srt_addr", "rist_addr",
                     "rtmp_addr", "http_addr", "no_encode", "output_dir"):
            if hasattr(cfg, attr):
                val = getattr(cfg, attr)
                if val is not None:
                    setattr(mc, attr, val)
        if hasattr(cfg, "output_dir") and getattr(cfg, "output_dir"):
            mc.output_dir = Path(getattr(cfg, "output_dir"))
        return mc

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (getattr(signal, "SIGINT", None),
                    getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                prev = signal.getsignal(sig)

                def _handler(signum, frame, _prev=prev):
                    self.logger.info("Signal %s — stopping transports", signum)
                    with contextlib.suppress(Exception):
                        self.close()
                    if callable(_prev):
                        with contextlib.suppress(Exception):
                            _prev(signum, frame)

                signal.signal(sig, _handler)
            except Exception:
                pass

    def _emit(self, type_: EventType, **kwargs: Any) -> None:
        if not self._config.enable_events:
            return
        with contextlib.suppress(Exception):
            self._events.emit(type_, **kwargs)

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def metrics(self) -> MetricsRegistry:
        return self._metrics

    @property
    def limiter(self) -> ResourceLimiter:
        return self._limiter

    # ------------------------------------------------------------------
    def set_source_ts(self, path: Opt[Path]) -> None:
        self._source_ts = Path(path) if path else None

    def set_source_url(self, url: Opt[str]) -> None:
        self._source_url = url

    def _resolve_source(self) -> Tuple[Opt[Path], Opt[str]]:
        return self._source_ts, self._source_url

    # ------------------------------------------------------------------
    def add_endpoint(self, ep: TransportEndpoint, *,
                     auto_start: bool = False) -> str:
        ep.validate()
        key = ep.key
        with self._lock:
            if key in self._outputs:
                self.logger.info("Endpoint already registered: %s", key)
                return key
            if len(self._outputs) >= self._config.max_concurrent_processes:
                raise TransportError(
                    f"max_concurrent_processes "
                    f"({self._config.max_concurrent_processes}) reached")
            rt = _OutputRuntime(endpoint=ep)
            rt.breaker = CircuitBreaker(
                threshold=ep.breaker_threshold,
                cooldown=ep.breaker_cooldown_sec,
                name=key)
            rt.backoff = Backoff(
                base=ep.base_backoff_sec,
                max_=ep.max_backoff_sec,
                jitter=ep.restart_jitter)
            rt.stats.mode = ep.mode.value
            rt.stats.target = ep.display
            self._outputs[key] = rt
        self._emit(EventType.CREATED, key=key,
                   message="endpoint registered", mode=ep.mode.value)
        if auto_start:
            self._start_endpoint(key)
        return key

    def remove_endpoint(self, key: str, *, stop: bool = True) -> bool:
        with self._lock:
            rt = self._outputs.get(key)
        if rt is None:
            return False
        if stop:
            self._stop_one(rt)
        with self._lock:
            self._outputs.pop(key, None)
        self.limiter.remove_cgroup(rt._cgroup_path)
        return True

    def get_endpoint(self, key: str) -> Opt[TransportEndpoint]:
        rt = self._outputs.get(key)
        return rt.endpoint if rt else None

    def list_endpoints(self) -> List[TransportEndpoint]:
        return [rt.endpoint for rt in self._outputs.values()]

    def list_keys(self) -> List[str]:
        return list(self._outputs.keys())

    # ------------------------------------------------------------------
    def start(
        self,
        mode: Opt[OutputMode] = None,
        source_ts: Opt[Path] = None,
        addr: Opt[str] = None,
        output_path: Opt[Path] = None,
        endpoints: Opt[List[TransportEndpoint]] = None,
        enable_even_if_no_encode: bool = True,
    ) -> None:
        if source_ts is not None:
            self.set_source_ts(source_ts)
        if self._config.no_encode and not enable_even_if_no_encode:
            self.logger.info("Transport skipped (--no-encode)")
            return
        if endpoints is None:
            endpoints = [self._default_endpoint(
                mode or OutputMode.UDP, addr, output_path)]
        for ep in endpoints:
            if ep.enabled:
                self.add_endpoint(ep, auto_start=False)
        self.start_all()

    def _default_endpoint(self, mode: OutputMode, addr: Opt[str],
                          output_path: Opt[Path]) -> TransportEndpoint:
        cfg = self._config
        ep = TransportEndpoint(mode=mode)
        if mode == OutputMode.FILE:
            ep.output_path = output_path or (cfg.output_dir / "final.ts")
            ep.target = str(ep.output_path)
        elif mode == OutputMode.UDP:
            ep.target = addr or cfg.udp_addr
        elif mode == OutputMode.RTP:
            ep.target = addr or cfg.rtp_addr
            ep.sdp_path = cfg.output_dir / "vectra.sdp"
        elif mode == OutputMode.SRT:
            ep.target = addr or cfg.srt_addr
        elif mode == OutputMode.RIST:
            ep.target = addr or cfg.rist_addr
        elif mode in (OutputMode.RTMP, OutputMode.RTMPS):
            ep.target = addr or cfg.rtmp_addr
        elif mode in (OutputMode.HTTP, OutputMode.HTTPS):
            ep.target = addr or cfg.http_addr
        else:
            ep.target = addr or ""
        return ep

    # ------------------------------------------------------------------
    def start_all(self) -> None:
        with self._lock:
            keys = list(self._outputs.keys())
        for k in keys:
            self._start_endpoint(k)
        self._ensure_supervisor()
        self._ensure_watchdog()

    def _start_endpoint(self, key: str) -> bool:
        rt = self._outputs.get(key)
        if rt is None:
            return False
        ep = rt.endpoint
        if not ep.enabled:
            rt.stats.mark(TransportState.DISABLED)
            return False

        src_path, src_url = self._resolve_source()

        if ep.failover_inputs:
            if rt._failover is None:
                rt._failover = InputFailover(
                    ep.failover_inputs, probe_sec=ep.failover_probe_sec)
            src_url = rt._failover.current()

        if (src_path is None and src_url is None
                and ep.mode not in (OutputMode.NULL,)):
            self.logger.warning("No source for %s — skipping", key)
            return False
        if src_path is not None and not Path(src_path).exists() \
                and src_url is None:
            self.logger.warning("Source TS missing: %s", src_path)
            return False

        if rt.breaker and not rt.breaker.allow():
            rt.stats.mark(TransportState.ERROR)
            rt.stats.last_error = "circuit breaker open"
            self._emit(EventType.BREAKER_OPEN, key=key,
                       message="refusing to start (breaker open)")
            self._metrics.transport_breaker_open.set(
                1, mode=ep.mode.value, key=key)
            return False

        if ep.mode == OutputMode.FILE:
            return self._start_file(rt)
        return self._start_process(rt, src_path, src_url)

    # ------------------------------------------------------------------
    def _start_file(self, rt: _OutputRuntime) -> bool:
        ep = rt.endpoint
        dest = (Path(ep.output_path) if ep.output_path
                else self._config.output_dir / "final.ts")
        dest.parent.mkdir(parents=True, exist_ok=True)

        if ep.mode == OutputMode.FILE:
            try:
                src = Path(self._source_ts) if self._source_ts else None
                if src and src.resolve() != dest.resolve() and src.exists():
                    if not dest.exists():
                        shutil.copy2(src, dest)
            except Exception as exc:
                self.logger.warning("FILE mirror failed: %s", exc)
            rt.stats.mark(TransportState.RUNNING)
            rt.stats.start_time = time.time()
            self._metrics.transport_running.set(
                1, mode=ep.mode.value, key=ep.key)
            self._emit(EventType.STARTED, key=ep.key,
                       message=f"FILE ready -> {dest}")
            self.logger.info("FILE ready -> %s", dest)
            return True

        return self._start_process(rt, self._source_ts, self._source_url)

    # ------------------------------------------------------------------
    def _start_process(self, rt: _OutputRuntime,
                       src_path: Opt[Path],
                       src_url: Opt[str]) -> bool:
        ep = rt.endpoint
        rt.stop_event.clear()
        rt.stats.mark(TransportState.STARTING)
        rt.stats.last_error = ""
        self._emit(EventType.STARTING, key=ep.key,
                   mode=ep.mode.value, target=ep.display)

        builder = CommandBuilder(
            ep,
            ffmpeg_bin=self._config.ffmpeg_bin,
            source_ts=src_path,
            source_url=src_url,
            global_args=self._config.extra_ffmpeg_global_args,
            stats_period=1.0,
            progress_pipe="pipe:1",
            global_rate_limit_kbps=self._config.global_rate_limit_kbps,
        )
        try:
            cmd = builder.build()
        except Exception as exc:
            rt.stats.mark(TransportState.ERROR)
            rt.stats.last_error = str(exc)
            self.logger.error("Command build failed for %s: %s",
                              ep.key, exc)
            self._emit(EventType.ERROR, key=ep.key, message=str(exc))
            return False

        # Apply systemd-run prefix if chosen
        unit_name = "astcie-" + hashlib.sha1(
            ep.key.encode()).hexdigest()[:10]
        cmd, kind = self._limiter.prefix_cmd(cmd, ep.resource_limits,
                                             unit_name)
        if kind == ResourceLimitKind.SYSTEMD_RUN:
            rt._systemd_unit = unit_name

        # WHIP env vars
        if ep.mode == OutputMode.WHIP:
            rt.env_overrides.update(WHIPHelper.build_env(ep))

        self.logger.debug(
            "CMD: %s", " ".join(shlex.quote(c) for c in cmd))
        supervisor = ProcessSupervisor(rt, manager=self)
        try:
            proc = supervisor.spawn(cmd)
        except ProcessError as exc:
            rt.stats.mark(TransportState.ERROR)
            rt.stats.last_error = str(exc)
            rt.stats.consecutive_failures += 1
            if rt.breaker:
                rt.breaker.on_failure()
            self._metrics.transport_errors.inc(
                mode=ep.mode.value, key=ep.key, kind="spawn")
            self._emit(EventType.ERROR, key=ep.key, message=str(exc))
            return False

        # Apply cgroup v2 if chosen
        if kind == ResourceLimitKind.CGROUP_V2 and proc.pid:
            rt._cgroup_path = self._limiter.create_cgroup(
                unit_name, ep.resource_limits, proc.pid)

        supervisor.start_reader_threads()

        rt.stats.mark(TransportState.RUNNING)
        rt.stats.start_time = time.time()
        if rt.breaker:
            rt.breaker.on_success()
        if rt.backoff:
            rt.backoff.reset()
        rt.stats.consecutive_failures = 0
        rt.last_bytes_snapshot = 0

        self._metrics.transport_starts.inc(mode=ep.mode.value, key=ep.key)
        self._metrics.transport_running.set(
            1, mode=ep.mode.value, key=ep.key)
        self._metrics.transport_breaker_open.set(
            0, mode=ep.mode.value, key=ep.key)

        self._emit(EventType.STARTED, key=ep.key,
                   mode=ep.mode.value, target=ep.display)
        self.logger.info("Started %s -> %s",
                         ep.mode.value.upper(), redact_url(ep.display))

        if ep.mode in (OutputMode.RTP, OutputMode.SRTP) and ep.sdp_path:
            ManifestWriter.write_sdp(ep)
        if ep.mode == OutputMode.HLS:
            ManifestWriter.write_m3u8_stub(ep)
            if ep.hls_master:
                ManifestWriter.write_hls_master(ep)
        if ep.mode == OutputMode.DASH:
            ManifestWriter.write_dash_mpd_stub(ep)

        if ep.health_enabled and ep.health_probe != "none" \
                and self._config.enable_health_checks:
            self._start_health(rt)

        return True

    # ------------------------------------------------------------------
    def _start_health(self, rt: _OutputRuntime) -> None:
        ep = rt.endpoint

        def _probe() -> HealthReport:
            return self._process_health_probe(rt)

        checker = HealthChecker(ep, probe_fn=_probe)

        def _cb(report: HealthReport) -> None:
            rt.health = report
            self._metrics.transport_health.set(
                report.status.numeric, mode=ep.mode.value, key=ep.key)
            self._emit(EventType.HEALTH, key=ep.key,
                       message=report.message,
                       status=report.status.value,
                       latency_ms=report.latency_ms)

        checker.set_callback(_cb)
        checker.start()
        rt._health_checker = checker

    def _stop_health(self, rt: _OutputRuntime) -> None:
        checker = rt._health_checker
        if checker:
            with contextlib.suppress(Exception):
                checker.stop()
            rt._health_checker = None

    def _process_health_probe(self, rt: _OutputRuntime) -> HealthReport:
        ep = rt.endpoint
        now = time.time()
        if ep.health_probe == "none":
            return HealthReport(HealthStatus.OK, message="disabled",
                                probe="none", checked_at=now)

        if rt.proc is None:
            if rt.stats.state == TransportState.RUNNING.value:
                return HealthReport(HealthStatus.OK,
                                    message="no child (mirror)",
                                    probe="process", checked_at=now)
            return HealthReport(HealthStatus.DEAD, message="no process",
                                probe="process", checked_at=now)

        code = rt.proc.poll()
        if code is not None:
            return HealthReport(HealthStatus.DEAD,
                                message=f"process exited with code {code}",
                                probe="process", checked_at=now)

        idle_timeout = max(3.0, ep.health_timeout_sec * 3.0)
        activity_ts = max(
            rt.stats.last_stderr_ts,
            rt.stats.last_stdout_ts,
            rt.stats.last_progress_ts,
            rt.stats.start_time)
        since = now - activity_ts

        if since > idle_timeout:
            return HealthReport(HealthStatus.FAILING,
                                message=f"no output for {since:.1f}s",
                                probe="process", checked_at=now)
        if since > idle_timeout / 2:
            return HealthReport(HealthStatus.DEGRADED,
                                message=f"slow output ({since:.1f}s idle)",
                                probe="process", checked_at=now)
        return HealthReport(HealthStatus.OK,
                            message=f"running, last activity {since:.1f}s ago",
                            probe="process", checked_at=now)

    # ------------------------------------------------------------------
    def stop_all(self) -> None:
        self._supervisor_stop.set()
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            self._supervisor_thread.join(timeout=3)
        self._supervisor_thread = None
        if self._watchdog:
            self._watchdog.stop()
            self._watchdog = None

        with self._lock:
            runtimes = list(self._outputs.values())
        for rt in runtimes:
            self._stop_one(rt)
        self._emit(EventType.STOPPED, message="all endpoints stopped")
        self.logger.info("All transports stopped")

    def stop(self, key: Opt[str] = None) -> None:
        if key is None:
            return self.stop_all()
        rt = self._outputs.get(key)
        if rt:
            self._stop_one(rt)

    def _drain_one(self, rt: _OutputRuntime,
                   timeout: Opt[float] = None) -> bool:
        """Graceful drain only (no stop of ffmpeg if not needed)."""
        ep = rt.endpoint
        if rt.proc is None or rt.proc.poll() is not None:
            return False
        if not ep.mode.needs_graceful_drain:
            return False
        rt.stats.mark(TransportState.DRAINING)
        self._emit(EventType.DRAINING, key=ep.key,
                   message="graceful drain started")
        supervisor = ProcessSupervisor(rt, manager=self)
        grace = (timeout
                 or (ep.hls_drain_timeout_sec
                     if ep.mode == OutputMode.HLS
                     else ep.dash_drain_timeout_sec)
                 or self._config.drain_timeout_sec)
        ok = supervisor.graceful_finish(timeout=grace)
        if ok:
            rt.proc = None
            supervisor._join_threads()
            rt.stats.mark(TransportState.STOPPED)
            self._metrics.transport_running.set(
                0, mode=ep.mode.value, key=ep.key)
        return ok

    def _stop_one(self, rt: _OutputRuntime) -> None:
        ep = rt.endpoint
        rt.stats.mark(TransportState.STOPPING)
        rt.stop_event.set()
        self._stop_health(rt)

        if rt.proc:
            supervisor = ProcessSupervisor(rt, manager=self)
            drained = False
            if ep.mode.needs_graceful_drain:
                drained = self._drain_one(rt)
            if not drained:
                supervisor.terminate(grace=self._config.shutdown_grace_sec)

        # cleanup resource limits
        self.limiter.remove_cgroup(rt._cgroup_path)
        rt._cgroup_path = None

        rt.stats.mark(TransportState.STOPPED)
        rt.stats.uptime_sec = 0.0
        self._metrics.transport_running.set(0, mode=ep.mode.value, key=ep.key)
        self._metrics.transport_stops.inc(mode=ep.mode.value, key=ep.key)
        self._emit(EventType.STOPPED, key=ep.key,
                   mode=ep.mode.value, target=ep.display)
        self.logger.info("Stopped %s %s", ep.mode.value.upper(), ep.display)

    # ------------------------------------------------------------------
    def is_alive(self, key: Opt[str] = None) -> bool:
        if key is not None:
            rt = self._outputs.get(key)
            return bool(rt and rt.proc and rt.proc.poll() is None)
        return any(self.is_alive(k) for k in self._outputs)

    def get_stats(self) -> List[TransportStats]:
        now = time.time()
        out: List[TransportStats] = []
        for rt in self._outputs.values():
            st = rt.stats
            if st.state == TransportState.RUNNING.value and st.start_time:
                st.uptime_sec = round(now - st.start_time, 1)
            st.bitrate_kbps = st.compute_bitrate()
            out.append(st)
        return out

    def status_summary(self) -> str:
        now = time.time()
        lines = ["Transport Status", "-" * 72]
        any_ = False
        for rt in self._outputs.values():
            st = rt.stats
            if st.state == TransportState.RUNNING.value and st.start_time:
                uptime = now - st.start_time
            else:
                uptime = st.uptime_sec
            any_ = True
            lines.append(
                f"  {st.mode.upper():7} "
                f"{shorten(st.target or st.mode, 32):32} "
                f"{st.state:12} "
                f"up={human_duration(uptime):>8} "
                f"restarts={st.restarts:>3} "
                f"br={st.bitrate_kbps:>7.1f}kbps")
            if st.last_error:
                lines.append(
                    f"           last_error: {shorten(st.last_error, 90)}")
        if not any_:
            lines.append("  (no endpoints)")
        return "\n".join(lines)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "started_at": self._started_at,
            "source_ts": str(self._source_ts) if self._source_ts else None,
            "source_url": self._source_url,
            "endpoints": [
                {"endpoint": rt.endpoint.to_dict(),
                 "stats": rt.stats.snapshot(),
                 "health": dataclasses.asdict(rt.health),
                 "breaker": rt.breaker.snapshot() if rt.breaker else None,
                 "cgroup": str(rt._cgroup_path) if rt._cgroup_path else None,
                 "systemd_unit": rt._systemd_unit}
                for rt in self._outputs.values()],
            "metrics": self._metrics.snapshot(),
            "watchdog": self._watchdog.snapshot() if self._watchdog else None,
        }

    # ------------------------------------------------------------------
    def _ensure_supervisor(self) -> None:
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="transport-supervisor", daemon=True)
        self._supervisor_thread.start()

    def _restart_supervisor(self) -> None:
        self._supervisor_thread = None
        self._ensure_supervisor()

    def _supervisor_loop(self) -> None:
        tick = self._config.supervisor_tick_sec
        while not self._supervisor_stop.wait(tick):
            self._last_supervisor_tick = time.time()
            if self._watchdog:
                self._watchdog.note_supervisor_tick()
            try:
                self._supervisor_tick()
            except Exception:
                self.logger.exception("Supervisor tick error")
            self._emit(EventType.SUPERVISOR_TICK, message="tick")

    def _supervisor_tick(self) -> None:
        with self._lock:
            runtimes = list(self._outputs.values())
        alive_count = 0
        for rt in runtimes:
            proc = rt.proc
            if proc is None:
                continue
            code = proc.poll()
            if code is None:
                alive_count += 1
                ep = rt.endpoint
                if rt.stats.start_time:
                    self._metrics.transport_uptime.set(
                        time.time() - rt.stats.start_time,
                        mode=ep.mode.value, key=ep.key)
                self._metrics.transport_bitrate.set(
                    rt.stats.compute_bitrate(),
                    mode=ep.mode.value, key=ep.key)
                cur = rt.stats.bytes_sent
                prev = rt.last_bytes_snapshot
                if cur > prev:
                    self._metrics.transport_bytes_total.inc(
                        cur - prev, mode=ep.mode.value, key=ep.key)
                rt.last_bytes_snapshot = cur
                if (rt.health.status == HealthStatus.FAILING
                        and ep.reconnect_policy != ReconnectPolicy.NEVER
                        and not rt.restart_pending):
                    self.logger.warning(
                        "Health FAILING on %s — forcing restart", ep.key)
                    self._emit(EventType.WARN, key=ep.key,
                               message="health failing, forcing restart")
                    with contextlib.suppress(Exception):
                        proc.terminate()
                continue
            if rt.stop_event.is_set():
                rt.stats.mark(TransportState.STOPPED)
                continue
            self._on_process_death(rt, code)
        self._metrics.active_processes.set(alive_count)

    def _on_process_death(self, rt: _OutputRuntime, code: int) -> None:
        ep = rt.endpoint
        rt.stats.consecutive_failures += 1
        rt.stats.last_error = f"process exited with code {code}"
        rt.stats.mark(TransportState.ERROR)

        if rt._failover is not None:
            new_url = rt._failover.mark_failure()
            if new_url:
                self._metrics.transport_failovers.inc(
                    mode=ep.mode.value, key=ep.key)
                self._emit(EventType.FAILOVER, key=ep.key,
                           message=f"switching to {new_url}")
                self.logger.warning("Failover %s -> %s", ep.key, new_url)

        if rt.breaker:
            rt.breaker.on_failure()
            if rt.breaker.state == CircuitBreaker.OPEN:
                self._metrics.transport_breaker_open.set(
                    1, mode=ep.mode.value, key=ep.key)
                self._emit(EventType.BREAKER_OPEN, key=ep.key,
                           message="breaker opened")

        self._metrics.transport_errors.inc(
            mode=ep.mode.value, key=ep.key, kind=f"exit_{code}")
        self._emit(EventType.ERROR, key=ep.key,
                   message=rt.stats.last_error, exit_code=code)

        self.logger.error("Transport died: %s %s (code=%s)",
                          ep.mode.value, ep.display, code)

        if ep.reconnect_policy == ReconnectPolicy.NEVER:
            return
        if rt.restart_pending:
            return
        self._schedule_restart(rt)

    def _schedule_restart(self, rt: _OutputRuntime) -> None:
        ep = rt.endpoint
        with rt.restart_lock:
            if rt.restart_pending:
                return
            rt.restart_pending = True
            if ep.max_restarts >= 0 and rt.stats.restarts >= ep.max_restarts:
                rt.stats.mark(TransportState.ERROR)
                self.logger.error(
                    "Max restarts (%s) reached for %s — giving up",
                    ep.max_restarts, ep.display)
                rt.restart_pending = False
                return
            backoff = rt.backoff.next_delay() if rt.backoff else 1.0
            rt.stats.restarts += 1
            rt.stats.total_restarts += 1
            rt.stats.mark(TransportState.RESTARTING)

        self._metrics.transport_restarts.inc(mode=ep.mode.value, key=ep.key)
        self._emit(EventType.RESTARTING, key=ep.key,
                   message=f"restart in {backoff:.1f}s",
                   attempt=rt.stats.restarts)
        self.logger.info("Restarting %s in %.1fs (attempt %s)",
                         ep.display, backoff, rt.stats.restarts)

        def _do_restart() -> None:
            try:
                if rt.stop_event.wait(backoff):
                    return
                if self._supervisor_stop.is_set():
                    return
                with rt.restart_lock:
                    rt.proc = None
                self._start_endpoint(ep.key)
            except Exception:
                self.logger.exception("Restart failed for %s", ep.key)
            finally:
                with rt.restart_lock:
                    rt.restart_pending = False

        threading.Thread(target=_do_restart,
                         name=f"restart-{ep.key}",
                         daemon=True).start()

    # ------------------------------------------------------------------
    def _ensure_watchdog(self) -> None:
        if not self._config.enable_watchdog:
            return
        if self._watchdog and self._watchdog._thread \
                and self._watchdog._thread.is_alive():
            return
        self._watchdog = Watchdog(
            self,
            interval=self._config.watchdog_interval_sec,
            stall_sec=self._config.watchdog_stall_sec)
        self._watchdog.start()

    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_all()
        srv = getattr(self, "_control_server", None)
        if srv:
            with contextlib.suppress(Exception):
                srv.stop()
            setattr(self, "_control_server", None)
        self._events.close()

    def __enter__(self) -> "TransportManager":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<TransportManager endpoints={len(self._outputs)} "
                f"running={sum(1 for k in self._outputs if self.is_alive(k))}>")


# ============================================================================
# 24. FACTORIES & DIAGNOSTICS
# ============================================================================

def build_endpoint(mode: Union[str, OutputMode], target: str = "",
                   *, name: str = "",
                   output_path: Opt[Union[str, Path]] = None,
                   **kwargs: Any) -> TransportEndpoint:
    m = OutputMode.from_str(mode) if isinstance(mode, str) else mode
    ep = TransportEndpoint(mode=m, name=name, target=target)
    if output_path is not None:
        ep.output_path = Path(output_path)
    if m in (OutputMode.RTP, OutputMode.SRTP) and not kwargs.get("sdp_path"):
        if ep.output_path:
            ep.sdp_path = ep.output_path.with_suffix(".sdp")
    for k, v in kwargs.items():
        if hasattr(ep, k):
            setattr(ep, k, v)
    return ep


def build_multivariant_hls_endpoint(
    output_dir: Union[str, Path],
    variants: Sequence[Mapping[str, Any]],
    *,
    name: str = "hls",
    segment_sec: float = 6.0,
    playlist_size: int = 6,
    **kwargs: Any,
) -> TransportEndpoint:
    """
    Build an HLS endpoint with a variant ladder and master playlist.

    Example
    -------
    ep = build_multivariant_hls_endpoint(
        "out/live",
        variants=[
            {"name": "1080p", "resolution": "1920x1080",
             "video_bitrate": "4500k", "maxrate": "5000k",
             "bufsize": "9000k", "audio_bitrate": "160k",
             "bandwidth": 5_000_000},
            {"name": "720p", "resolution": "1280x720",
             "video_bitrate": "2500k", "maxrate": "3000k",
             "bufsize": "6000k", "audio_bitrate": "128k",
             "bandwidth": 3_000_000},
            {"name": "480p", "resolution": "854x480",
             "video_bitrate": "1000k", "audio_bitrate": "96k",
             "bandwidth": 1_200_000},
        ],
    )
    """
    mv = MultiVariantHLS(variants)
    ep = TransportEndpoint(
        mode=OutputMode.HLS,
        name=name,
        output_path=Path(output_dir),
        segment_sec=segment_sec,
        playlist_size=playlist_size,
        hls_master=True,
        hls_variants=mv.manifest_entries(segment_sec),
        read_rate=False,
    )
    for k, v in kwargs.items():
        if hasattr(ep, k):
            setattr(ep, k, v)
    return ep


def endpoint_to_dict(ep: TransportEndpoint) -> Dict[str, Any]:
    return ep.to_dict()


def endpoint_from_dict(data: Mapping[str, Any]) -> TransportEndpoint:
    return TransportEndpoint.from_dict(data)


def make_transport_from_config(config: Any) -> TransportManager:
    return TransportManager(config)


def ffmpeg_version(binary: str = DEFAULT_FFMPEG_BIN) -> Opt[str]:
    try:
        out = subprocess.run([binary, "-version"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.splitlines()[0]
    except Exception:
        return None
    return None


def ffmpeg_has_protocol(protocol: str,
                        binary: str = DEFAULT_FFMPEG_BIN) -> bool:
    try:
        out = subprocess.run([binary, "-protocols"], capture_output=True,
                             text=True, timeout=5)
        return protocol.lower() in (out.stdout or "").lower()
    except Exception:
        return False


def probe_ts(path: Union[str, Path],
             ffprobe: str = DEFAULT_FFPROBE_BIN) -> Dict[str, Any]:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,size,bit_rate:stream=index,codec_type,codec_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return {"error": out.stderr.strip() or "ffprobe failed"}
        return json.loads(out.stdout or "{}")
    except Exception as exc:
        return {"error": str(exc)}


# ============================================================================
# 25. CLI
# ============================================================================

def _cli(argv: Opt[List[str]] = None) -> int:  # pragma: no cover
    p = argparse.ArgumentParser(prog="astcie.transport")
    p.add_argument("target", help="host:port, URL, or file path")
    p.add_argument("--mode", default=None,
                   help="output mode (auto-detected from target when possible)")
    p.add_argument("--source", required=False,
                   help="source TS file or URL")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--json-logging", action="store_true")
    p.add_argument("--control-port", type=int, default=0,
                   help="0 disables the HTTP control server")
    p.add_argument("--control-host", default="127.0.0.1")
    p.add_argument("--control-token", default="",
                   help="bearer token for the control server (empty = no auth)")
    p.add_argument("--control-user", action="append", default=[],
                   metavar="USER:PASS",
                   help="basic auth user (repeatable)")
    p.add_argument("--control-ip-allow", action="append", default=[],
                   metavar="CIDR", help="IP allow list (repeatable)")
    p.add_argument("--rate-limit-kbps", type=int, default=0)
    p.add_argument("--memory-mb", type=int, default=0,
                   help="per-child memory limit (cgroup v2 / systemd)")
    p.add_argument("--cpu-quota-pct", type=int, default=0,
                   help="per-child CPU quota (100 = 1 core)")
    args = p.parse_args(argv)

    lg = (configure_json_logging(level=getattr(logging, args.log_level.upper(),
                                               logging.INFO))
          if args.json_logging
          else configure_plain_logging(
              level=getattr(logging, args.log_level.upper(), logging.INFO)))

    mode = args.mode
    if not mode:
        tgt = args.target.lower()
        for candidate in PROTOCOL_SCHEMES:
            if tgt.startswith(candidate + "://"):
                mode = candidate
                break
        else:
            mode = "file" if "://" not in args.target else "udp"

    ep = build_endpoint(mode, args.target)
    if args.rate_limit_kbps:
        ep.rate_limit_kbps = args.rate_limit_kbps
    if args.memory_mb or args.cpu_quota_pct:
        ep.resource_limits = ResourceLimits(
            memory_mb=args.memory_mb,
            cpu_quota_pct=args.cpu_quota_pct)
    mgr = TransportManager()
    if args.source:
        mgr.set_source_ts(Path(args.source))
    mgr.add_endpoint(ep)

    users = dict(parse_kv_list(args.control_user)) if args.control_user else None
    auth = None
    if args.control_token or users or args.control_ip_allow:
        auth = ControlAuth(token=args.control_token, users=users or {},
                           ip_allow=args.control_ip_allow)

    if args.control_port > 0:
        attach_control_server(mgr, args.control_host, args.control_port,
                              auth=auth)

    print(f"ffmpeg: {ffmpeg_version() or 'NOT FOUND'}")
    print(f"WHIP supported: {WHIPHelper.ffmpeg_supports_whip()}")
    print(mgr.status_summary())
    mgr.start_all()
    try:
        while True:
            time.sleep(5)
            print("\n" + mgr.status_summary())
    except KeyboardInterrupt:
        print("\nStopping…")
        mgr.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
