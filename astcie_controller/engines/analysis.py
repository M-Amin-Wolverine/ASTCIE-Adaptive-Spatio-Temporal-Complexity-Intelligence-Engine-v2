#!/usr/bin/env python3
"""
AnalysisEngine – runs ASTCIE V8.1 (complexity7) + V9 (complexity8)
in parallel for every channel and extracts the required metrics.

Mode A:
  - segment_seconds clamped to [1.0, 5.0]
  - per-segment rows from engine JSON kept on channel (segments / segment_analyses)
  - Hybrid/Allocator still use aggregate complexity/bdi only
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import ChannelContext, ControllerConfig

logger = logging.getLogger("astcie.analysis")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str | Path], logger: logging.Logger | None = None) -> None:
    cmd_str = [str(x) for x in cmd]
    if logger:
        logger.debug("Running: %s", " ".join(cmd_str))

    result = subprocess.run(
        cmd_str,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        err = (result.stderr or "").strip()
        if logger:
            logger.error("Command failed (%s): %s", result.returncode, err)
        raise RuntimeError(f"Command failed ({result.returncode}): {err}")


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pick(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Result discovery (safe, unique per channel)
# ---------------------------------------------------------------------------

def _find_v81_result(channel: ChannelContext) -> Path:
    """
    V8.1 always writes to results/<stem>/result.json (no --output flag).
    """
    candidates = [
        Path("results") / channel.stem / "result.json",
        Path("results") / channel.stem / "result_v81.json",
        channel.analysis_dir / "result.json",
        channel.analysis_dir / "result_v81.json",
        channel.analysis_dir / channel.stem / "result.json",
        Path(".") / channel.stem / "result.json",
    ]
    for p in candidates:
        if p.exists():
            return p

    for root in (Path("results"), Path(".")):
        if root.exists():
            for p in root.rglob("result*.json"):
                if p.parent.name.lower() == channel.stem.lower():
                    if "v9" in p.name.lower():
                        continue
                    return p
    raise FileNotFoundError(
        f"V8.1 JSON not found for channel {channel.channel_id} ({channel.stem})"
    )


def _find_v9_result(channel: ChannelContext) -> Path:
    """
    V9 writes to:
      - results_v9/<stem>/result_v9.json  (default)
      - <output>/<stem>/result_v9.json    (when --output is given)
    """
    candidates = [
        channel.analysis_dir / channel.stem / "result_v9.json",
        channel.analysis_dir / channel.stem / "result.json",
        channel.analysis_dir / "result_v9.json",
        channel.analysis_dir / "result.json",
        Path("results_v9") / channel.stem / "result_v9.json",
        Path("results_v9") / channel.stem / "result.json",
        Path("results") / channel.stem / "result_v9.json",
        Path(".") / channel.stem / "result_v9.json",
    ]
    for p in candidates:
        if p.exists():
            return p

    search_roots = []
    if channel.analysis_dir and channel.analysis_dir.exists():
        search_roots.append(channel.analysis_dir)
    search_roots.extend([Path("results_v9"), Path("results"), Path(".")])

    for root in search_roots:
        if not root.exists():
            continue
        for p in root.rglob("result*.json"):
            parent = p.parent.name.lower()
            stem = channel.stem.lower()
            if parent == stem or parent.endswith(stem):
                return p
    raise FileNotFoundError(
        f"V9 JSON not found for channel {channel.channel_id} ({channel.stem})"
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_v81(data: Dict[str, Any]) -> Dict[str, Any]:
    c = _pick(data, "v8_fusion_score", "complexity")
    b = _pick(data, "bit_demand_index", "bdi", "BDI")
    if c is None or b is None:
        raise KeyError("V8.1 JSON missing v8_fusion_score or bit_demand_index")
    return {
        "complexity": _to_float(c),
        "bdi": _to_float(b),
        "spatial_score": _to_float(_pick(data, "spatial_score")),
        "temporal_score": _to_float(_pick(data, "temporal_score")),
        "motion_score": _to_float(_pick(data, "motion_score")),
        "regime": _pick(data, "content_regime", "regime"),
        "segments": data.get("segments") or [],
        "raw": data,
    }


def extract_v9(data: Dict[str, Any]) -> Dict[str, Any]:
    c = _pick(data, "v9_fusion_score", "complexity", "v9_fusion")
    b = _pick(data, "bit_demand_index", "bdi", "BDI")
    if c is None or b is None:
        keys = list(data.keys())[:30]
        raise KeyError(
            f"V9 JSON missing v9_fusion_score or bit_demand_index. "
            f"Available keys (first 30): {keys}"
        )
    return {
        "complexity": _to_float(c),
        "bdi": _to_float(b),
        "adci_temporal": _to_float(_pick(data, "adci_temporal")),
        "adci_spatial": _to_float(_pick(data, "adci_spatial")),
        "risk": _to_float(_pick(data, "risk")),
        "regime": _pick(data, "content_regime", "regime"),
        "segments": data.get("segments") or [],
        "raw": data,
    }


# ---------------------------------------------------------------------------
# AnalysisEngine
# ---------------------------------------------------------------------------

class AnalysisEngine:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.analysis")

    def _segment_seconds(self) -> float:
        """Clamp segment length to [1.0, 5.0] seconds (Mode A)."""
        s = float(getattr(self.config, "segment_seconds", 5.0) or 5.0)
        return max(1.0, min(5.0, s))

    def _source_for(self, channel: ChannelContext) -> Path:
        """
        Return the actual source selected by SourceManager.

        Source priority:
            1. active_source_path
            2. input_path
        """
        source = channel.active_source_path or channel.input_path

        if not source:
            raise FileNotFoundError(
                f"Channel {channel.channel_id}: no source available"
            )

        source = Path(source)

        if not source.exists():
            raise FileNotFoundError(
                f"Channel {channel.channel_id}: source not found: {source}"
            )

        return source

    def _run_v81(self, channel: ChannelContext) -> Dict[str, Any]:
        channel.analysis_dir.mkdir(parents=True, exist_ok=True)
        source = self._source_for(channel)

        cmd = [
            sys.executable,
            self.config.v81_script,
            source,
            "--fps", str(self.config.analysis_fps),
            "--width", str(self.config.analysis_width),
            "--segment", str(self._segment_seconds()),
        ]

        _run_cmd(cmd, self.logger)
        data = _load_json(_find_v81_result(channel))
        return extract_v81(data)

    def _run_v9(self, channel: ChannelContext) -> Dict[str, Any]:
        channel.analysis_dir.mkdir(parents=True, exist_ok=True)
        source = self._source_for(channel)

        cmd = [
            sys.executable,
            self.config.v9_script,
            source,
            "--fps", str(self.config.analysis_fps),
            "--width", str(self.config.analysis_width),
            "--segment", str(self._segment_seconds()),
            "--output", str(channel.analysis_dir),
        ]

        _run_cmd(cmd, self.logger)
        data = _load_json(_find_v9_result(channel))
        return extract_v9(data)

    def run_parallel(self, channels: List[ChannelContext]) -> None:
        """
        For every channel run V8.1 + V9 (optionally in parallel threads).
        Results are stored inside channel.v81 / channel.v9.
        Segment rows are kept under channel.segment_analyses (Mode A).
        """
        self.logger.info("Starting analysis for %d channel(s)", len(channels))

        def process_one(ch: ChannelContext) -> None:
            self.logger.info(
                "▶ Analyzing channel %03d – %s", ch.channel_id, ch.stem
            )

            if self.config.no_parallel_engines:
                ch.v81 = self._run_v81(ch)
                ch.v9 = self._run_v9(ch)
            else:
                with ThreadPoolExecutor(max_workers=2) as ex:
                    f1 = ex.submit(self._run_v81, ch)
                    f2 = ex.submit(self._run_v9, ch)
                    ch.v81 = f1.result()
                    ch.v9 = f2.result()

            ch.segment_analyses = {
                "v81": (ch.v81 or {}).get("segments") or [],
                "v9": (ch.v9 or {}).get("segments") or [],
                "segment_seconds": self._segment_seconds(),
            }
            self.logger.info(
                "CH%03d segments: v81=%d v9=%d (%.1fs each)",
                ch.channel_id,
                len(ch.segment_analyses["v81"]),
                len(ch.segment_analyses["v9"]),
                ch.segment_analyses["segment_seconds"],
            )
            self.logger.info("✔ Channel %03d analysis done", ch.channel_id)

        workers = min(self.config.max_workers, len(channels)) or 1
        if workers == 1:
            for ch in channels:
                process_one(ch)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(process_one, ch): ch for ch in channels}
                for fut in as_completed(futures):
                    ch = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        self.logger.error(
                            "Analysis failed for channel %03d: %s",
                            ch.channel_id,
                            exc,
                        )
                        raise
