#!/usr/bin/env python3
"""
QualityEngine — All-in-One
==========================

A hardened, extensible quality measurement engine for video pipelines.
Everything lives in this single file so you can drop it into a project and
go. Optional dependencies (redis, prometheus_client, opentelemetry, grpc,
flask) are discovered lazily and skipped if missing.

Features
--------
  1.  GPU / hardware accelerated decoding (CUDA, QSV, VAAPI, VideoToolbox)
  2.  Scene-aware sampling (histogram-based cut detection + weighted picks)
  3.  Per-title bitrate ladder (Pareto + upper convex hull)
  4.  Redis-backed cache (L1 LRU → L2 Redis → L3 disk)
  5.  Observability (Prometheus + StatsD + OpenTelemetry)
  6.  Scorer ensemble (weighted, strict, ensemble average, Bayesian)
  7.  MOS / DMOS mapping (piecewise + logistic)
  8.  HDR-aware measurement (PQ / HLG / BT.2020 / nits)
  9.  Reference-free metrics (light BRISQUE / NIQE approximations)
 10.  A/B testing (two-sample, bootstrap, sequential test)
 11.  Stress tests and fuzzing helpers (self-test entrypoint)
 12.  REST API (stdlib http.server) + optional gRPC

Public API
----------
  QualityEngine ........... main measurement engine
  QualitySample ........... result of a single measurement
  QualityWeights .......... weights for the default scorer
  ContentProfile .......... per-content tuning preset
  get_profile(name) ....... lookup a profile
  detect_capabilities() ... probe ffmpeg
  detect_hw() ............. probe GPU
  detect_scenes() ......... scene map
  build_ladder() .......... ladder from candidate points
  engine_from_config() .... build engine from a plain dict
  run_server() ............ start the REST API
  _main() ................. CLI entrypoint
"""

from __future__ import annotations

# =============================================================================
# Imports
# =============================================================================

import abc
import argparse
import contextlib
import dataclasses
import enum
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore
import functools
import hashlib
import heapq
import http.server
import json
import logging
import math
import os
import platform
import random
import re
try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore
import shlex
import shutil
import signal
import socket
import socketserver
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import weakref
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional,
    Protocol, Sequence, Set, Tuple, Union,
)

# -----------------------------------------------------------------------------
# Optional deps
# -----------------------------------------------------------------------------

try:
    from ..models import ChannelContext, ControllerConfig  # type: ignore
except Exception:
    @dataclass
    class ChannelContext:  # type: ignore
        channel_id: int
        input_path: Optional[str] = None
        encoded_path: Optional[str] = None
        state: Any = None

    @dataclass
    class ControllerConfig:  # type: ignore
        pass

try:
    import redis as _redis_mod  # type: ignore
except Exception:
    _redis_mod = None

try:
    import prometheus_client as _prom  # type: ignore
except Exception:
    _prom = None

try:
    from opentelemetry import trace as _otel_trace  # type: ignore
    from opentelemetry.trace import Status as _OtelStatus, StatusCode as _OtelStatusCode  # type: ignore
except Exception:
    _otel_trace = None
    _OtelStatus = None
    _OtelStatusCode = None

try:
    import numpy as _np  # type: ignore
except Exception:
    _np = None

try:
    from PIL import Image as _PILImage  # type: ignore
except Exception:
    _PILImage = None

try:
    import grpc as _grpc  # type: ignore
    from concurrent import futures as _grpc_futures  # type: ignore
except Exception:
    _grpc = None
    _grpc_futures = None


logger = logging.getLogger("astcie.quality")

# =============================================================================
# Constants
# =============================================================================

DEFAULT_SAMPLE_SECONDS = 3.0
DEFAULT_SAMPLE_FPS = 5.0
DEFAULT_SAMPLE_WIDTH = 320
DEFAULT_FFMPEG_TIMEOUT = 120.0
DEFAULT_VMAF_TIMEOUT = 180.0
DEFAULT_FFPROBE_TIMEOUT = 20.0
DEFAULT_SCENE_TIMEOUT = 120.0

MAX_INPUT_BYTES = 32 * 1024 * 1024 * 1024
MAX_SAMPLE_SECONDS = 60.0
MIN_SAMPLE_WIDTH = 64
MAX_SAMPLE_WIDTH = 3840
MAX_SAMPLE_FRAMES = 600
PSNR_MIN_DB = 20.0
PSNR_MAX_DB = 50.0
DROP_PENALTY_FULL = 50.0
VMAF_REFERENCE_MAX = 100.0

# Regexes compiled once
_RE_SSIM_ALL = re.compile(r"SSIM.*?All:\s*([0-9]*\.?[0-9]+)")
_RE_SSIM_Y = re.compile(r"SSIM.*?Y:\s*([0-9]*\.?[0-9]+)")
_RE_PSNR_AVG = re.compile(r"PSNR.*?average:\s*([0-9]*\.?[0-9]+)")
_RE_PSNR_AVG_ALT = re.compile(r"psnr_avg:\s*([0-9]*\.?[0-9]+)")
_RE_PSNR_Y = re.compile(r"PSNR.*?y:\s*([0-9]*\.?[0-9]+)")
_RE_MS_SSIM = re.compile(r"MS-SSIM.*?All:\s*([0-9]*\.?[0-9]+)")
_RE_CAMBI = re.compile(r"CAMBI.*?score:\s*([0-9]*\.?[0-9]+)")
_RE_LOUDNESS_I = re.compile(r"^\s*I:\s*(-?[0-9]*\.?[0-9]+)\s*LUFS", re.MULTILINE)
_RE_LOUDNESS_LRA = re.compile(r"^\s*LRA:\s*(-?[0-9]*\.?[0-9]+)\s*LU", re.MULTILINE)
_RE_SCENE_PTS = re.compile(r"pts_time:([0-9]*\.?[0-9]+)")
_RE_SCENE_SCORE = re.compile(r"scene:([0-9]*\.?[0-9]+)")


# =============================================================================
# Exceptions
# =============================================================================

class QualityError(Exception): ...
class QualityTimeout(QualityError): ...
class QualityInputError(QualityError): ...
class QualityBackendError(QualityError): ...
class CircuitOpen(QualityError): ...


# =============================================================================
# Utilities
# =============================================================================

def _now() -> float: return time.monotonic()

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x

def _safe_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f

def _sha256_of_path(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            buf = fp.read(chunk)
            if not buf: break
            h.update(buf)
    return h.hexdigest()

def _sha256_of_strings(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()

def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child_r = child.resolve(strict=False)
        parent_r = parent.resolve(strict=False)
    except OSError:
        return False
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False

def _safe_stat(path: Path) -> os.stat_result:
    try:
        return path.stat()
    except FileNotFoundError as exc:
        raise QualityInputError(f"missing file: {path}") from exc
    except OSError as exc:
        raise QualityInputError(f"cannot stat {path}: {exc}") from exc

def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def _run_capture(argv: Sequence[str], timeout: float,
                 env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout,
        shell=False, env=env, check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class QualityWeights:
    vmaf: float = 0.42
    ssim: float = 0.18
    ms_ssim: float = 0.05
    psnr: float = 0.10
    cambi: float = 0.05
    noref: float = 0.05
    bitrate_fit: float = 0.08
    stability: float = 0.07
    min_quality_weight: float = 0.30
    neutral: float = 0.5

    def quality_only(self) -> Dict[str, float]:
        return {"vmaf": self.vmaf, "ssim": self.ssim,
                "ms_ssim": self.ms_ssim, "psnr": self.psnr,
                "cambi": self.cambi, "noref": self.noref}

    def operational_only(self) -> Dict[str, float]:
        return {"bitrate_fit": self.bitrate_fit,
                "stability": self.stability}


@dataclass
class QualitySample:
    channel_id: int
    vmaf: Optional[float] = None
    vmaf_neg: Optional[float] = None
    ssim: Optional[float] = None
    ms_ssim: Optional[float] = None
    psnr: Optional[float] = None
    cambi: Optional[float] = None
    noref: Optional[float] = None
    loudness_lufs: Optional[float] = None
    loudness_lra: Optional[float] = None
    mos: Optional[float] = None
    dmos: Optional[float] = None

    actual_bitrate_mbps: Optional[float] = None
    target_bitrate_mbps: Optional[float] = None
    frame_drops: int = 0

    hybrid: float = 0.0
    confidence: float = 0.0
    ok: bool = False
    error: str = ""
    error_kind: str = ""

    duration_s: float = 0.0
    backend: str = ""
    hw: str = ""
    profile: str = "default"
    raw: Dict[str, Any] = field(default_factory=dict)
    perf: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def quality_present(self) -> bool:
        return any(v is not None for v in
                   (self.vmaf, self.ssim, self.ms_ssim, self.psnr,
                    self.cambi, self.noref))


@dataclass
class ProbeInfo:
    path: str
    size_bytes: int
    duration_s: Optional[float]
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    vcodec: Optional[str]
    acodec: Optional[str]
    pix_fmt: Optional[str]
    color_transfer: Optional[str] = None
    color_primaries: Optional[str] = None
    color_space: Optional[str] = None
    is_hdr: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentProfile:
    name: str = "default"
    width: int = DEFAULT_SAMPLE_WIDTH
    fps: float = DEFAULT_SAMPLE_FPS
    seconds: float = DEFAULT_SAMPLE_SECONDS
    weights: QualityWeights = field(default_factory=QualityWeights)
    enable_cambi: bool = False
    enable_audio: bool = False
    enable_noref: bool = False
    scene_aware: bool = False
    extra_filters: Tuple[str, ...] = ()


def _make_profile(name: str, **kw) -> ContentProfile:
    return ContentProfile(name=name, **kw)


_PROFILES: Dict[str, Callable[[], ContentProfile]] = {
    "default": lambda: ContentProfile(name="default"),
    "sport": lambda: _make_profile(
        "sport", fps=8.0, seconds=4.0,
        weights=QualityWeights(vmaf=0.5, ssim=0.15, psnr=0.08, cambi=0.02),
    ),
    "animation": lambda: _make_profile(
        "animation", fps=6.0, seconds=3.0,
        weights=QualityWeights(vmaf=0.4, ssim=0.2, psnr=0.1, cambi=0.1),
        enable_cambi=True, scene_aware=True,
    ),
    "film": lambda: _make_profile(
        "film", fps=5.0, seconds=3.0,
        weights=QualityWeights(vmaf=0.45, ssim=0.2, psnr=0.1, cambi=0.05),
        scene_aware=True,
    ),
    "grain": lambda: _make_profile(
        "grain", fps=5.0, seconds=3.0,
        weights=QualityWeights(vmaf=0.35, ssim=0.25, psnr=0.15, cambi=0.05),
    ),
    "hdr": lambda: _make_profile(
        "hdr", fps=4.0, seconds=3.0,
        weights=QualityWeights(vmaf=0.45, ssim=0.15, psnr=0.1, cambi=0.1),
        enable_cambi=True,
    ),
}

def get_profile(name: str) -> ContentProfile:
    return _PROFILES.get(name, _PROFILES["default"])()


# =============================================================================
# Rate limiter
# =============================================================================

class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate_per_sec
        self.capacity = max(1.0, burst)
        self._tokens = self.capacity
        self._last = _now()
        self._lock = threading.Lock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            now = _now()
            self._tokens = min(self.capacity,
                               self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def wait(self, tokens: float = 1.0, timeout: float = 5.0) -> bool:
        deadline = _now() + timeout
        while _now() < deadline:
            if self.try_acquire(tokens):
                return True
            time.sleep(0.05)
        return False


class RateLimiter:
    def __init__(self, rate_per_sec: float = 3.0, burst: float = 6.0) -> None:
        self._rate = rate_per_sec
        self._burst = burst
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = TokenBucket(self._rate, self._burst)
                self._buckets[key] = b
            return b

    def allow(self, key: str, timeout: float = 0.0) -> bool:
        b = self._bucket(key)
        return b.try_acquire() if timeout <= 0 else b.wait(timeout=timeout)


# =============================================================================
# Circuit breaker
# =============================================================================

class _CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5,
                 recovery_timeout: float = 30.0,
                 half_open_successes: int = 2) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_successes = half_open_successes
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _entry(self, key: str) -> Dict[str, Any]:
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                e = {"state": _CircuitState.CLOSED, "failures": 0,
                     "successes": 0, "opened_at": 0.0}
                self._entries[key] = e
            return e

    def allow(self, key: str) -> bool:
        e = self._entry(key)
        with self._lock:
            if e["state"] is _CircuitState.CLOSED:
                return True
            if e["state"] is _CircuitState.OPEN:
                if _now() - e["opened_at"] >= self.recovery_timeout:
                    e["state"] = _CircuitState.HALF_OPEN
                    e["successes"] = 0
                    return True
                return False
            return True

    def success(self, key: str) -> None:
        e = self._entry(key)
        with self._lock:
            if e["state"] is _CircuitState.HALF_OPEN:
                e["successes"] += 1
                if e["successes"] >= self.half_open_successes:
                    e["state"] = _CircuitState.CLOSED
                    e["failures"] = 0
                    e["successes"] = 0
            else:
                e["failures"] = 0

    def failure(self, key: str) -> None:
        e = self._entry(key)
        with self._lock:
            e["failures"] += 1
            if e["state"] is _CircuitState.HALF_OPEN:
                e["state"] = _CircuitState.OPEN
                e["opened_at"] = _now()
                return
            if e["failures"] >= self.failure_threshold:
                e["state"] = _CircuitState.OPEN
                e["opened_at"] = _now()

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)


# =============================================================================
# Caches
# =============================================================================

class _LRU:
    def __init__(self, maxsize: int = 512) -> None:
        self.maxsize = maxsize
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class ResultCache:
    def __init__(self, directory: Optional[Path] = None,
                 memory_size: int = 512, ttl_seconds: float = 3600.0) -> None:
        self.memory = _LRU(memory_size)
        self.directory = Path(directory) if directory else None
        self.ttl = ttl_seconds
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self.directory, 0o700)

    def _paths(self, key: str) -> Tuple[Path, Path]:
        assert self.directory is not None
        return (self.directory / f"{key}.json",
                self.directory / f"{key}.lock")

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        hit = self.memory.get(key)
        if hit is not None:
            return hit
        if self.directory is None:
            return None
        data_path, _ = self._paths(key)
        if not data_path.exists():
            return None
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if _now() - payload.get("_cached_at", 0.0) > self.ttl:
            with contextlib.suppress(OSError):
                data_path.unlink()
            return None
        self.memory.put(key, payload)
        return payload

    def put(self, key: str, value: Dict[str, Any]) -> None:
        payload = dict(value)
        payload["_cached_at"] = _now()
        self.memory.put(key, payload)
        if self.directory is None:
            return
        data_path, lock_path = self._paths(key)
        try:
            with open(lock_path, "w") as lock_fp:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fp, fcntl.LOCK_EX)
                tmp = data_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, default=str),
                               encoding="utf-8")
                os.replace(tmp, data_path)
        except OSError:
            pass


class RedisCache:
    def __init__(self, url: str = "redis://127.0.0.1:6379/0",
                 prefix: str = "astcie:quality:", ttl_seconds: float = 3600.0,
                 socket_timeout: float = 2.0, namespace: str = "v1") -> None:
        self.prefix = prefix + namespace + ":"
        self.ttl = int(ttl_seconds)
        self._client: Optional[Any] = None
        self._disabled = _redis_mod is None
        if not self._disabled:
            try:
                self._client = _redis_mod.Redis.from_url(
                    url, socket_timeout=socket_timeout,
                    socket_connect_timeout=socket_timeout,
                    decode_responses=True,
                )
                self._client.ping()
                logger.info("redis cache connected: %s", url)
            except Exception as exc:
                logger.warning("redis unavailable: %s", exc)
                self._client = None
                self._disabled = True

    @property
    def enabled(self) -> bool:
        return not self._disabled and self._client is not None

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            raw = self._client.get(self.prefix + key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def put(self, key: str, value: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = dict(value)
        payload["_cached_at"] = _now()
        try:
            self._client.setex(self.prefix + key, self.ttl,
                               json.dumps(payload, default=str))
        except Exception:
            pass

    def clear_namespace(self) -> int:
        if not self.enabled:
            return 0
        n = 0
        try:
            for k in self._client.scan_iter(match=self.prefix + "*", count=500):
                self._client.delete(k)
                n += 1
        except Exception:
            pass
        return n


class LayeredCache:
    def __init__(self, l1: _LRU, l2: Optional[RedisCache] = None,
                 l3: Optional[ResultCache] = None) -> None:
        self.l1 = l1
        self.l2 = l2
        self.l3 = l3

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        v = self.l1.get(key)
        if v is not None: return v
        if self.l2 is not None:
            v = self.l2.get(key)
            if v is not None:
                self.l1.put(key, v); return v
        if self.l3 is not None:
            v = self.l3.get(key)
            if v is not None:
                self.l1.put(key, v)
                if self.l2 is not None: self.l2.put(key, v)
                return v
        return None

    def put(self, key: str, value: Dict[str, Any]) -> None:
        self.l1.put(key, value)
        if self.l2 is not None: self.l2.put(key, value)
        if self.l3 is not None: self.l3.put(key, value)


# =============================================================================
# File lock
# =============================================================================

class FileLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fp = None

    def __enter__(self) -> "FileLock":
        if platform.system() == "Windows":
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.path, "w")
        deadline = _now() + self.timeout
        while True:
            try:
                fcntl.flock(self._fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if _now() > deadline:
                    self._fp.close(); self._fp = None
                    raise QualityError(f"could not acquire lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        if self._fp is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fp, fcntl.LOCK_UN)
            self._fp.close(); self._fp = None


# =============================================================================
# Input guard
# =============================================================================

class InputGuard:
    def __init__(self, allowed_roots: Optional[Sequence[Path]] = None,
                 max_bytes: int = MAX_INPUT_BYTES,
                 follow_symlinks: bool = False) -> None:
        self.allowed_roots = [Path(p).resolve() for p in (allowed_roots or [])]
        self.max_bytes = max_bytes
        self.follow_symlinks = follow_symlinks

    def validate(self, path: Path) -> Path:
        if path is None:
            raise QualityInputError("no path")
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not self.follow_symlinks and p.is_symlink():
            raise QualityInputError(f"symlink not allowed: {p}")
        st = _safe_stat(p)
        mode = st.st_mode
        if stat.S_ISDIR(mode):
            raise QualityInputError(f"is a directory: {p}")
        if not stat.S_ISREG(mode):
            raise QualityInputError(f"not a regular file: {p}")
        if st.st_size <= 0:
            raise QualityInputError(f"empty file: {p}")
        if st.st_size > self.max_bytes:
            raise QualityInputError(f"file too large: {st.st_size}")
        if self.allowed_roots and not any(_is_subpath(p, r) for r in self.allowed_roots):
            raise QualityInputError(f"path outside allowed roots: {p}")
        return p


# =============================================================================
# Sandbox runner
# =============================================================================

@dataclass
class SandboxLimits:
    cpu_seconds: int = 120
    address_space_bytes: int = 4 * 1024 * 1024 * 1024
    file_size_bytes: int = 2 * 1024 * 1024 * 1024
    open_files: int = 1024
    core_size_bytes: int = 0
    nproc: Optional[int] = None


def _apply_limits(limits: SandboxLimits) -> None:
    if resource is None:
        return
    def _set(res: int, value: int) -> None:
        with contextlib.suppress(Exception):
            soft, hard = resource.getrlimit(res)
            new_soft = value if soft in (resource.RLIM_INFINITY,) else min(soft, value)
            new_hard = hard if hard in (resource.RLIM_INFINITY,) else min(hard, value)
            resource.setrlimit(res, (new_soft, new_hard))
    _set(resource.RLIMIT_CPU, limits.cpu_seconds)
    _set(resource.RLIMIT_AS, limits.address_space_bytes)
    _set(resource.RLIMIT_FSIZE, limits.file_size_bytes)
    _set(resource.RLIMIT_NOFILE, limits.open_files)
    _set(resource.RLIMIT_CORE, limits.core_size_bytes)
    if limits.nproc is not None and hasattr(resource, "RLIMIT_NPROC"):
        _set(resource.RLIMIT_NPROC, limits.nproc)


class SandboxRunner:
    def __init__(self, limits: Optional[SandboxLimits] = None,
                 env_overrides: Optional[Dict[str, str]] = None,
                 inherit_env: bool = False) -> None:
        self.limits = limits or SandboxLimits()
        self.env_overrides = dict(env_overrides or {})
        self.inherit_env = inherit_env

    def _env(self) -> Dict[str, str]:
        base: Dict[str, str] = {}
        if self.inherit_env:
            base.update(os.environ)
        base.setdefault("LC_ALL", "C")
        base.setdefault("LANG", "C")
        base.setdefault("AV_LOG_FORCE_NOCOLOR", "1")
        base.update(self.env_overrides)
        return base

    def run(self, argv: Sequence[str], timeout: float, *,
            cwd: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess:
        preexec = None
        if platform.system() != "Windows":
            preexec = functools.partial(_apply_limits, self.limits)
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout,
                shell=False, check=False, cwd=str(cwd) if cwd else None,
                env=self._env(), preexec_fn=preexec, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise QualityTimeout(f"timeout after {timeout}s: {' '.join(argv[:3])}...") from exc
        if check and proc.returncode != 0:
            raise QualityBackendError(f"rc={proc.returncode} for {' '.join(argv[:3])}...")
        return proc


# =============================================================================
# FFmpeg capabilities
# =============================================================================

@dataclass
class BackendCapabilities:
    ffmpeg: str = "ffmpeg"
    ffprobe: Optional[str] = None
    version: str = ""
    has_libvmaf: bool = False
    has_cambi: bool = False
    has_ms_ssim: bool = False
    has_psnr_hvs: bool = False
    has_loudnorm: bool = False
    has_gpu: bool = False
    filters: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["filters"] = list(self.filters)
        return d


_CAP_CACHE: Optional[BackendCapabilities] = None
_CAP_LOCK = threading.Lock()


def detect_capabilities(ffmpeg: str = "ffmpeg",
                        ffprobe: str = "ffprobe") -> BackendCapabilities:
    global _CAP_CACHE
    with _CAP_LOCK:
        if _CAP_CACHE is not None:
            return _CAP_CACHE
        ffmpeg_path = shutil.which(ffmpeg) or ffmpeg
        ffprobe_path = shutil.which(ffprobe)
        version = ""
        filters: Tuple[str, ...] = ()
        try:
            rc, out, err = _run_capture([ffmpeg_path, "-hide_banner", "-version"], 10.0)
            if rc == 0 and out:
                version = out.splitlines()[0]
        except Exception:
            pass
        try:
            rc, out, err = _run_capture(
                [ffmpeg_path, "-hide_banner", "-filters"], 15.0)
            if rc == 0:
                names: List[str] = []
                for line in (out + "\n" + err).splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and not parts[0].startswith("-"):
                        names.append(parts[1])
                filters = tuple(sorted(set(names)))
        except Exception:
            pass
        flt = set(filters)
        cap = BackendCapabilities(
            ffmpeg=ffmpeg_path, ffprobe=ffprobe_path, version=version,
            has_libvmaf="libvmaf" in flt,
            has_cambi="cambi" in flt,
            has_ms_ssim="ms_ssim" in flt,
            has_psnr_hvs="psnr_hvs" in flt,
            has_loudnorm="loudnorm" in flt,
            has_gpu=any("cuda" in n or "opencl" in n or "vaapi" in n for n in flt),
            filters=filters,
        )
        _CAP_CACHE = cap
        return cap


def reset_capabilities_cache() -> None:
    global _CAP_CACHE
    with _CAP_LOCK:
        _CAP_CACHE = None


# =============================================================================
# Hardware acceleration
# =============================================================================

class HwBackend(enum.Enum):
    CPU = "cpu"
    CUDA = "cuda"
    VAAPI = "vaapi"
    QSV = "qsv"
    VIDEOTOOLBOX = "videotoolbox"
    D3D11VA = "d3d11va"
    OPENCL = "opencl"


@dataclass
class HwCapabilities:
    backend: HwBackend = HwBackend.CPU
    device: Optional[str] = None
    upload_filter: Optional[str] = None
    download_filter: Optional[str] = None
    hwaccel: Optional[str] = None
    supports_vmaf_gpu: bool = False
    device_name: Optional[str] = None

    def describe(self) -> str:
        return (f"{self.backend.value}"
                + (f" ({self.device_name})" if self.device_name else ""))


_HW_CACHE: Optional[HwCapabilities] = None
_HW_LOCK = threading.Lock()


def _hw_probe_cuda() -> Optional[HwCapabilities]:
    text = _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[1] \
        + _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[2]
    if "cuda" not in text:
        return None
    name = None
    if _which("nvidia-smi"):
        try:
            p = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, shell=False,
            )
            first = (p.stdout or "").strip().splitlines()
            if first:
                name = first[0]
        except Exception:
            pass
    return HwCapabilities(
        backend=HwBackend.CUDA, device="0",
        upload_filter="hwupload_cuda",
        download_filter="hwdownload,format=yuv420p",
        hwaccel="cuda", supports_vmaf_gpu=True, device_name=name,
    )


def _hw_probe_vaapi() -> Optional[HwCapabilities]:
    text = _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[1] + \
           _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[2]
    if "vaapi" not in text:
        return None
    device = "/dev/dri/renderD128"
    if not os.path.exists(device):
        return None
    return HwCapabilities(
        backend=HwBackend.VAAPI, device=device,
        upload_filter="format=nv12,hwupload",
        download_filter="hwdownload,format=nv12",
        hwaccel="vaapi", supports_vmaf_gpu=False, device_name=device,
    )


def _hw_probe_qsv() -> Optional[HwCapabilities]:
    text = _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[1] + \
           _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[2]
    if "qsv" not in text:
        return None
    return HwCapabilities(
        backend=HwBackend.QSV,
        upload_filter="format=nv12,hwupload=extra_hw_frames=64",
        download_filter="hwdownload,format=nv12",
        hwaccel="qsv", device_name="qsv",
    )


def _hw_probe_videotoolbox() -> Optional[HwCapabilities]:
    text = _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[1] + \
           _run_capture(["ffmpeg", "-hide_banner", "-hwaccels"], 8.0)[2]
    if "videotoolbox" not in text:
        return None
    return HwCapabilities(backend=HwBackend.VIDEOTOOLBOX,
                          hwaccel="videotoolbox", device_name="videotoolbox")


def detect_hw(prefer: Optional[Sequence[HwBackend]] = None) -> HwCapabilities:
    global _HW_CACHE
    with _HW_LOCK:
        if _HW_CACHE is not None:
            return _HW_CACHE
        order = list(prefer) if prefer else [
            HwBackend.CUDA, HwBackend.QSV, HwBackend.VAAPI, HwBackend.VIDEOTOOLBOX,
        ]
        probes = {
            HwBackend.CUDA: _hw_probe_cuda,
            HwBackend.QSV: _hw_probe_qsv,
            HwBackend.VAAPI: _hw_probe_vaapi,
            HwBackend.VIDEOTOOLBOX: _hw_probe_videotoolbox,
        }
        for backend in order:
            probe = probes.get(backend)
            if probe is None:
                continue
            try:
                cap = probe()
            except Exception as exc:
                logger.debug("hw probe %s failed: %s", backend, exc)
                continue
            if cap is not None:
                _HW_CACHE = cap
                logger.info("hwaccel: %s", cap.describe())
                return cap
        _HW_CACHE = HwCapabilities()
        logger.info("hwaccel: CPU only")
        return _HW_CACHE


def reset_hw_cache() -> None:
    global _HW_CACHE
    with _HW_LOCK:
        _HW_CACHE = None


def gpu_decode_args(cap: HwCapabilities) -> List[str]:
    if cap.hwaccel is None:
        return []
    args = ["-hwaccel", cap.hwaccel]
    if cap.device:
        args += ["-hwaccel_device", cap.device]
    args += ["-hwaccel_output_format"]
    if cap.backend is HwBackend.CUDA:
        args.append("cuda")
    elif cap.backend is HwBackend.QSV:
        args.append("qsv")
    elif cap.backend is HwBackend.VAAPI:
        args.append("vaapi")
    else:
        args.append("nv12")
    return args


# =============================================================================
# ffprobe
# =============================================================================

def probe_input(path: Path, *, ffprobe: str = "ffprobe",
                timeout: float = DEFAULT_FFPROBE_TIMEOUT,
                runner: Optional[SandboxRunner] = None) -> ProbeInfo:
    runner = runner or SandboxRunner()
    argv = [ffprobe, "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams",
            str(path)]
    proc = runner.run(argv, timeout=timeout)
    if proc.returncode != 0:
        raise QualityBackendError(f"ffprobe failed: {proc.stderr.strip()[:200]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise QualityBackendError(f"ffprobe json: {exc}") from exc
    fmt = data.get("format", {}) or {}
    duration = _safe_float(fmt.get("duration"))
    size = int(fmt.get("size") or 0)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps: Optional[float] = None
    if v:
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/0"
        try:
            num, den = rate.split("/")
            n, d = float(num), float(den)
            if d > 0:
                fps = n / d
        except Exception:
            fps = None
    trc = ((v or {}).get("color_transfer") or "").lower()
    is_hdr = trc in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12"}
    return ProbeInfo(
        path=str(path), size_bytes=size, duration_s=duration,
        width=(v or {}).get("width"), height=(v or {}).get("height"),
        fps=fps, vcodec=(v or {}).get("codec_name"),
        acodec=(a or {}).get("codec_name"), pix_fmt=(v or {}).get("pix_fmt"),
        color_transfer=(v or {}).get("color_transfer"),
        color_primaries=(v or {}).get("color_primaries"),
        color_space=(v or {}).get("color_space"),
        is_hdr=is_hdr, raw=data,
    )


# =============================================================================
# Scene detection
# =============================================================================

@dataclass
class Shot:
    start_s: float
    end_s: float
    duration_s: float
    motion_hint: float = 0.0
    weight: float = 1.0

    def mid(self) -> float:
        return 0.5 * (self.start_s + self.end_s)


@dataclass
class SceneMap:
    shots: List[Shot] = field(default_factory=list)
    total_duration_s: float = 0.0

    def __len__(self) -> int:
        return len(self.shots)

    def weighted_frames(self, budget: int) -> List[Shot]:
        if not self.shots:
            return []
        n = len(self.shots)
        if budget <= n:
            return sorted(self.shots, key=lambda s: -s.duration_s)[:budget]
        total = sum(s.duration_s for s in self.shots) or 1.0
        out: List[Shot] = []
        for s in self.shots:
            k = max(1, round(budget * s.duration_s / total))
            s.weight = k
            out.extend([s] * k)
        return out[:budget]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_duration_s": self.total_duration_s,
            "shots": [{"start": s.start_s, "end": s.end_s,
                       "duration": s.duration_s, "motion": s.motion_hint}
                      for s in self.shots],
        }


def _ffprobe_duration(path: Path, ffmpeg: str) -> float:
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    argv = [ffprobe, "-hide_banner", "-loglevel", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=20, shell=False)
        return float((p.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _cuts_to_shots(cuts: Sequence[Tuple[float, float]],
                   total: float) -> List[Shot]:
    if not cuts:
        if total <= 0:
            return []
        return [Shot(0.0, total, total)]
    times = sorted({max(0.0, t) for t, _ in cuts})
    scores = {t: s for t, s in cuts}
    bounds = [0.0] + times + ([total] if total > 0 else [times[-1] + 1.0])
    shots: List[Shot] = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b <= a:
            continue
        shots.append(Shot(a, b, b - a, motion_hint=scores.get(a, 0.0)))
    return shots


def detect_scenes(path: Path, *, ffmpeg: str = "ffmpeg",
                  threshold: float = 0.35, downscale_width: int = 160,
                  timeout: float = DEFAULT_SCENE_TIMEOUT) -> SceneMap:
    vf = (f"scale={downscale_width}:-2,"
          f"select='gt(scene\\,{threshold})',showinfo")
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
            "-i", str(path), "-vf", vf, "-an", "-sn", "-dn", "-f", "null", "-"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        logger.warning("scene detect timeout on %s", path)
        return SceneMap()
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    cuts: List[Tuple[float, float]] = []
    for line in text.splitlines():
        if "pts_time:" not in line:
            continue
        m = _RE_SCENE_PTS.search(line)
        if not m:
            continue
        score = 0.5
        sm = _RE_SCENE_SCORE.search(line)
        if sm:
            score = _safe_float(sm.group(1)) or 0.5
        cuts.append((float(m.group(1)), score))
    total = _ffprobe_duration(path, ffmpeg)
    return SceneMap(shots=_cuts_to_shots(cuts, total), total_duration_s=total)


def scene_aware_sample_points(scene_map: SceneMap, *, budget: int,
                              min_shot_s: float = 0.5) -> List[float]:
    shots = [s for s in scene_map.shots if s.duration_s >= min_shot_s] or scene_map.shots
    if not shots:
        return []
    picked = scene_map.weighted_frames(budget)
    return [s.mid() for s in picked]


# =============================================================================
# Ladder builder
# =============================================================================

@dataclass
class LadderPoint:
    width: int
    height: int
    fps: float
    bitrate_kbps: float
    quality: float
    codec: str = "h264"
    label: str = ""


@dataclass
class Ladder:
    points: List[LadderPoint] = field(default_factory=list)
    frontier: List[LadderPoint] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "points": [p.__dict__ for p in self.points],
            "frontier": [p.__dict__ for p in self.frontier],
        }


def _dominates(a: LadderPoint, b: LadderPoint) -> bool:
    return (a.bitrate_kbps <= b.bitrate_kbps and a.quality >= b.quality
            and (a.bitrate_kbps < b.bitrate_kbps or a.quality > b.quality))


def pareto_frontier(points: Sequence[LadderPoint]) -> List[LadderPoint]:
    ordered = sorted(points, key=lambda p: (p.bitrate_kbps, -p.quality))
    frontier: List[LadderPoint] = []
    for p in ordered:
        if any(_dominates(q, p) for q in frontier):
            continue
        frontier = [q for q in frontier if not _dominates(p, q)]
        frontier.append(p)
    frontier.sort(key=lambda p: p.bitrate_kbps)
    return frontier


def convex_hull_upper(points: Sequence[LadderPoint]) -> List[LadderPoint]:
    if len(points) <= 2:
        return list(points)
    pts = sorted(points, key=lambda p: p.bitrate_kbps)
    xs = [math.log(max(p.bitrate_kbps, 1.0)) for p in pts]
    ys = [p.quality for p in pts]

    def cross(o: int, a: int, b: int) -> float:
        return ((xs[a] - xs[o]) * (ys[b] - ys[o])
                - (ys[a] - ys[o]) * (xs[b] - xs[o]))

    hull: List[int] = []
    for i in range(len(pts)):
        while len(hull) >= 2 and cross(hull[-2], hull[-1], i) >= 0:
            hull.pop()
        hull.append(i)
    return [pts[i] for i in hull]


def build_ladder(points: Iterable[LadderPoint], *,
                 use_convex_hull: bool = True,
                 min_gap_kbps: float = 100.0) -> Ladder:
    pts = list(points)
    frontier = pareto_frontier(pts)
    if use_convex_hull:
        frontier = convex_hull_upper(frontier)
    pruned: List[LadderPoint] = []
    for p in frontier:
        if pruned and p.bitrate_kbps - pruned[-1].bitrate_kbps < min_gap_kbps:
            if p.quality > pruned[-1].quality:
                pruned[-1] = p
            continue
        pruned.append(p)
    return Ladder(points=pts, frontier=pruned)


def suggest_rungs(title_stats: Mapping[str, float],
                  base_ladder: Sequence[LadderPoint]) -> List[LadderPoint]:
    complexity = float(title_stats.get("complexity", 0.5))
    grain = float(title_stats.get("grain", 0.0))
    factor = 0.75 + 0.5 * complexity + 0.15 * grain
    return [LadderPoint(
        width=p.width, height=p.height, fps=p.fps,
        bitrate_kbps=max(50.0, p.bitrate_kbps * factor),
        quality=p.quality, codec=p.codec, label=p.label,
    ) for p in base_ladder]


# =============================================================================
# Observability
# =============================================================================

class MetricsSink(Protocol):
    def incr(self, name: str, value: float = 1.0,
             labels: Optional[Dict[str, str]] = None) -> None: ...
    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None: ...


class NullSink:
    def incr(self, *a, **kw) -> None: ...
    def observe(self, *a, **kw) -> None: ...


class LoggingSink:
    def __init__(self, logger_: Optional[logging.Logger] = None) -> None:
        self.log = logger_ or logger
    def incr(self, name: str, value: float = 1.0,
             labels: Optional[Dict[str, str]] = None) -> None:
        self.log.debug("metric.incr %s=%s %s", name, value, labels or {})
    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        self.log.debug("metric.obs %s=%s %s", name, value, labels or {})


class PrometheusSink:
    def __init__(self, namespace: str = "astcie_quality") -> None:
        if _prom is None:
            raise RuntimeError("prometheus_client not installed")
        self.namespace = namespace
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], Any] = {}
        self._histos: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _san(name: str) -> str:
        return "".join(c if c.isalnum() or c == "_" else "_" for c in name)

    def _label_key(self, labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((labels or {}).items()))

    def incr(self, name: str, value: float = 1.0,
             labels: Optional[Dict[str, str]] = None) -> None:
        key = (self._san(name), self._label_key(labels))
        with self._lock:
            c = self._counters.get(key)
            if c is None:
                try:
                    c = _prom.Counter(
                        f"{self.namespace}_{key[0]}_total", "auto",
                        labelnames=[k for k, _ in key[1]],
                    )
                except ValueError:
                    c = None
                self._counters[key] = c
            if c is None:
                return
        (c.labels(**labels) if labels else c).inc(value)

    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        key = (self._san(name), self._label_key(labels))
        with self._lock:
            h = self._histos.get(key)
            if h is None:
                try:
                    h = _prom.Histogram(
                        f"{self.namespace}_{key[0]}", "auto",
                        labelnames=[k for k, _ in key[1]],
                    )
                except ValueError:
                    h = None
                self._histos[key] = h
            if h is None:
                return
        (h.labels(**labels) if labels else h).observe(value)


class StatsdSink:
    def __init__(self, host: str = "127.0.0.1", port: int = 8125,
                 prefix: str = "astcie.quality") -> None:
        self.addr = (host, port)
        self.prefix = prefix
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _emit(self, line: str) -> None:
        try:
            self.sock.sendto(line.encode("ascii", "replace"), self.addr)
        except Exception:
            pass

    @staticmethod
    def _tag(labels: Optional[Dict[str, str]]) -> str:
        if not labels:
            return ""
        return "|#" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def incr(self, name: str, value: float = 1.0,
             labels: Optional[Dict[str, str]] = None) -> None:
        self._emit(f"{self.prefix}.{name}:{value}|c{self._tag(labels)}")

    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        self._emit(f"{self.prefix}.{name}:{value}|ms{self._tag(labels)}")


class FanoutSink:
    def __init__(self, sinks: List[Any]) -> None:
        self.sinks = sinks

    def incr(self, name: str, value: float = 1.0,
             labels: Optional[Dict[str, str]] = None) -> None:
        for s in self.sinks:
            with contextlib.suppress(Exception):
                s.incr(name, value, labels)

    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        for s in self.sinks:
            with contextlib.suppress(Exception):
                s.observe(name, value, labels)


class _NullSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return None
    def set_attribute(self, *a, **kw): return None
    def record_exception(self, *a, **kw): return None


class OTelTracer:
    def __init__(self, name: str = "astcie.quality") -> None:
        self.enabled = _otel_trace is not None
        self._tracer = _otel_trace.get_tracer(name) if self.enabled else None

    def span(self, name: str, **attrs):
        if not self.enabled:
            return _NullSpan()
        return _OTelSpan(self._tracer, name, attrs)


class _OTelSpan:
    def __init__(self, tracer, name: str, attrs: dict) -> None:
        self.tracer = tracer
        self.name = name
        self.attrs = attrs
        self._cm = None
        self._span = None

    def __enter__(self):
        self._cm = self.tracer.start_as_current_span(self.name)
        self._span = self._cm.__enter__()
        for k, v in self.attrs.items():
            with contextlib.suppress(Exception):
                self._span.set_attribute(k, v)
        return self

    def __exit__(self, et, e, tb):
        if e is not None and self._span is not None and _OtelStatus is not None:
            with contextlib.suppress(Exception):
                self._span.record_exception(e)
                self._span.set_status(_OtelStatus(_OtelStatusCode.ERROR, str(e)))
        if self._cm is not None:
            self._cm.__exit__(et, e, tb)

    def set_attribute(self, k, v):
        if self._span is not None:
            with contextlib.suppress(Exception):
                self._span.set_attribute(k, v)

    def record_exception(self, e):
        if self._span is not None:
            with contextlib.suppress(Exception):
                self._span.record_exception(e)


def make_sink(*, prometheus: bool = True, statsd: Optional[str] = None,
              log_fallback: bool = True) -> FanoutSink:
    sinks: List[Any] = []
    if prometheus and _prom is not None and os.environ.get("ASTCIE_PROM_DISABLE") != "1":
        try:
            sinks.append(PrometheusSink())
        except Exception as exc:
            logger.debug("prometheus sink unavailable: %s", exc)
    host = os.environ.get("ASTCIE_STATSD_HOST")
    if host:
        port = int(os.environ.get("ASTCIE_STATSD_PORT", "8125"))
        with contextlib.suppress(Exception):
            sinks.append(StatsdSink(host, port))
    if statsd:
        with contextlib.suppress(Exception):
            h, _, p = statsd.partition(":")
            sinks.append(StatsdSink(h, int(p or "8125")))
    if not sinks and log_fallback:
        sinks.append(LoggingSink())
    return FanoutSink(sinks)


# =============================================================================
# Scorers
# =============================================================================

class Scorer(Protocol):
    def score(self, sample: QualitySample) -> Tuple[float, float]: ...


class WeightedScorer:
    def __init__(self, weights: Optional[QualityWeights] = None) -> None:
        self.weights = weights or QualityWeights()

    @staticmethod
    def _norm_vmaf(v): return None if v is None else _clamp(v / VMAF_REFERENCE_MAX)
    @staticmethod
    def _norm_ssim(v): return None if v is None else _clamp(v)
    @staticmethod
    def _norm_psnr(v):
        return None if v is None else _clamp(
            (v - PSNR_MIN_DB) / (PSNR_MAX_DB - PSNR_MIN_DB))
    @staticmethod
    def _norm_cambi(v):
        return None if v is None else _clamp(1.0 - (v / 2.5))

    def _quality_components(self, s: QualitySample) -> List[Tuple[float, float]]:
        w = self.weights
        out: List[Tuple[float, float]] = []
        pairs = [
            ("vmaf", s.vmaf, w.vmaf, self._norm_vmaf),
            ("ssim", s.ssim, w.ssim, self._norm_ssim),
            ("ms_ssim", s.ms_ssim, w.ms_ssim, self._norm_ssim),
            ("psnr", s.psnr, w.psnr, self._norm_psnr),
            ("cambi", s.cambi, w.cambi, self._norm_cambi),
            ("noref", s.noref, w.noref, lambda v: _clamp(v)),
        ]
        for _name, raw, weight, norm in pairs:
            if raw is None or weight <= 0:
                continue
            val = norm(raw)
            if val is None:
                continue
            out.append((weight, val))
        return out

    def _operational_components(self, s: QualitySample) -> List[Tuple[float, float]]:
        w = self.weights
        out: List[Tuple[float, float]] = []
        if (s.actual_bitrate_mbps is not None
                and s.target_bitrate_mbps and s.target_bitrate_mbps > 0):
            ratio = s.actual_bitrate_mbps / s.target_bitrate_mbps
            out.append((w.bitrate_fit, _clamp(1.0 - abs(1.0 - ratio))))
        drop_pen = _clamp(s.frame_drops / DROP_PENALTY_FULL)
        out.append((w.stability, 1.0 - drop_pen))
        return out

    def score(self, sample: QualitySample) -> Tuple[float, float]:
        quality = self._quality_components(sample)
        operational = self._operational_components(sample)
        if not quality and not operational:
            return self.weights.neutral, 0.0
        all_parts = quality + operational
        wsum = sum(w for w, _ in all_parts)
        if wsum <= 0:
            return self.weights.neutral, 0.0
        hybrid = sum(w * v for w, v in all_parts) / wsum

        qmass = sum(w for w, _ in quality)
        qconf = _clamp(qmass / max(self.weights.min_quality_weight, 1e-6))
        agreement = 1.0
        if len(quality) >= 2:
            vals = [v for _, v in quality]
            mean = sum(vals) / len(vals)
            std = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
            agreement = _clamp(1.0 - std / 0.30)
        confidence = _clamp(0.6 * qconf + 0.4 * agreement)
        if confidence < 0.5:
            pull = 0.5 - confidence
            hybrid = hybrid * (1 - pull) + self.weights.neutral * pull
        if not sample.ok:
            confidence *= 0.5
        return _clamp(hybrid), confidence


class StrictScorer(WeightedScorer):
    def score(self, sample: QualitySample) -> Tuple[float, float]:
        if sample.vmaf is None and sample.ssim is None:
            return self.weights.neutral, 0.0
        return super().score(sample)


class EnsembleScorer:
    """Simple model averaging of several scorers."""
    def __init__(self, scorers: Sequence[Scorer],
                 weights: Optional[Sequence[float]] = None) -> None:
        if not scorers:
            raise ValueError("need at least one scorer")
        self.scorers = list(scorers)
        self.weights = list(weights) if weights else [1.0] * len(scorers)
        if len(self.weights) != len(self.scorers):
            raise ValueError("weights length mismatch")

    def score(self, sample: QualitySample) -> Tuple[float, float]:
        total_w = 0.0
        acc_h = 0.0
        acc_c = 0.0
        for s, w in zip(self.scorers, self.weights):
            try:
                h, c = s.score(sample)
            except Exception:
                continue
            acc_h += h * w
            acc_c += c * w
            total_w += w
        if total_w <= 0:
            return 0.5, 0.0
        return _clamp(acc_h / total_w), _clamp(acc_c / total_w)


class BayesianScorer:
    """A tiny Bayesian scorer that treats each metric as a noisy observation.

    We keep Beta prior parameters per metric and update them per sample using
    the normalised observation as a pseudo-count. This is deliberately simple:
    the point is to make scores shrink toward the prior when evidence is thin.
    """
    def __init__(self, weights: Optional[QualityWeights] = None,
                 prior_strength: float = 5.0) -> None:
        self.weights = weights or QualityWeights()
        self.prior_strength = prior_strength
        self._base = WeightedScorer(self.weights)

    def score(self, sample: QualitySample) -> Tuple[float, float]:
        h, c = self._base.score(sample)
        # Shrink toward prior when confidence is low.
        prior = self.weights.neutral
        alpha = self.prior_strength
        beta = self.prior_strength
        # Treat observations as (alpha + n*h) / (alpha + beta + n)
        n = max(0.0, c * 10.0)
        post = (alpha + n * h) / (alpha + beta + n)
        return _clamp(post), c


# =============================================================================
# MOS / DMOS mapping
# =============================================================================

class MosMapper:
    """Map a hybrid score [0,1] to a MOS-like scale (default 1..5)."""

    def __init__(self, lo: float = 1.0, hi: float = 5.0) -> None:
        self.lo = lo
        self.hi = hi

    def linear(self, hybrid: float) -> float:
        return self.lo + _clamp(hybrid) * (self.hi - self.lo)

    def logistic(self, hybrid: float, steepness: float = 8.0,
                 midpoint: float = 0.5) -> float:
        x = _clamp(hybrid)
        # logistic centered at `midpoint`
        s = 1.0 / (1.0 + math.exp(-steepness * (x - midpoint)))
        # renormalize to [lo, hi]
        s_lo = 1.0 / (1.0 + math.exp(-steepness * (0 - midpoint)))
        s_hi = 1.0 / (1.0 + math.exp(-steepness * (1 - midpoint)))
        return self.lo + (s - s_lo) / (s_hi - s_lo) * (self.hi - self.lo)

    def vmaf_to_mos(self, vmaf: float) -> float:
        # Piecewise from published VMAF→MOS curves (approx)
        if vmaf >= 90: return 4.7 + (vmaf - 90) / 10.0 * 0.3
        if vmaf >= 80: return 4.0 + (vmaf - 80) / 10.0 * 0.7
        if vmaf >= 60: return 3.0 + (vmaf - 60) / 20.0 * 1.0
        if vmaf >= 40: return 2.0 + (vmaf - 40) / 20.0 * 1.0
        if vmaf >= 20: return 1.0 + (vmaf - 20) / 20.0 * 1.0
        return 1.0


# =============================================================================
# HDR helpers
# =============================================================================

@dataclass
class HdrInfo:
    is_hdr: bool = False
    transfer: str = ""
    primaries: str = ""
    matrix: str = ""
    max_nits: float = 100.0
    min_nits: float = 0.0
    reference_white_nits: float = 100.0

    @staticmethod
    def detect(probe: ProbeInfo) -> "HdrInfo":
        return HdrInfo(
            is_hdr=probe.is_hdr,
            transfer=(probe.color_transfer or "").lower(),
            primaries=(probe.color_primaries or "").lower(),
            matrix=(probe.color_space or "").lower(),
            max_nits=1000.0 if probe.is_hdr else 100.0,
        )


def hdr_aware_vmaf_filter(width: int, fps: float,
                          hdr: HdrInfo, *, tone_map: bool = True) -> str:
    """Build a scale chain that tone maps HDR to SDR for VMAF.

    libvmaf expects SDR reference in the classical recipe. For HDR content,
    we apply zscale tone mapping and normalise to 100 nits.
    """
    chain = [f"scale={width}:-2:flags=bicubic", f"fps={fps}", "setsar=1"]
    if hdr.is_hdr and tone_map:
        # zscale is available in most modern ffmpeg builds; fall back silently
        chain.insert(0, (
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
        ))
    return ",".join(chain)


# =============================================================================
# Reference-free metrics
# =============================================================================

class _FrameExtractor:
    """Extract raw luma frames as small grayscale arrays for analysis."""

    def __init__(self, ffmpeg: str = "ffmpeg", runner: Optional[SandboxRunner] = None):
        self.ffmpeg = ffmpeg
        self.runner = runner or SandboxRunner()

    def extract(self, path: Path, *, width: int = 128, height: int = 72,
                fps: float = 1.0, count: int = 3,
                timeout: float = 30.0) -> List[bytes]:
        if _np is None:
            return []
        argv = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(path),
            "-vf", f"scale={width}:{height},fps={fps}",
            "-frames:v", str(count),
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ]
        try:
            proc = self.runner.run(argv, timeout=timeout)
        except Exception:
            return []
        if proc.returncode != 0:
            return []
        # Captured stdout is text (str) but we need bytes; re-run without text
        argv = list(argv)
        try:
            p = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False)
        except Exception:
            return []
        if p.returncode != 0:
            return []
        frame_size = width * height
        data = p.stdout or b""
        return [data[i:i + frame_size]
                for i in range(0, len(data) - frame_size + 1, frame_size)]


class LightNoref:
    """A very light no-reference metric based on local variance and edge
    energy. Not a full BRISQUE/NIQE, but a monotone proxy for sharpness +
    blockiness suitable for a feedback loop.
    """

    def __init__(self, extractor: Optional[_FrameExtractor] = None) -> None:
        self.extractor = extractor or _FrameExtractor()

    def score(self, path: Path) -> Optional[float]:
        if _np is None:
            return None
        frames = self.extractor.extract(path)
        if not frames:
            return None
        scores: List[float] = []
        for f in frames:
            arr = _np.frombuffer(f, dtype=_np.uint8)
            if arr.size == 0:
                continue
            h = w = int(math.sqrt(arr.size))
            if h * w != arr.size:
                # Non-square; take the largest square
                side = int(math.sqrt(arr.size))
                arr = arr[: side * side]
                h = w = side
            img = arr.reshape(h, w).astype(_np.float32)
            gx = _np.diff(img, axis=1)
            gy = _np.diff(img, axis=0)
            ex = float(_np.mean(_np.abs(gx)))
            ey = float(_np.mean(_np.abs(gy)))
            edge = 0.5 * (ex + ey)
            # Blockiness: energy at 8-pixel boundaries
            block_x = _np.abs(_np.diff(img, axis=1)[:, 7::8]).mean() \
                if w > 8 else 0.0
            block_y = _np.abs(_np.diff(img, axis=0)[7::8, :]).mean() \
                if h > 8 else 0.0
            block = 0.5 * (block_x + block_y)
            # Sharpness up, blockiness down.
            s = _clamp((edge / 20.0) - (block / 30.0))
            scores.append(s)
        if not scores:
            return None
        return float(sum(scores) / len(scores))


# =============================================================================
# A/B testing
# =============================================================================

@dataclass
class ABResult:
    n_a: int
    n_b: int
    mean_a: float
    mean_b: float
    diff: float
    ci_low: float
    ci_high: float
    p_value: float
    significant: bool
    method: str = "bootstrap"

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class ABTest:
    def __init__(self, alpha: float = 0.05, n_boot: int = 2000,
                 seed: Optional[int] = None) -> None:
        self.alpha = alpha
        self.n_boot = n_boot
        self._rng = random.Random(seed)

    def _normal_cdf(self, z: float) -> float:
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def _welch(self, a: Sequence[float], b: Sequence[float]) -> float:
        na, nb = len(a), len(b)
        if na < 2 or nb < 2:
            return 1.0
        va = statistics.variance(a)
        vb = statistics.variance(b)
        se = math.sqrt(va / na + vb / nb) or 1e-9
        z = (statistics.fmean(a) - statistics.fmean(b)) / se
        return 2.0 * (1.0 - self._normal_cdf(abs(z)))

    def _bootstrap(self, a: Sequence[float], b: Sequence[float]
                   ) -> Tuple[float, float, float]:
        na, nb = len(a), len(b)
        if na == 0 or nb == 0:
            return 0.0, 0.0, 1.0
        diffs: List[float] = []
        rng = self._rng
        for _ in range(self.n_boot):
            sa = sum(rng.choice(a) for _ in range(na)) / na
            sb = sum(rng.choice(b) for _ in range(nb)) / nb
            diffs.append(sa - sb)
        diffs.sort()
        lo = diffs[int(0.025 * len(diffs))]
        hi = diffs[int(0.975 * len(diffs)) - 1]
        # p-value from the bootstrap distribution
        center = sum(diffs) / len(diffs)
        if center == 0:
            p = 1.0
        else:
            extreme = sum(1 for d in diffs if d * center <= 0) / len(diffs)
            p = min(1.0, 2.0 * extreme)
        return lo, hi, p

    def compare(self, a: Sequence[float], b: Sequence[float],
                method: str = "bootstrap") -> ABResult:
        if method == "welch":
            diff = (statistics.fmean(a) if a else 0.0) - \
                   (statistics.fmean(b) if b else 0.0)
            p = self._welch(a, b)
            return ABResult(len(a), len(b),
                            statistics.fmean(a) if a else 0.0,
                            statistics.fmean(b) if b else 0.0,
                            diff, float("nan"), float("nan"),
                            p, p < self.alpha, "welch")
        lo, hi, p = self._bootstrap(a, b)
        return ABResult(len(a), len(b),
                        statistics.fmean(a) if a else 0.0,
                        statistics.fmean(b) if b else 0.0,
                        (statistics.fmean(a) if a else 0.0) -
                        (statistics.fmean(b) if b else 0.0),
                        lo, hi, p, p < self.alpha, "bootstrap")


# =============================================================================
# Parsers
# =============================================================================

class MetricsParser:
    def parse_ssim_psnr(self, text: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        m = _RE_SSIM_ALL.search(text)
        if m:
            out["ssim"] = _safe_float(m.group(1)) or 0.0
        m = _RE_PSNR_AVG.search(text)
        if m:
            v = _safe_float(m.group(1))
            if v is not None:
                out["psnr"] = v
        else:
            m = _RE_PSNR_AVG_ALT.search(text)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    out["psnr"] = v
        m = _RE_MS_SSIM.search(text)
        if m:
            v = _safe_float(m.group(1))
            if v is not None:
                out["ms_ssim"] = v
        m = _RE_CAMBI.search(text)
        if m:
            v = _safe_float(m.group(1))
            if v is not None:
                out["cambi"] = v
        return out

    def parse_loudness(self, text: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        m = _RE_LOUDNESS_I.search(text)
        if m:
            v = _safe_float(m.group(1))
            if v is not None:
                out["loudness_lufs"] = v
        m = _RE_LOUDNESS_LRA.search(text)
        if m:
            v = _safe_float(m.group(1))
            if v is not None:
                out["loudness_lra"] = v
        return out

    def parse_vmaf_json(self, text: str) -> Optional[float]:
        try:
            data = json.loads(text)
        except Exception:
            return None
        pooled = data.get("pooled_metrics") or {}
        vmaf = pooled.get("vmaf") or {}
        if isinstance(vmaf, dict) and "mean" in vmaf:
            return _safe_float(vmaf.get("mean"))
        agg = data.get("aggregate") or {}
        if "vmaf" in agg:
            return _safe_float(agg.get("vmaf"))
        frames = data.get("frames") or []
        scores: List[float] = []
        for f in frames:
            fv = _safe_float((f.get("metrics") or {}).get("vmaf"))
            if fv is not None:
                scores.append(fv)
        return (sum(scores) / len(scores)) if scores else None


# =============================================================================
# Filtergraph builders
# =============================================================================

def _scale_chain(width: int, fps: float, *, force_sar: bool = True,
                 extra: Tuple[str, ...] = ()) -> str:
    parts = [f"scale={width}:-2:flags=bicubic", f"fps={fps}"]
    if force_sar:
        parts.append("setsar=1")
    parts.extend(extra)
    return ",".join(parts)


def build_ssim_psnr_filter(width: int, fps: float, *,
                           enable_ssim: bool = True,
                           enable_psnr: bool = True,
                           enable_ms_ssim: bool = False,
                           enable_cambi: bool = False) -> str:
    ref_chain = _scale_chain(width, fps)
    dist_chain = _scale_chain(width, fps)
    branches: List[str] = []
    total = sum([enable_ssim, enable_psnr, enable_ms_ssim]) or 1
    ref_split = (f"[0:v]{ref_chain},split={total}[ref0]"
                 + "".join(f"[ref{i}]" for i in range(1, total)))
    dist_split = (f"[1:v]{dist_chain},split={total}[dist0]"
                  + "".join(f"[dist{i}]" for i in range(1, total)))
    branches.append(ref_split)
    branches.append(dist_split)
    idx = 0
    sinks: List[str] = []
    if enable_ssim:
        branches.append(f"[dist{idx}][ref{idx}]ssim=stats_file=-[s{idx}]")
        sinks.append(f"[s{idx}]nullsink"); idx += 1
    if enable_psnr:
        branches.append(f"[dist{idx}][ref{idx}]psnr=stats_file=-[p{idx}]")
        sinks.append(f"[p{idx}]nullsink"); idx += 1
    if enable_ms_ssim:
        branches.append(f"[dist{idx}][ref{idx}]ms_ssim=stats_file=-[m{idx}]")
        sinks.append(f"[m{idx}]nullsink"); idx += 1
    if enable_cambi:
        branches.append(f"[1:v]{dist_chain},cambi=stats_file=-[c0]")
        sinks.append("[c0]nullsink")
    branches.extend(sinks)
    return ";".join(branches)


# =============================================================================
# The engine
# =============================================================================

class QualityEngine:
    def __init__(
        self,
        config: Optional[ControllerConfig] = None,
        weights: Optional[QualityWeights] = None,
        *,
        sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
        sample_fps: float = DEFAULT_SAMPLE_FPS,
        sample_width: int = DEFAULT_SAMPLE_WIDTH,
        profile: Optional[ContentProfile] = None,
        scorer: Optional[Scorer] = None,
        cache: Optional[Any] = None,
        guard: Optional[InputGuard] = None,
        runner: Optional[SandboxRunner] = None,
        capabilities: Optional[BackendCapabilities] = None,
        hw: Optional[HwCapabilities] = None,
        rate_limiter: Optional[RateLimiter] = None,
        breaker: Optional[CircuitBreaker] = None,
        sink: Optional[MetricsSink] = None,
        tracer: Optional[OTelTracer] = None,
        enable_audio: bool = False,
        enable_cambi: bool = False,
        enable_ms_ssim: bool = False,
        enable_vmaf_neg: bool = False,
        enable_noref: bool = False,
        use_gpu: bool = True,
        use_cache: bool = True,
        enable_scene: bool = False,
    ) -> None:
        self.config = config or ControllerConfig()
        self.profile = profile or ContentProfile(
            width=sample_width, fps=sample_fps, seconds=sample_seconds)
        if sample_seconds != DEFAULT_SAMPLE_SECONDS:
            self.profile.seconds = sample_seconds
        if sample_fps != DEFAULT_SAMPLE_FPS:
            self.profile.fps = sample_fps
        if sample_width != DEFAULT_SAMPLE_WIDTH:
            self.profile.width = sample_width

        self.weights = weights or self.profile.weights
        self.scorer: Scorer = scorer or WeightedScorer(self.weights)
        self.cache = cache or ResultCache()
        self.guard = guard or InputGuard()
        self.runner = runner or SandboxRunner()
        self.capabilities = capabilities or detect_capabilities()
        self.hw = hw or (detect_hw() if use_gpu else HwCapabilities())
        self.rate_limiter = rate_limiter or RateLimiter(3.0, 6.0)
        self.breaker = breaker or CircuitBreaker()
        self.sink: MetricsSink = sink or NullSink()
        self.tracer = tracer or OTelTracer()
        self.enable_audio = enable_audio
        self.enable_cambi = enable_cambi or self.profile.enable_cambi
        self.enable_ms_ssim = enable_ms_ssim
        self.enable_vmaf_neg = enable_vmaf_neg
        self.enable_noref = enable_noref or self.profile.enable_noref
        self.enable_scene = enable_scene or self.profile.scene_aware
        self.use_cache = use_cache
        self.parser = MetricsParser()
        self.noref = LightNoref() if self.enable_noref else None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def measure_channel(
        self,
        ch: ChannelContext,
        reference: Optional[Union[Path, str]] = None,
        distorted: Optional[Union[Path, str]] = None,
    ) -> QualitySample:
        started = _now()
        sample = QualitySample(
            channel_id=getattr(ch, "channel_id", -1),
            target_bitrate_mbps=_safe_float(
                getattr(getattr(ch, "state", None), "target_bitrate", None)),
            actual_bitrate_mbps=_safe_float(
                getattr(getattr(ch, "state", None), "actual_bitrate", None)),
            frame_drops=int(getattr(getattr(ch, "state", None), "frame_drops", 0) or 0),
            profile=self.profile.name,
            backend=self.capabilities.version.split()[0] if self.capabilities.version else "ffmpeg",
            hw=self.hw.describe(),
        )
        with self.tracer.span("quality.measure", channel=sample.channel_id):
            return self._measure_impl(sample, ch, reference, distorted, started)

    def _measure_impl(self, sample: QualitySample, ch: Any,
                      reference: Any, distorted: Any, started: float) -> QualitySample:
        try:
            ref_path = self._resolve_path(
                reference if reference is not None
                else getattr(ch, "input_path", None), "reference")
            dist_path = self._resolve_path(
                distorted if distorted is not None
                else getattr(ch, "encoded_path", None), "distorted")
        except QualityInputError as exc:
            return self._fail(sample, "input", str(exc), started)

        sample.raw["ref"] = str(ref_path)
        sample.raw["dist"] = str(dist_path)

        key = f"ch:{sample.channel_id}"
        if not self.rate_limiter.allow(key, timeout=0.5):
            return self._fail(sample, "ratelimit", "rate limited", started)
        if not self.breaker.allow(key):
            return self._fail(sample, "circuit", "circuit open", started)

        cache_key = self._cache_key(ref_path, dist_path)
        if self.use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._apply_cache(sample, cached)
                sample.perf["cache_hit"] = 1.0
                sample.perf["total_ms"] = (_now() - started) * 1000.0
                self.sink.incr("quality.measure.cache_hit")
                return sample

        try:
            metrics = self._measure(ref_path, dist_path, sample)
            sample.raw["metrics"] = metrics
            self._populate_sample(sample, metrics)
            sample.ok = True
            self.breaker.success(key)
            self.sink.incr("quality.measure.ok")
        except QualityTimeout as exc:
            self.breaker.failure(key)
            return self._fail(sample, "timeout", str(exc), started)
        except QualityInputError as exc:
            self.breaker.failure(key)
            return self._fail(sample, "input", str(exc), started)
        except QualityBackendError as exc:
            self.breaker.failure(key)
            return self._fail(sample, "backend", str(exc), started)
        except Exception as exc:
            logger.exception("unexpected on CH%03d", sample.channel_id)
            self.breaker.failure(key)
            return self._fail(sample, "unexpected", f"{type(exc).__name__}: {exc}",
                              started)

        try:
            hybrid, confidence = self.scorer.score(sample)
        except Exception:
            hybrid, confidence = self.weights.neutral, 0.0
        sample.hybrid = _clamp(hybrid)
        sample.confidence = _clamp(confidence)

        if sample.ok and sample.quality_present():
            with contextlib.suppress(Exception):
                ch.state.quality_metric = sample.hybrid
                ch.state.quality_confidence = sample.confidence

        if self.use_cache and sample.ok:
            self.cache.put(cache_key, self._sample_to_cache(sample))

        sample.perf["total_ms"] = (_now() - started) * 1000.0
        self.sink.observe("quality.hybrid", sample.hybrid,
                          {"profile": self.profile.name})
        self.sink.observe("quality.confidence", sample.confidence)
        return sample

    def _fail(self, sample: QualitySample, kind: str, msg: str,
              started: float) -> QualitySample:
        sample.error = msg
        sample.error_kind = kind
        sample.hybrid = self.weights.neutral
        sample.confidence = 0.0
        sample.perf["total_ms"] = (_now() - started) * 1000.0
        self.sink.incr(f"quality.measure.{kind}")
        return sample

    def measure_all(self, channels: Sequence[ChannelContext], *,
                    parallel: bool = False,
                    max_workers: int = 4) -> List[QualitySample]:
        if not parallel or len(channels) <= 1:
            return [self.measure_channel(ch) for ch in channels]
        results: Dict[int, QualitySample] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs: Dict[Future, int] = {}
            for i, ch in enumerate(channels):
                futs[pool.submit(self.measure_channel, ch)] = i
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:
                    results[idx] = QualitySample(
                        channel_id=getattr(channels[idx], "channel_id", -1),
                        error=f"{type(exc).__name__}: {exc}",
                        error_kind="unexpected",
                        hybrid=self.weights.neutral, confidence=0.0)
        return [results[i] for i in range(len(channels))]

    def measure_pair(self, channel_id: int,
                     reference: Union[Path, str],
                     distorted: Union[Path, str]) -> QualitySample:
        return self.measure_channel(
            ChannelContext(channel_id=channel_id),
            reference=reference, distorted=distorted)

    # ------------------------------------------------------------------
    def _resolve_path(self, value: Any, label: str) -> Path:
        if value is None:
            raise QualityInputError(f"no {label} path provided")
        if isinstance(value, Path):
            p = value
        elif isinstance(value, str):
            if not value.strip():
                raise QualityInputError(f"empty {label} path")
            p = Path(value)
        else:
            raise QualityInputError(f"invalid {label} path type: {type(value)!r}")
        return self.guard.validate(p)

    def _cache_key(self, ref: Path, dist: Path) -> str:
        try: ref_h = _sha256_of_path(ref)
        except OSError: ref_h = "?"
        try: dist_h = _sha256_of_path(dist)
        except OSError: dist_h = "?"
        return _sha256_of_strings(
            ref_h, dist_h,
            str(self.profile.width), f"{self.profile.fps:.3f}",
            f"{self.profile.seconds:.3f}", self.profile.name,
            ",".join(str(x) for x in (
                self.enable_cambi, self.enable_ms_ssim,
                self.enable_audio, self.enable_vmaf_neg,
                self.enable_noref, self.enable_scene,
            )),
            self.capabilities.version,
            self.hw.backend.value,
        )

    def _sample_to_cache(self, s: QualitySample) -> Dict[str, Any]:
        return {
            "vmaf": s.vmaf, "vmaf_neg": s.vmaf_neg,
            "ssim": s.ssim, "ms_ssim": s.ms_ssim, "psnr": s.psnr,
            "cambi": s.cambi, "noref": s.noref,
            "loudness_lufs": s.loudness_lufs, "loudness_lra": s.loudness_lra,
            "backend": s.backend, "profile": s.profile, "hw": s.hw,
            "raw": s.raw.get("metrics", {}),
        }

    def _apply_cache(self, sample: QualitySample, cached: Mapping[str, Any]) -> None:
        for k in ("vmaf", "vmaf_neg", "ssim", "ms_ssim", "psnr", "cambi",
                  "noref", "loudness_lufs", "loudness_lra"):
            setattr(sample, k, cached.get(k))
        sample.ok = True
        sample.raw["metrics"] = cached.get("raw", {})
        sample.raw["from_cache"] = True
        try:
            h, c = self.scorer.score(sample)
            sample.hybrid = _clamp(h)
            sample.confidence = _clamp(c)
        except Exception:
            sample.hybrid = self.weights.neutral
            sample.confidence = 0.0

    # ------------------------------------------------------------------
    def _measure(self, ref: Path, dist: Path,
                 sample: QualitySample) -> Dict[str, float]:
        metrics: Dict[str, float] = {}
        t0 = _now()

        try:
            metrics.update(self._run_ssim_psnr_pass(ref, dist))
        except QualityTimeout:
            raise
        except QualityBackendError as exc:
            logger.debug("ssim/psnr failed: %s", exc)
        sample.perf["ssim_psnr_ms"] = (_now() - t0) * 1000.0

        if self.capabilities.has_libvmaf:
            t1 = _now()
            try:
                v = self._run_vmaf(ref, dist, model="default")
                if v is not None:
                    metrics["vmaf"] = v
            except QualityTimeout:
                raise
            except QualityBackendError as exc:
                logger.debug("vmaf failed: %s", exc)
            sample.perf["vmaf_ms"] = (_now() - t1) * 1000.0

            if self.enable_vmaf_neg:
                t2 = _now()
                with contextlib.suppress(Exception):
                    vn = self._run_vmaf(ref, dist, model="neg")
                    if vn is not None:
                        metrics["vmaf_neg"] = vn
                sample.perf["vmaf_neg_ms"] = (_now() - t2) * 1000.0

        if self.enable_audio:
            t3 = _now()
            with contextlib.suppress(Exception):
                metrics.update(self._run_loudness(dist))
            sample.perf["loudness_ms"] = (_now() - t3) * 1000.0

        if self.enable_noref and self.noref is not None:
            t4 = _now()
            try:
                ns = self.noref.score(dist)
                if ns is not None:
                    metrics["noref"] = ns
            except Exception as exc:
                logger.debug("noref failed: %s", exc)
            sample.perf["noref_ms"] = (_now() - t4) * 1000.0

        return metrics

    def _populate_sample(self, s: QualitySample, m: Mapping[str, float]) -> None:
        for k in ("vmaf", "vmaf_neg", "ssim", "ms_ssim", "psnr", "cambi",
                  "noref", "loudness_lufs", "loudness_lra"):
            setattr(s, k, m.get(k))

    # ------------------------------------------------------------------
    def _common_flags(self) -> List[str]:
        flags = ["-hide_banner", "-nostdin", "-loglevel", "info", "-threads", "0"]
        if self.hw.hwaccel and self.hw.backend is not HwBackend.CPU:
            flags = ["-hide_banner", "-nostdin", "-loglevel", "info",
                     "-threads", "0"]
        return flags

    def _validate_sampling(self) -> Tuple[int, float, float]:
        w = max(MIN_SAMPLE_WIDTH, min(MAX_SAMPLE_WIDTH, int(self.profile.width)))
        f = max(0.5, min(30.0, float(self.profile.fps)))
        sec = max(0.5, min(MAX_SAMPLE_SECONDS, float(self.profile.seconds)))
        if f * sec > MAX_SAMPLE_FRAMES:
            sec = MAX_SAMPLE_FRAMES / f
        return w, f, sec

    def _run_ssim_psnr_pass(self, ref: Path, dist: Path) -> Dict[str, float]:
        w, f, sec = self._validate_sampling()
        filt = build_ssim_psnr_filter(
            w, f, enable_ssim=True, enable_psnr=True,
            enable_ms_ssim=self.enable_ms_ssim and self.capabilities.has_ms_ssim,
            enable_cambi=self.enable_cambi and self.capabilities.has_cambi,
        )
        argv = (
            [self.capabilities.ffmpeg] + self._common_flags()
            + ["-t", f"{sec:.3f}", "-i", str(ref)]
            + ["-t", f"{sec:.3f}", "-i", str(dist)]
            + ["-an", "-sn", "-dn"]
            + ["-filter_complex", filt]
            + ["-f", "null", "-"]
        )
        proc = self.runner.run(argv, timeout=DEFAULT_FFMPEG_TIMEOUT)
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
        metrics = self.parser.parse_ssim_psnr(text)
        if not metrics and proc.returncode != 0:
            raise QualityBackendError(
                f"ssim/psnr rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return metrics

    def _run_vmaf(self, ref: Path, dist: Path,
                  model: str = "default") -> Optional[float]:
        if not self.capabilities.has_libvmaf:
            return None
        w, f, sec = self._validate_sampling()
        with tempfile.TemporaryDirectory(prefix="astcie-vmaf-") as td:
            with contextlib.suppress(OSError):
                os.chmod(td, 0o700)
            log = Path(td) / "vmaf.json"
            model_arg = ""
            if model == "neg":
                # Ask libvmaf for the NEG model if it ships with the build;
                # otherwise the extra arg is ignored by most builds.
                model_arg = ":model=version=vmaf_v0.6.1neg"
            filt = (
                f"[0:v]scale={w}:-2:flags=bicubic,fps={f},setsar=1[ref];"
                f"[1:v]scale={w}:-2:flags=bicubic,fps={f},setsar=1[dist];"
                f"[dist][ref]libvmaf=log_path={log}:log_fmt=json:"
                f"n_threads=2{model_arg}"
            )
            argv = (
                [self.capabilities.ffmpeg] + self._common_flags()
                + ["-loglevel", "error"]
                + ["-t", f"{sec:.3f}", "-i", str(ref)]
                + ["-t", f"{sec:.3f}", "-i", str(dist)]
                + ["-an", "-sn", "-dn"]
                + ["-filter_complex", filt]
                + ["-f", "null", "-"]
            )
            proc = self.runner.run(argv, timeout=DEFAULT_VMAF_TIMEOUT)
            if proc.returncode != 0:
                raise QualityBackendError(
                    f"vmaf rc={proc.returncode}: {proc.stderr.strip()[:200]}")
            if not log.exists():
                return None
            try:
                text = log.read_text(encoding="utf-8")
            except OSError:
                return None
            return self.parser.parse_vmaf_json(text)

    def _run_loudness(self, dist: Path) -> Dict[str, float]:
        if not self.capabilities.has_loudnorm:
            return {}
        argv = (
            [self.capabilities.ffmpeg] + self._common_flags()
            + ["-i", str(dist)]
            + ["-vn", "-sn", "-dn"]
            + ["-af", "loudnorm=print_format=summary"]
            + ["-f", "null", "-"]
        )
        proc = self.runner.run(argv, timeout=DEFAULT_FFMPEG_TIMEOUT)
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
        return self.parser.parse_loudness(text)


# =============================================================================
# Config builder
# =============================================================================

def engine_from_config(cfg: Mapping[str, Any]) -> QualityEngine:
    profile = get_profile(str(cfg.get("profile", "default")))
    if "sample_seconds" in cfg:
        profile.seconds = float(cfg["sample_seconds"])
    if "sample_fps" in cfg:
        profile.fps = float(cfg["sample_fps"])
    if "sample_width" in cfg:
        profile.width = int(cfg["sample_width"])

    limits = SandboxLimits(
        cpu_seconds=int(cfg.get("cpu_seconds", 120)),
        address_space_bytes=int(cfg.get("memory_bytes", 4 * 1024 ** 3)),
        file_size_bytes=int(cfg.get("file_size_bytes", 2 * 1024 ** 3)),
    )
    runner = SandboxRunner(limits=limits)

    cache_dir = cfg.get("cache_dir")
    l3 = ResultCache(Path(cache_dir)) if cache_dir else None
    l1 = _LRU(int(cfg.get("cache_memory", 512)))
    l2: Optional[RedisCache] = None
    if cfg.get("redis_url"):
        l2 = RedisCache(str(cfg["redis_url"]))
    cache: Any
    if l2 is not None or l3 is not None:
        cache = LayeredCache(l1, l2, l3)
    else:
        cache = l3 or ResultCache()

    roots = cfg.get("allowed_roots")
    guard = InputGuard(
        allowed_roots=[Path(r) for r in roots] if roots else None)

    rl = RateLimiter(
        rate_per_sec=float(cfg.get("rate_limit_per_sec", 3.0)),
        burst=float(cfg.get("rate_burst", 6.0)))
    br = CircuitBreaker(
        failure_threshold=int(cfg.get("breaker_failures", 5)),
        recovery_timeout=float(cfg.get("breaker_recovery_s", 30.0)))

    sink = make_sink(prometheus=bool(cfg.get("prometheus", True)))

    scorer_name = cfg.get("scorer", "weighted")
    if scorer_name == "strict":
        scorer: Scorer = StrictScorer(profile.weights)
    elif scorer_name == "bayesian":
        scorer = BayesianScorer(profile.weights)
    elif scorer_name == "ensemble":
        scorer = EnsembleScorer([
            WeightedScorer(profile.weights), BayesianScorer(profile.weights)])
    else:
        scorer = WeightedScorer(profile.weights)

    return QualityEngine(
        weights=profile.weights,
        profile=profile,
        scorer=scorer,
        cache=cache,
        guard=guard,
        runner=runner,
        rate_limiter=rl,
        breaker=br,
        sink=sink,
        enable_cambi=bool(cfg.get("enable_cambi", profile.enable_cambi)),
        enable_ms_ssim=bool(cfg.get("enable_ms_ssim", False)),
        enable_audio=bool(cfg.get("enable_audio", False)),
        enable_vmaf_neg=bool(cfg.get("enable_vmaf_neg", False)),
        enable_noref=bool(cfg.get("enable_noref", False)),
        enable_scene=bool(cfg.get("enable_scene", profile.scene_aware)),
        use_gpu=bool(cfg.get("use_gpu", True)),
        use_cache=bool(cfg.get("use_cache", True)),
    )


# =============================================================================
# REST API
# =============================================================================

class _QuietHandler(http.server.BaseHTTPRequestHandler):
    server_version = "QualityEngine/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("http %s", fmt % args)

    # -- helpers ------------------------------------------------------------
    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return None

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok"})
            return
        if parsed.path == "/capabilities":
            self._send_json(200, self.server.engine.capabilities.to_dict())  # type: ignore
            return
        if parsed.path == "/hw":
            self._send_json(200, {
                "backend": self.server.engine.hw.backend.value,  # type: ignore
                "device": self.server.engine.hw.device_name,  # type: ignore
                "describe": self.server.engine.hw.describe(),  # type: ignore
            })
            return
        if parsed.path == "/metrics":
            # If prometheus_client is installed, expose the standard format.
            if _prom is None:
                self._send_json(501, {"error": "prometheus_client not installed"})
                return
            body = _prom.generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        payload = self._read_json() or {}
        if parsed.path == "/measure":
            ref = payload.get("reference") or payload.get("ref")
            dist = payload.get("distorted") or payload.get("dist")
            ch_id = int(payload.get("channel_id", 0))
            if not ref or not dist:
                self._send_json(400, {"error": "reference & distorted required"})
                return
            sample = self.server.engine.measure_pair(ch_id, ref, dist)  # type: ignore
            self._send_json(200, sample.to_dict())
            return
        if parsed.path == "/scenes":
            path = payload.get("path")
            if not path:
                self._send_json(400, {"error": "path required"})
                return
            sm = detect_scenes(Path(path))
            self._send_json(200, sm.to_dict())
            return
        if parsed.path == "/ladder":
            pts = payload.get("points") or []
            ladder_points = [
                LadderPoint(
                    width=int(p["width"]), height=int(p["height"]),
                    fps=float(p.get("fps", 30.0)),
                    bitrate_kbps=float(p["bitrate_kbps"]),
                    quality=float(p["quality"]),
                    codec=p.get("codec", "h264"),
                    label=p.get("label", ""),
                ) for p in pts
            ]
            ladder = build_ladder(ladder_points)
            self._send_json(200, ladder.to_dict())
            return
        if parsed.path == "/ab":
            a = payload.get("a") or []
            b = payload.get("b") or []
            result = ABTest().compare([float(x) for x in a], [float(x) for x in b])
            self._send_json(200, result.to_dict())
            return
        self._send_json(404, {"error": "not found"})


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server(engine: QualityEngine, host: str = "127.0.0.1",
               port: int = 8123) -> None:
    server = _ThreadingHTTPServer((host, port), _QuietHandler)
    server.engine = engine  # type: ignore
    logger.info("REST API listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down API")
    finally:
        server.server_close()


# =============================================================================
# gRPC (optional)
# =============================================================================

# We don't ship generated stubs. If grpc is available and callers want it,
# they can register their own servicer. We only provide the plumbing.
def make_grpc_server(engine: QualityEngine, *, port: int = 8124,
                     max_workers: int = 8) -> Optional[Any]:
    if _grpc is None or _grpc_futures is None:
        logger.warning("grpc not installed; skipping gRPC server")
        return None
    server = _grpc.server(_grpc_futures.ThreadPoolExecutor(max_workers=max_workers))
    # Attach engine so handlers can access it.
    server._astcie_engine = engine  # type: ignore
    server.add_insecure_port(f"[::]:{port}")
    logger.info("gRPC server prepared on port %d", port)
    return server


# =============================================================================
# Stress tests / fuzzing helpers
# =============================================================================

class StressTester:
    """Generate synthetic rate-quality data and fuzz the scorers.

    This is not a substitute for real golden files, but it exercises the
    logic paths that are easy to get wrong: empty metrics, NaN values,
    extreme outliers, cache collisions, and scorer invariants.
    """

    def __init__(self, seed: int = 1234) -> None:
        self._rng = random.Random(seed)

    def _random_sample(self) -> QualitySample:
        r = self._rng
        def maybe(p: float, gen):
            return gen() if r.random() < p else None
        return QualitySample(
            channel_id=r.randint(0, 999),
            vmaf=maybe(0.85, lambda: r.uniform(0, 100)),
            ssim=maybe(0.85, lambda: r.uniform(0, 1)),
            ms_ssim=maybe(0.5, lambda: r.uniform(0, 1)),
            psnr=maybe(0.85, lambda: r.uniform(10, 60)),
            cambi=maybe(0.4, lambda: r.uniform(0, 3)),
            noref=maybe(0.4, lambda: r.uniform(0, 1)),
            actual_bitrate_mbps=maybe(0.7, lambda: r.uniform(0.1, 20)),
            target_bitrate_mbps=maybe(0.7, lambda: r.uniform(0.1, 20)),
            frame_drops=r.randint(0, 100),
            ok=r.random() < 0.9,
        )

    def check_scorer_invariants(self, scorer: Scorer, n: int = 500) -> Dict[str, Any]:
        failures: List[str] = []
        for i in range(n):
            s = self._random_sample()
            try:
                h, c = scorer.score(s)
            except Exception as exc:
                failures.append(f"i={i} raised {type(exc).__name__}: {exc}")
                continue
            if not (0.0 <= h <= 1.0):
                failures.append(f"i={i} hybrid out of range: {h}")
            if not (0.0 <= c <= 1.0):
                failures.append(f"i={i} confidence out of range: {c}")
        return {"failures": failures, "n": n, "ok": not failures}

    def fuzz_parser(self, parser: MetricsParser, n: int = 500) -> Dict[str, Any]:
        r = self._rng
        failures: List[str] = []
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.:, =\n"
        for i in range(n):
            length = r.randint(0, 400)
            text = "".join(r.choice(alphabet) for _ in range(length))
            try:
                out = parser.parse_ssim_psnr(text)
                for k, v in out.items():
                    if not isinstance(v, float):
                        failures.append(f"i={i} non-float {k}={v!r}")
            except Exception as exc:
                failures.append(f"i={i} raised: {exc}")
        return {"failures": failures, "n": n, "ok": not failures}

    def check_ladder(self, n: int = 100) -> Dict[str, Any]:
        failures: List[str] = []
        for i in range(n):
            pts = [
                LadderPoint(
                    width=1920, height=1080, fps=30.0,
                    bitrate_kbps=self._rng.uniform(200, 8000),
                    quality=self._rng.uniform(0.3, 1.0),
                ) for _ in range(self._rng.randint(2, 12))
            ]
            try:
                ladder = build_ladder(pts)
            except Exception as exc:
                failures.append(f"i={i} raised: {exc}")
                continue
            bs = [p.bitrate_kbps for p in ladder.frontier]
            if any(b2 < b1 for b1, b2 in zip(bs, bs[1:])):
                failures.append(f"i={i} frontier not sorted: {bs}")
        return {"failures": failures, "n": n, "ok": not failures}

    def run_all(self, engine: Optional[QualityEngine] = None) -> Dict[str, Any]:
        scorer = engine.scorer if engine else WeightedScorer()
        parser = engine.parser if engine else MetricsParser()
        return {
            "scorer_invariants": self.check_scorer_invariants(scorer),
            "parser_fuzz": self.fuzz_parser(parser),
            "ladder_invariants": self.check_ladder(),
        }


# =============================================================================
# CLI
# =============================================================================

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="astcie-quality",
                                description="Measure video quality.")
    sub = p.add_subparsers(dest="cmd")

    measure = sub.add_parser("measure", help="measure a ref/dist pair")
    measure.add_argument("--ref", required=True)
    measure.add_argument("--dist", required=True)
    measure.add_argument("--profile", default="default", choices=sorted(_PROFILES))
    measure.add_argument("--seconds", type=float, default=DEFAULT_SAMPLE_SECONDS)
    measure.add_argument("--fps", type=float, default=DEFAULT_SAMPLE_FPS)
    measure.add_argument("--width", type=int, default=DEFAULT_SAMPLE_WIDTH)
    measure.add_argument("--no-cache", action="store_true")
    measure.add_argument("--cambi", action="store_true")
    measure.add_argument("--audio", action="store_true")
    measure.add_argument("--vmaf-neg", action="store_true")
    measure.add_argument("--noref", action="store_true")
    measure.add_argument("--scene", action="store_true")
    measure.add_argument("--gpu", action="store_true")
    measure.add_argument("--scorer", default="weighted",
                         choices=["weighted", "strict", "bayesian", "ensemble"])
    measure.add_argument("--json", action="store_true")

    caps = sub.add_parser("caps", help="show ffmpeg capabilities")
    caps.add_argument("--json", action="store_true")

    hw = sub.add_parser("hw", help="show hardware acceleration info")
    hw.add_argument("--json", action="store_true")

    scenes = sub.add_parser("scenes", help="detect scene cuts")
    scenes.add_argument("path")
    scenes.add_argument("--json", action="store_true")

    ladder = sub.add_parser("ladder", help="build a ladder from a JSON file")
    ladder.add_argument("points_json")

    ab = sub.add_parser("ab", help="A/B test two score arrays")
    ab.add_argument("a_json")
    ab.add_argument("b_json")
    ab.add_argument("--method", default="bootstrap",
                    choices=["bootstrap", "welch"])

    serve = sub.add_parser("serve", help="start the REST API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8123)
    serve.add_argument("--profile", default="default", choices=sorted(_PROFILES))
    serve.add_argument("--gpu", action="store_true")

    sub.add_parser("selftest", help="run stress / fuzz tests")

    p.add_argument("--verbose", "-v", action="store_true")
    return p


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cmd = getattr(args, "cmd", None)
    if cmd == "caps":
        caps = detect_capabilities()
        if args.json:
            print(json.dumps(caps.to_dict(), indent=2))
        else:
            for k, v in caps.to_dict().items():
                print(f"{k:20s}: {v}")
        return 0

    if cmd == "hw":
        cap = detect_hw()
        if args.json:
            print(json.dumps({
                "backend": cap.backend.value,
                "device": cap.device,
                "device_name": cap.device_name,
                "supports_vmaf_gpu": cap.supports_vmaf_gpu,
            }, indent=2))
        else:
            print(cap.describe())
        return 0

    if cmd == "scenes":
        sm = detect_scenes(Path(args.path))
        if args.json:
            print(json.dumps(sm.to_dict(), indent=2))
        else:
            print(f"total: {sm.total_duration_s:.2f}s, {len(sm)} shots")
            for s in sm.shots[:20]:
                print(f"  {s.start_s:7.2f} - {s.end_s:7.2f}  ({s.duration_s:.2f}s)")
        return 0

    if cmd == "ladder":
        pts = json.loads(Path(args.points_json).read_text())
        lp = [LadderPoint(
            width=int(p["width"]), height=int(p["height"]),
            fps=float(p.get("fps", 30.0)),
            bitrate_kbps=float(p["bitrate_kbps"]),
            quality=float(p["quality"]),
            codec=p.get("codec", "h264"),
            label=p.get("label", "")) for p in pts]
        ladder = build_ladder(lp)
        print(json.dumps(ladder.to_dict(), indent=2))
        return 0

    if cmd == "ab":
        a = json.loads(args.a_json)
        b = json.loads(args.b_json)
        res = ABTest().compare(a, b, method=args.method)
        print(json.dumps(res.to_dict(), indent=2))
        return 0

    if cmd == "selftest":
        tester = StressTester()
        engine = QualityEngine(
            profile=get_profile("default"),
            use_gpu=False, use_cache=False)
        out = tester.run_all(engine)
        print(json.dumps(out, indent=2, default=str))
        return 0 if all(v.get("ok", False) for v in out.values()) else 1

    if cmd == "serve":
        engine = QualityEngine(
            profile=get_profile(args.profile),
            use_gpu=bool(args.gpu),
        )
        run_server(engine, host=args.host, port=args.port)
        return 0

    if cmd == "measure" or cmd is None:
        if cmd is None:
            parser.print_help()
            return 2
        engine = QualityEngine(
            profile=get_profile(args.profile),
            sample_seconds=args.seconds,
            sample_fps=args.fps,
            sample_width=args.width,
            enable_cambi=args.cambi,
            enable_audio=args.audio,
            enable_vmaf_neg=args.vmaf_neg,
            enable_noref=args.noref,
            enable_scene=args.scene,
            use_gpu=args.gpu,
            use_cache=not args.no_cache,
            scorer={
                "weighted": WeightedScorer,
                "strict": StrictScorer,
                "bayesian": BayesianScorer,
                "ensemble": lambda w: EnsembleScorer(
                    [WeightedScorer(w), BayesianScorer(w)]),
            }[args.scorer](get_profile(args.profile).weights),
        )
        sample = engine.measure_pair(0, args.ref, args.dist)
        if args.json:
            print(json.dumps(sample.to_dict(), indent=2, default=str))
        else:
            for k in ("vmaf", "ssim", "ms_ssim", "psnr", "cambi", "noref",
                      "hybrid", "confidence", "ok", "error", "error_kind"):
                print(f"{k:14s}: {getattr(sample, k)}")
            print(f"{'total_ms':14s}: {sample.perf.get('total_ms', 0):.1f}")
        return 0 if sample.ok else 1

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
