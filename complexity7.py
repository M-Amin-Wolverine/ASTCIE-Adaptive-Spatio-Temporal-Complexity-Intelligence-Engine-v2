#!/usr/bin/env python3
"""
ASTCIE V8.1 ULTIMATE
Adaptive Spatio-Temporal Complexity Intelligence Engine

V4  Robust MedAD Distribution
V5  Burst / Tail / Extreme Intelligence
V6  Event / Risk / Profile Intelligence
V7  Spatial + Temporal Unified Intelligence
V8  Robust Motion + Texture + Segment Fusion

Key improvements:
- Robust temporal distribution
- Spatial Information (SI)
- Motion Occupancy
- Motion Magnitude
- Texture / Laplacian variance
- Burst / Tail / Extreme
- Robust scene/event activity
- Fixed 5-second segmentation
- Dataset-independent robust normalization
- Balanced V8 Fusion
- Independent Bit Demand Index
- Sharp graphical dashboard
- CSV / JSON exports

Experimental research tool.
NOT a bitrate predictor.
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
)
from rich.text import Text

# Fix Windows Unicode issues with Rich
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    os.environ["PYTHONIOENCODING"] = "utf-8"

# ============================================================
# CONFIG
# ============================================================

VERSION = "8.1.0-ULTIMATE"

DEFAULT_FPS = 25.0
DEFAULT_WIDTH = 640
DEFAULT_SEGMENT_SECONDS = 5.0

# Robust event threshold
SCENE_THRESHOLD = 0.35

# Extreme temporal event
EXTREME_MULTIPLIER = 3.0

console = Console()


# ============================================================
# VISUAL CONFIG
# ============================================================

COLORS = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "grid": "#21262d",
    "text": "#e6edf3",
    "cyan": "#39d0d8",
    "magenta": "#ff7b72",
    "green": "#3fb950",
    "yellow": "#d29922",
    "blue": "#58a6ff",
    "orange": "#f0883e",
    "purple": "#bc8cff",
    "red": "#f85149",
    "white": "#f0f6fc",
}

plt.rcParams.update({
    "figure.facecolor": COLORS["bg"],
    "axes.facecolor": COLORS["panel"],
    "axes.edgecolor": COLORS["grid"],
    "axes.labelcolor": COLORS["text"],
    "xtick.color": COLORS["text"],
    "ytick.color": COLORS["text"],
    "text.color": COLORS["text"],
    "grid.color": COLORS["grid"],
    "grid.linewidth": 0.6,
    "grid.alpha": 0.7,
    "font.size": 10,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.labelsize": 10,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "lines.linewidth": 1.6,
    "axes.linewidth": 1.2,
})


# ============================================================
# UTILITIES
# ============================================================

def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def safe_div(a, b):
    return float(a) / float(b) if b != 0 else 0.0


def percentile(values, p):
    if values is None or len(values) == 0:
        return 0.0
    return float(np.percentile(values, p))


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def ensure_parent(path):
    parent = Path(path).parent

    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# ROBUST NORMALIZATION
# ============================================================

def robust_normalize(value, p10, p90):
    """
    Robust min-max style normalization.

    Uses P10/P90 instead of absolute min/max to reduce
    sensitivity to outliers.
    """
    if p90 <= p10:
        return 0.0

    return clamp((float(value) - p10) / (p90 - p10))


def robust_score(values, value=None):
    """
    Dataset/video-relative robust normalization.

    Returns:
        score
        statistics
    """
    if values is None or len(values) == 0:
        return 0.0, {
            "p10": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
        }

    arr = np.asarray(values, dtype=np.float64)

    p10, p50, p90, p99 = np.percentile(
        arr,
        [10, 50, 90, 99]
    )

    if value is None:
        value = p50

    score = robust_normalize(value, p10, p90)

    return score, {
        "p10": float(p10),
        "p50": float(p50),
        "p90": float(p90),
        "p99": float(p99),
    }


# ============================================================
# CONSOLE
# ============================================================

def print_banner():

    body = (
        "[bold cyan]ASTCIE V8.1 ULTIMATE[/bold cyan]\n"
        "[dim]Adaptive Spatio-Temporal Complexity Intelligence Engine[/dim]\n\n"

        "[yellow]V4[/yellow]  Robust MedAD Distribution\n"
        "[yellow]V5[/yellow]  Burst / Tail / Extreme Intelligence\n"
        "[yellow]V6[/yellow]  Event / Risk / Profile Intelligence\n"
        "[yellow]V7[/yellow]  Spatial + Temporal Fusion\n"
        "[bold green]V8[/bold green]  Robust Motion + Texture + Fusion\n\n"

        "[dim]Experimental content-complexity framework[/dim]"
    )

    console.print(
        Panel(
            body,
            border_style="cyan",
            padding=(1, 3),
        )
    )


def info(msg):
    console.print(
        f"[bold blue]>>[/bold blue] [cyan]{msg}[/cyan]"
    )


def success(msg):
    console.print(
        f"[bold green]SUCCESS[/bold green] {msg}"
    )


def warning(msg):
    console.print(
        f"[bold yellow]WARNING[/bold yellow] {msg}"
    )


def error(msg):
    console.print(
        f"[bold red]ERROR[/bold red] {msg}"
    )


# ============================================================
# CORE METRICS
# ============================================================

def calculate_medad(frame1, frame2):

    diff = cv2.absdiff(frame1, frame2)

    return float(np.median(diff))


def calculate_motion_magnitude(frame1, frame2):

    diff = cv2.absdiff(frame1, frame2)

    return float(np.mean(diff))


def calculate_motion_occupancy(
    frame1,
    frame2,
    threshold=8,
):

    diff = cv2.absdiff(frame1, frame2)

    changed = diff > threshold

    return float(
        np.count_nonzero(changed)
        / changed.size
    )


def calculate_si(gray):

    gx = cv2.Sobel(
        gray,
        cv2.CV_32F,
        1,
        0,
        ksize=3,
    )

    gy = cv2.Sobel(
        gray,
        cv2.CV_32F,
        0,
        1,
        ksize=3,
    )

    magnitude = cv2.magnitude(gx, gy)

    return float(np.std(magnitude))


def calculate_scene_activity(frame1, frame2):

    diff = cv2.absdiff(frame1, frame2)

    return float(
        np.mean(diff) / 255.0
    )


def calculate_texture(gray):

    return float(
        cv2.Laplacian(
            gray,
            cv2.CV_64F
        ).var()
    )


# ============================================================
# DISTRIBUTION
# ============================================================

def distribution_metrics(values):

    if values is None or len(values) == 0:

        return {
            "p10": 0.0,
            "p25": 0.0,
            "p50": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "iqr": 0.0,
            "mad": 0.0,
            "mean": 0.0,
        }

    arr = np.asarray(
        values,
        dtype=np.float64
    )

    p10, p25, p50, p75, p90, p95, p99 = np.percentile(
        arr,
        [10, 25, 50, 75, 90, 95, 99]
    )

    median = np.median(arr)

    mad = np.median(
        np.abs(arr - median)
    )

    return {
        "p10": float(p10),
        "p25": float(p25),
        "p50": float(p50),
        "p75": float(p75),
        "p90": float(p90),
        "p95": float(p95),
        "p99": float(p99),
        "max": float(np.max(arr)),
        "iqr": float(p75 - p25),
        "mad": float(mad),
        "mean": float(np.mean(arr)),
    }


# ============================================================
# TEMPORAL MODEL
# ============================================================

def temporal_model(metrics):

    p50 = metrics["p50"]
    p90 = metrics["p90"]
    p99 = metrics["p99"]
    iqr = metrics["iqr"]
    maximum = metrics["max"]

    # Sustained activity:
    # robust relationship between central tendency and tail.
    sustained = clamp(
        p50 / max(p90, 1.0)
    )

    # Burst:
    burst_ratio = safe_div(
        p90,
        max(p50, 1.0)
    )

    burst = clamp(
        burst_ratio / 8.0
    )

    # Tail:
    tail_ratio = safe_div(
        p99,
        max(p90, 1.0)
    )

    tail = clamp(
        tail_ratio / 8.0
    )

    # Extreme:
    extreme_ratio = safe_div(
        maximum,
        max(p99, 1.0)
    )

    extreme = clamp(
        extreme_ratio / 8.0
    )

    dispersion = clamp(
        safe_div(
            iqr,
            max(p90, 1.0)
        )
    )

    rtci = clamp(
        0.45 * sustained
        + 0.20 * burst
        + 0.20 * tail
        + 0.15 * dispersion
    )

    return {
        "sustained_score": sustained,
        "burst_score": burst,
        "tail_score": tail,
        "extreme_score": extreme,
        "dispersion_score": dispersion,
        "rtci": rtci,
    }


# ============================================================
# SPATIAL MODEL
# ============================================================

def spatial_model(si_values):

    metrics = distribution_metrics(
        si_values
    )

    p50 = metrics["p50"]
    p90 = metrics["p90"]
    p99 = metrics["p99"]

    sustained = clamp(
        p50 / max(p90, 1e-9)
    )

    burst = clamp(
        safe_div(
            p90,
            max(p50, 1e-9)
        ) / 8.0
    )

    tail = clamp(
        safe_div(
            p99,
            max(p90, 1e-9)
        ) / 8.0
    )

    return {
        "si_p10": metrics["p10"],
        "si_p25": metrics["p25"],
        "si_p50": metrics["p50"],
        "si_p75": metrics["p75"],
        "si_p90": metrics["p90"],
        "si_p95": metrics["p95"],
        "si_p99": metrics["p99"],
        "si_max": metrics["max"],
        "si_iqr": metrics["iqr"],
        "si_mad": metrics["mad"],
        "si_sustained": sustained,
        "si_burst": burst,
        "si_tail": tail,
    }


# ============================================================
# EVENTS
# ============================================================

def detect_scene_changes(
    scene_scores,
    timestamps,
    threshold=SCENE_THRESHOLD,
):

    events = []

    for i, score in enumerate(scene_scores):

        if score >= threshold:

            timestamp = (
                timestamps[i]
                if i < len(timestamps)
                else 0.0
            )

            events.append({
                "index": i,
                "timestamp": float(timestamp),
                "score": float(score),
            })

    return events


def detect_extreme_events(
    medad_values,
    timestamps,
):

    events = []

    metrics = distribution_metrics(
        medad_values
    )

    p99 = metrics["p99"]

    if p99 <= 0:
        return events

    threshold = (
        p99 * EXTREME_MULTIPLIER
    )

    for i, value in enumerate(
        medad_values
    ):

        if value >= threshold:

            timestamp = (
                timestamps[i]
                if i < len(timestamps)
                else 0.0
            )

            events.append({
                "type": "EXTREME_TEMPORAL",
                "timestamp": float(timestamp),
                "medad": float(value),
                "severity": float(
                    safe_div(
                        value,
                        p99
                    )
                ),
            })

    return events


# ============================================================
# REGIME
# ============================================================

def classify_regime(
    spatial_score,
    temporal_score,
    burst_score,
    tail_score,
    motion_score,
):

    spatial_high = (
        spatial_score >= 0.60
    )

    temporal_high = (
        temporal_score >= 0.60
    )

    motion_high = (
        motion_score >= 0.60
    )

    burst_high = (
        burst_score >= 0.20
    )

    tail_high = (
        tail_score >= 0.20
    )

    if spatial_high and temporal_high:

        return "HIGH-SPATIO-TEMPORAL"

    if spatial_high and motion_high:

        return "SPATIAL-MOTION-HEAVY"

    if temporal_high and motion_high:

        return "TEMPORAL-MOTION-HEAVY"

    if spatial_high:

        return "SPATIAL-HEAVY"

    if temporal_high:

        return "MOTION-HEAVY"

    if tail_high:

        return "SPARSE-EXTREME"

    if burst_high:

        return "BURST-DOMINATED"

    return "LOW-COMPLEXITY"


def classify_level(score):

    if score < 0.15:
        return "Very Low"

    if score < 0.30:
        return "Low"

    if score < 0.50:
        return "Moderate"

    if score < 0.70:
        return "High"

    if score < 0.85:
        return "Very High"

    return "Extreme"


# ============================================================
# V8 FUSION
# ============================================================

def calculate_v8_fusion(
    temporal_score,
    spatial_score,
    motion_occupancy_score,
    motion_magnitude_score,
    burst_score,
    tail_score,
    extreme_score,
    event_score,
    texture_score,
    segment_variability_score,
):

    score = (

        # Temporal core
        0.25 * temporal_score

        # Spatial core
        + 0.20 * spatial_score

        # Motion occupancy
        + 0.10 * motion_occupancy_score

        # Motion magnitude
        + 0.10 * motion_magnitude_score

        # Burst / tail / extreme
        + 0.07 * burst_score
        + 0.07 * tail_score
        + 0.05 * extreme_score

        # Events
        + 0.06 * event_score

        # Texture
        + 0.05 * texture_score

        # Segment variability
        + 0.05 * segment_variability_score
    )

    return clamp(score)


# ============================================================
# BDI
# ============================================================

def calculate_bdi(
    fusion,
    temporal_score,
    spatial_score,
    motion_score,
    texture_score,
    tail_score,
):

    """
    Bit Demand Index.

    This is intentionally NOT equal to complexity.
    It emphasizes persistent content + motion + texture.
    """

    score = (

        0.30 * fusion

        + 0.20 * temporal_score

        + 0.18 * spatial_score

        + 0.17 * motion_score

        + 0.10 * texture_score

        + 0.05 * tail_score
    )

    return clamp(score)


# ============================================================
# SEGMENT
# ============================================================

def analyze_segment(
    medad_values,
    si_values,
    occupancy_values,
    magnitude_values,
    scene_values,
    texture_values,
    segment_id,
    start_time,
    end_time,
):

    medad = distribution_metrics(
        medad_values
    )

    spatial = spatial_model(
        si_values
    )

    temporal = temporal_model(
        medad
    )

    occupancy = (
        float(np.mean(
            occupancy_values
        ))
        if occupancy_values
        else 0.0
    )

    magnitude = (
        float(np.mean(
            magnitude_values
        ))
        if magnitude_values
        else 0.0
    )

    scene = (
        float(np.mean(
            scene_values
        ))
        if scene_values
        else 0.0
    )

    texture = (
        float(np.mean(
            texture_values
        ))
        if texture_values
        else 0.0
    )

    # Robust local normalization
    occupancy_score = clamp(
        occupancy
    )

    magnitude_score, _ = robust_score(
        magnitude_values,
        magnitude
    )

    texture_score, _ = robust_score(
        texture_values,
        texture
    )

    temporal_score = clamp(
        0.55 * temporal["rtci"]
        + 0.45 * temporal["burst_score"]
    )

    spatial_score = clamp(
        0.60 * spatial["si_sustained"]
        + 0.25 * spatial["si_burst"]
        + 0.15 * spatial["si_tail"]
    )

    event_score = clamp(
        scene / SCENE_THRESHOLD
    )

    segment_variability = clamp(
        safe_div(
            medad["iqr"],
            max(medad["p90"], 1.0)
        )
    )

    motion_score = clamp(
        0.55 * occupancy_score
        + 0.45 * magnitude_score
    )

    fusion = calculate_v8_fusion(

        temporal_score,
        spatial_score,

        occupancy_score,
        magnitude_score,

        temporal["burst_score"],
        temporal["tail_score"],
        temporal["extreme_score"],

        event_score,
        texture_score,
        segment_variability,
    )

    bdi = calculate_bdi(
        fusion,
        temporal_score,
        spatial_score,
        motion_score,
        texture_score,
        temporal["tail_score"],
    )

    regime = classify_regime(
        spatial_score,
        temporal_score,
        temporal["burst_score"],
        temporal["tail_score"],
        motion_score,
    )

    return {

        "segment": segment_id,

        "start": float(start_time),
        "end": float(end_time),

        "medad_p50": medad["p50"],
        "medad_p90": medad["p90"],
        "medad_p99": medad["p99"],

        "si_p50": spatial["si_p50"],
        "si_p90": spatial["si_p90"],
        "si_p99": spatial["si_p99"],

        "motion_occupancy": occupancy,
        "motion_magnitude": magnitude,

        "scene_activity": scene,
        "texture": texture,

        "rtci": temporal["rtci"],
        "burst_score": temporal["burst_score"],
        "tail_score": temporal["tail_score"],
        "extreme_score": temporal["extreme_score"],

        "spatial_score": spatial_score,
        "temporal_score": temporal_score,

        "motion_score": motion_score,

        "event_score": event_score,

        "segment_variability": segment_variability,

        "v8_fusion": fusion,

        "bdi": bdi,

        "regime": regime,

        "class": classify_level(fusion),
    }


# ============================================================
# VIDEO ANALYSIS
# ============================================================

def analyze_video(
    video_path,
    target_fps=DEFAULT_FPS,
    analysis_width=DEFAULT_WIDTH,
    segment_seconds=DEFAULT_SEGMENT_SECONDS,
):

    cap = cv2.VideoCapture(
        str(video_path)
    )

    if not cap.isOpened():

        raise RuntimeError(
            f"Cannot open video: {video_path}"
        )

    source_fps = float(
        cap.get(cv2.CAP_PROP_FPS)
    ) or 25.0

    frame_count = int(
        cap.get(
            cv2.CAP_PROP_FRAME_COUNT
        )
    )

    source_width = int(
        cap.get(
            cv2.CAP_PROP_FRAME_WIDTH
        )
    )

    source_height = int(
        cap.get(
            cv2.CAP_PROP_FRAME_HEIGHT
        )
    )

    duration = (
        frame_count / source_fps
        if source_fps > 0
        else 0.0
    )

    sample_interval = (
        1.0 / target_fps
    )

    info(
        f"Input: {video_path}"
    )

    info(
        f"Source: "
        f"{source_width}×{source_height} "
        f"@ {source_fps:.3f} FPS"
    )

    info(
        f"Analysis: "
        f"{analysis_width}px / "
        f"{target_fps:.2f} FPS"
    )

    medad_values = []
    si_values = []
    occupancy_values = []
    magnitude_values = []
    scene_values = []
    texture_values = []
    timestamps = []

    sample_rows = []
    segment_rows = []

    current_segment_id = 0

    current_segment = {
        "medad": [],
        "si": [],
        "occupancy": [],
        "magnitude": [],
        "scene": [],
        "texture": [],
    }

    next_sample_time = 0.0

    previous_gray = None

    processed_frames = 0

    start_wall = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn(
            "[progress.description]{task.description}"
        ),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TextColumn(
            "{task.fields[samples]} samples"
        ),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:

        task = progress.add_task(
            "Analyzing",
            total=(
                frame_count
                if frame_count > 0
                else None
            ),
            samples=0,
        )

        while True:

            ret, frame = cap.read()

            if not ret:
                break

            processed_frames += 1

            timestamp = (
                cap.get(
                    cv2.CAP_PROP_POS_MSEC
                )
                / 1000.0
            )

            if (
                timestamp + 1e-9
                < next_sample_time
            ):

                progress.update(
                    task,
                    advance=1,
                )

                continue

            if analysis_width > 0:

                h, w = frame.shape[:2]

                if w != analysis_width:

                    scale = (
                        analysis_width / w
                    )

                    new_h = max(
                        1,
                        int(h * scale)
                    )

                    frame = cv2.resize(
                        frame,
                        (
                            analysis_width,
                            new_h,
                        ),
                        interpolation=cv2.INTER_AREA,
                    )

            gray = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2GRAY
            )

            if previous_gray is not None:

                # --------------------------------------------
                # CORE METRICS
                # --------------------------------------------

                medad = calculate_medad(
                    previous_gray,
                    gray
                )

                si = calculate_si(
                    gray
                )

                occupancy = (
                    calculate_motion_occupancy(
                        previous_gray,
                        gray
                    )
                )

                magnitude = (
                    calculate_motion_magnitude(
                        previous_gray,
                        gray
                    )
                )

                scene = (
                    calculate_scene_activity(
                        previous_gray,
                        gray
                    )
                )

                texture = calculate_texture(
                    gray
                )

                # --------------------------------------------
                # GLOBAL
                # --------------------------------------------

                medad_values.append(
                    medad
                )

                si_values.append(
                    si
                )

                occupancy_values.append(
                    occupancy
                )

                magnitude_values.append(
                    magnitude
                )

                scene_values.append(
                    scene
                )

                texture_values.append(
                    texture
                )

                timestamps.append(
                    timestamp
                )

                # --------------------------------------------
                # SEGMENT
                # --------------------------------------------

                segment_id = int(
                    timestamp
                    // segment_seconds
                )

                if segment_id != current_segment_id:

                    if current_segment["medad"]:

                        segment_rows.append(
                            analyze_segment(
                                current_segment["medad"],
                                current_segment["si"],
                                current_segment["occupancy"],
                                current_segment["magnitude"],
                                current_segment["scene"],
                                current_segment["texture"],
                                current_segment_id,
                                current_segment_id * segment_seconds,
                                timestamp,
                            )
                        )

                    current_segment_id = (
                        segment_id
                    )

                    current_segment = {
                        "medad": [],
                        "si": [],
                        "occupancy": [],
                        "magnitude": [],
                        "scene": [],
                        "texture": [],
                    }

                current_segment["medad"].append(
                    medad
                )

                current_segment["si"].append(
                    si
                )

                current_segment["occupancy"].append(
                    occupancy
                )

                current_segment["magnitude"].append(
                    magnitude
                )

                current_segment["scene"].append(
                    scene
                )

                current_segment["texture"].append(
                    texture
                )

                # --------------------------------------------
                # SAMPLE RECORD
                # --------------------------------------------

                sample_rows.append({

                    "timestamp": timestamp,

                    "medad": medad,

                    "si": si,

                    "motion_occupancy":
                        occupancy,

                    "motion_magnitude":
                        magnitude,

                    "scene_activity":
                        scene,

                    "texture":
                        texture,

                    "segment":
                        segment_id,
                })

            previous_gray = gray.copy()

            next_sample_time += (
                sample_interval
            )

            progress.update(
                task,
                advance=1,
                samples=len(medad_values),
            )

    cap.release()

    # Final segment
    if current_segment["medad"]:

        segment_rows.append(
            analyze_segment(
                current_segment["medad"],
                current_segment["si"],
                current_segment["occupancy"],
                current_segment["magnitude"],
                current_segment["scene"],
                current_segment["texture"],
                current_segment_id,
                current_segment_id
                * segment_seconds,
                duration,
            )
        )

    processing_time = (
        time.time() - start_wall
    )

    # ========================================================
    # GLOBAL MODELS
    # ========================================================

    medad = distribution_metrics(
        medad_values
    )

    spatial = spatial_model(
        si_values
    )

    temporal = temporal_model(
        medad
    )

    occupancy_mean = (
        float(
            np.mean(
                occupancy_values
            )
        )
        if occupancy_values
        else 0.0
    )

    magnitude_mean = (
        float(
            np.mean(
                magnitude_values
            )
        )
        if magnitude_values
        else 0.0
    )

    texture_mean = (
        float(
            np.mean(
                texture_values
            )
        )
        if texture_values
        else 0.0
    )

    # ========================================================
    # ROBUST SCORES
    # ========================================================

    occupancy_score = clamp(
        occupancy_mean
    )

    magnitude_score, magnitude_stats = (
        robust_score(
            magnitude_values,
            magnitude_mean
        )
    )

    texture_score, texture_stats = (
        robust_score(
            texture_values,
            texture_mean
        )
    )

    # ========================================================
    # TEMPORAL SCORE
    # ========================================================

    temporal_score = clamp(

        0.45 * temporal["rtci"]

        + 0.20
        * temporal["burst_score"]

        + 0.20
        * temporal["tail_score"]

        + 0.15
        * temporal["extreme_score"]
    )

    # ========================================================
    # SPATIAL SCORE
    # ========================================================

    spatial_score = clamp(

        0.55
        * spatial["si_sustained"]

        + 0.25
        * spatial["si_burst"]

        + 0.20
        * spatial["si_tail"]
    )

    # ========================================================
    # MOTION SCORE
    # ========================================================

    motion_score = clamp(

        0.55
        * occupancy_score

        + 0.45
        * magnitude_score
    )

    # ========================================================
    # SCENE / EVENT
    # ========================================================

    scene_events = detect_scene_changes(
        scene_values,
        timestamps,
    )

    scene_count = len(
        scene_events
    )

    scene_per_minute = safe_div(
        scene_count,
        duration / 60.0
    )

    event_score = clamp(
        scene_per_minute / 30.0
    )

    # ========================================================
    # SEGMENT VARIABILITY
    # ========================================================

    segment_fusions = [
        s["v8_fusion"]
        for s in segment_rows
    ]

    if len(segment_fusions) > 1:

        segment_variability = clamp(
            float(
                np.std(
                    segment_fusions
                )
            ) * 3.0
        )

    else:

        segment_variability = 0.0

    # ========================================================
    # V8 FUSION
    # ========================================================

    v8_fusion = calculate_v8_fusion(

        temporal_score,

        spatial_score,

        occupancy_score,

        magnitude_score,

        temporal["burst_score"],

        temporal["tail_score"],

        temporal["extreme_score"],

        event_score,

        texture_score,

        segment_variability,
    )

    # ========================================================
    # BDI
    # ========================================================

    bdi = calculate_bdi(

        v8_fusion,

        temporal_score,

        spatial_score,

        motion_score,

        texture_score,

        temporal["tail_score"],
    )

    # ========================================================
    # REGIME
    # ========================================================

    regime = classify_regime(

        spatial_score,

        temporal_score,

        temporal["burst_score"],

        temporal["tail_score"],

        motion_score,
    )

    # ========================================================
    # EXTREME EVENTS
    # ========================================================

    extreme_events = detect_extreme_events(
        medad_values,
        timestamps,
    )

    events = []

    for ev in extreme_events:

        events.append(ev)

    for ev in scene_events:

        events.append({

            "type":
                "SCENE_ACTIVITY",

            "timestamp":
                ev["timestamp"],

            "medad":
                0.0,

            "severity":
                ev["score"],
        })

    events.sort(
        key=lambda x:
        x["timestamp"]
    )

    # ========================================================
    # RESULT
    # ========================================================

    result = {

        "version":
            VERSION,

        "video":
            str(video_path),

        "resolution": {
            "width":
                source_width,

            "height":
                source_height,
        },

        "source_fps":
            source_fps,

        "analysis_fps":
            target_fps,

        "frame_count":
            frame_count,

        "duration":
            duration,

        "analysis_width":
            analysis_width,

        "sample_count":
            len(medad_values),

        # --------------------------------------------
        # DISTRIBUTIONS
        # --------------------------------------------

        "medad":
            medad,

        "spatial":
            spatial,

        # --------------------------------------------
        # MOTION
        # --------------------------------------------

        "motion_occupancy":
            occupancy_mean,

        "motion_magnitude":
            magnitude_mean,

        "motion_score":
            motion_score,

        "motion_normalization":
            magnitude_stats,

        # --------------------------------------------
        # TEXTURE
        # --------------------------------------------

        "texture_mean":
            texture_mean,

        "texture_score":
            texture_score,

        "texture_normalization":
            texture_stats,

        # --------------------------------------------
        # EVENTS
        # --------------------------------------------

        "scene_changes":
            scene_count,

        "scene_changes_per_minute":
            scene_per_minute,

        "event_score":
            event_score,

        # --------------------------------------------
        # INTELLIGENCE
        # --------------------------------------------

        "temporal":
            temporal,

        "spatial_score":
            spatial_score,

        "temporal_score":
            temporal_score,

        "segment_variability":
            segment_variability,

        "v8_fusion_score":
            v8_fusion,

        "bit_demand_index":
            bdi,

        "content_regime":
            regime,

        "complexity_class":
            classify_level(
                v8_fusion
            ),

        # --------------------------------------------
        # META
        # --------------------------------------------

        "event_count":
            len(events),

        "processing_seconds":
            processing_time,

        "samples":
            sample_rows,

        "segments":
            segment_rows,

        "events":
            events,
    }

    return result


# ============================================================
# DASHBOARD
# ============================================================

def create_dashboard(
    result,
    output_path,
):

    samples = result["samples"]

    segments = result["segments"]

    if not samples:

        warning(
            "No samples to plot."
        )

        return

    ts = np.array([
        s["timestamp"]
        for s in samples
    ])

    medad = np.array([
        s["medad"]
        for s in samples
    ])

    si = np.array([
        s["si"]
        for s in samples
    ])

    occupancy = np.array([
        s["motion_occupancy"]
        for s in samples
    ])

    magnitude = np.array([
        s["motion_magnitude"]
        for s in samples
    ])

    texture = np.array([
        s["texture"]
        for s in samples
    ])

    fig = plt.figure(
        figsize=(18, 12),
        dpi=160,
    )

    fig.suptitle(
        "ASTCIE V8.1 ULTIMATE  •  "
        f"{Path(result['video']).name}",
        fontsize=16,
        fontweight="bold",
        color=COLORS["cyan"],
        y=0.98,
    )

    gs = GridSpec(
        3,
        3,
        figure=fig,
        hspace=0.32,
        wspace=0.28,
        left=0.06,
        right=0.97,
        top=0.92,
        bottom=0.06,
    )

    # ========================================================
    # MEDAD
    # ========================================================

    ax1 = fig.add_subplot(
        gs[0, :2]
    )

    ax1.plot(
        ts,
        medad,
        color=COLORS["cyan"],
        alpha=0.9,
    )

    ax1.fill_between(
        ts,
        medad,
        alpha=0.15,
        color=COLORS["cyan"],
    )

    ax1.axhline(
        result["medad"]["p50"],
        color=COLORS["green"],
        linestyle="--",
        label="P50",
    )

    ax1.axhline(
        result["medad"]["p90"],
        color=COLORS["yellow"],
        linestyle="--",
        label="P90",
    )

    ax1.axhline(
        result["medad"]["p99"],
        color=COLORS["magenta"],
        linestyle="--",
        label="P99",
    )

    ax1.set_title(
        "Temporal Complexity — MedAD"
    )

    ax1.set_ylabel(
        "MedAD"
    )

    ax1.legend(
        framealpha=0.3
    )

    ax1.grid(
        True,
        alpha=0.4
    )

    # ========================================================
    # SCORE PANEL
    # ========================================================

    ax2 = fig.add_subplot(
        gs[0, 2]
    )

    labels = [
        "Spatial",
        "Temporal",
        "Occupancy",
        "Magnitude",
        "Burst",
        "Tail",
        "Fusion",
        "BDI",
    ]

    values = [
        result["spatial_score"],
        result["temporal_score"],
        result["motion_occupancy"],
        robust_normalize(
            result["motion_magnitude"],
            result["motion_normalization"]["p10"],
            result["motion_normalization"]["p90"],
        ),
        result["temporal"]["burst_score"],
        result["temporal"]["tail_score"],
        result["v8_fusion_score"],
        result["bit_demand_index"],
    ]

    y = np.arange(
        len(labels)
    )

    bars = ax2.barh(
        y,
        values,
        color=[
            COLORS["blue"],
            COLORS["cyan"],
            COLORS["orange"],
            COLORS["yellow"],
            COLORS["purple"],
            COLORS["magenta"],
            COLORS["red"],
            COLORS["green"],
        ],
        edgecolor=COLORS["white"],
        linewidth=0.6,
    )

    ax2.set_yticks(y)
    ax2.set_yticklabels(labels)

    ax2.set_xlim(
        0,
        1.05
    )

    ax2.set_title(
        "V8 Intelligence"
    )

    for bar, value in zip(
        bars,
        values,
    ):

        ax2.text(
            value + 0.015,
            bar.get_y()
            + bar.get_height() / 2,
            f"{value:.3f}",
            va="center",
            fontsize=8,
        )

    ax2.grid(
        True,
        axis="x",
        alpha=0.4,
    )

    # ========================================================
    # SI
    # ========================================================

    ax3 = fig.add_subplot(
        gs[1, 0]
    )

    ax3.plot(
        ts,
        si,
        color=COLORS["blue"],
    )

    ax3.fill_between(
        ts,
        si,
        alpha=0.18,
        color=COLORS["blue"],
    )

    ax3.set_title(
        "Spatial Information"
    )

    ax3.set_ylabel(
        "SI"
    )

    ax3.grid(
        True,
        alpha=0.4,
    )

    # ========================================================
    # MOTION
    # ========================================================

    ax4 = fig.add_subplot(
        gs[1, 1]
    )

    ax4.plot(
        ts,
        occupancy * 100,
        color=COLORS["orange"],
        label="Occupancy %",
    )

    ax4.set_title(
        "Motion Occupancy"
    )

    ax4.set_ylabel(
        "%"
    )

    ax4.grid(
        True,
        alpha=0.4,
    )

    # ========================================================
    # MOTION MAGNITUDE
    # ========================================================

    ax5 = fig.add_subplot(
        gs[1, 2]
    )

    ax5.plot(
        ts,
        magnitude,
        color=COLORS["yellow"],
    )

    ax5.set_title(
        "Motion Magnitude"
    )

    ax5.set_ylabel(
        "Mean Δ"
    )

    ax5.grid(
        True,
        alpha=0.4,
    )

    # ========================================================
    # SEGMENTS
    # ========================================================

    ax6 = fig.add_subplot(
        gs[2, :2]
    )

    if segments:

        ids = [
            s["segment"]
            for s in segments
        ]

        fusions = [
            s["v8_fusion"]
            for s in segments
        ]

        bar_colors = []

        for value in fusions:

            if value < 0.30:
                bar_colors.append(
                    COLORS["green"]
                )

            elif value < 0.50:
                bar_colors.append(
                    COLORS["yellow"]
                )

            elif value < 0.70:
                bar_colors.append(
                    COLORS["orange"]
                )

            else:
                bar_colors.append(
                    COLORS["red"]
                )

        ax6.bar(
            ids,
            fusions,
            color=bar_colors,
            width=0.8,
        )

        ax6.axhline(
            result["v8_fusion_score"],
            color=COLORS["cyan"],
            linestyle="--",
            label="Global V8",
        )

        ax6.set_title(
            "Segment Complexity"
        )

        ax6.set_xlabel(
            "Segment"
        )

        ax6.set_ylabel(
            "Fusion"
        )

        ax6.set_ylim(
            0,
            1.05
        )

        ax6.legend(
            framealpha=0.3
        )

        ax6.grid(
            True,
            axis="y",
            alpha=0.4,
        )

    # ========================================================
    # SUMMARY
    # ========================================================

    ax7 = fig.add_subplot(
        gs[2, 2]
    )

    ax7.axis(
        "off"
    )

    text = (

        f"DURATION     "
        f"{fmt_time(result['duration'])}\n"

        f"SAMPLES      "
        f"{result['sample_count']}\n"

        f"SCENE Δ      "
        f"{result['scene_changes']}\n"

        f"SCENE/MIN    "
        f"{result['scene_changes_per_minute']:.2f}\n\n"

        f"MOTION       "
        f"{result['motion_score']:.4f}\n"

        f"REGIME\n"
        f"{result['content_regime']}\n\n"

        f"V8 COMPLEXITY\n"
        f"{result['v8_fusion_score']:.4f}\n"

        f"{result['complexity_class']}\n\n"

        f"BDI\n"
        f"{result['bit_demand_index']:.4f}"
    )

    ax7.text(
        0.05,
        0.95,
        text,
        transform=ax7.transAxes,
        fontsize=10,
        verticalalignment="top",
        fontfamily="monospace",
        bbox=dict(
            boxstyle="round,pad=0.6",
            facecolor=COLORS["panel"],
            edgecolor=COLORS["cyan"],
            linewidth=1.5,
        ),
    )

    ensure_parent(
        output_path
    )

    fig.savefig(
        output_path,
        dpi=160,
        bbox_inches="tight",
        facecolor=COLORS["bg"],
    )

    plt.close(fig)

    success(
        f"Dashboard → {output_path}"
    )


# ============================================================
# DISTRIBUTION
# ============================================================

def create_distribution_plot(
    result,
    output_path,
):

    samples = result["samples"]

    if not samples:
        return

    medad = np.array([
        s["medad"]
        for s in samples
    ])

    si = np.array([
        s["si"]
        for s in samples
    ])

    occupancy = np.array([
        s["motion_occupancy"]
        for s in samples
    ])

    fig, axes = plt.subplots(
        2,
        2,
        figsize=(14, 9),
        dpi=150,
    )

    fig.suptitle(
        "ASTCIE V8.1 Distribution Intelligence",
        fontsize=14,
        fontweight="bold",
        color=COLORS["cyan"],
    )

    # MedAD
    axes[0, 0].hist(
        medad,
        bins=50,
        color=COLORS["cyan"],
        alpha=0.9,
    )

    axes[0, 0].axvline(
        result["medad"]["p50"],
        color=COLORS["green"],
        linestyle="--",
    )

    axes[0, 0].axvline(
        result["medad"]["p90"],
        color=COLORS["yellow"],
        linestyle="--",
    )

    axes[0, 0].axvline(
        result["medad"]["p99"],
        color=COLORS["magenta"],
        linestyle="--",
    )

    axes[0, 0].set_title(
        "MedAD Distribution"
    )

    axes[0, 0].grid(
        True,
        alpha=0.4,
    )

    # SI
    axes[0, 1].hist(
        si,
        bins=50,
        color=COLORS["blue"],
        alpha=0.9,
    )

    axes[0, 1].set_title(
        "Spatial Information"
    )

    axes[0, 1].grid(
        True,
        alpha=0.4,
    )

    # Occupancy
    axes[1, 0].hist(
        occupancy * 100,
        bins=50,
        color=COLORS["orange"],
        alpha=0.9,
    )

    axes[1, 0].set_title(
        "Motion Occupancy (%)"
    )

    axes[1, 0].grid(
        True,
        alpha=0.4,
    )

    # Summary
    axes[1, 1].axis(
        "off"
    )

    box = (

        "ASTCIE V8.1\n\n"

        f"MedAD P50   "
        f"{result['medad']['p50']:.3f}\n"

        f"MedAD P90   "
        f"{result['medad']['p90']:.3f}\n"

        f"MedAD P99   "
        f"{result['medad']['p99']:.3f}\n"

        f"MedAD IQR   "
        f"{result['medad']['iqr']:.3f}\n\n"

        f"SI P50      "
        f"{result['spatial']['si_p50']:.3f}\n"

        f"SI P90      "
        f"{result['spatial']['si_p90']:.3f}\n"

        f"SI P99      "
        f"{result['spatial']['si_p99']:.3f}\n\n"

        f"Motion      "
        f"{result['motion_score']:.3f}\n"

        f"Fusion      "
        f"{result['v8_fusion_score']:.3f}\n"

        f"BDI         "
        f"{result['bit_demand_index']:.3f}"
    )

    axes[1, 1].text(
        0.1,
        0.95,
        box,
        transform=axes[1, 1].transAxes,
        fontsize=11,
        verticalalignment="top",
        fontfamily="monospace",
        bbox=dict(
            boxstyle="round,pad=0.5",
            facecolor=COLORS["panel"],
            edgecolor=COLORS["magenta"],
            linewidth=1.5,
        ),
    )

    plt.tight_layout(
        rect=[
            0,
            0,
            1,
            0.95,
        ]
    )

    ensure_parent(
        output_path
    )

    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
        facecolor=COLORS["bg"],
    )

    plt.close(fig)

    success(
        f"Distribution → {output_path}"
    )


# ============================================================
# REPORT
# ============================================================

def print_report(result):

    console.print()

    table = Table(
        title="V8.1 Spatial / Temporal / Motion Intelligence"
    )

    table.add_column(
        "Metric",
        style="cyan"
    )

    table.add_column(
        "Value",
        justify="right"
    )

    table.add_row(
        "SI P50",
        f"{result['spatial']['si_p50']:.4f}"
    )

    table.add_row(
        "SI P90",
        f"{result['spatial']['si_p90']:.4f}"
    )

    table.add_row(
        "SI P99",
        f"{result['spatial']['si_p99']:.4f}"
    )

    table.add_row(
        "MedAD P50",
        f"{result['medad']['p50']:.4f}"
    )

    table.add_row(
        "MedAD P90",
        f"{result['medad']['p90']:.4f}"
    )

    table.add_row(
        "MedAD P99",
        f"{result['medad']['p99']:.4f}"
    )

    table.add_row(
        "MedAD IQR",
        f"{result['medad']['iqr']:.4f}"
    )

    table.add_row(
        "Motion Occupancy",
        f"{result['motion_occupancy'] * 100:.2f}%"
    )

    table.add_row(
        "Motion Magnitude",
        f"{result['motion_magnitude']:.4f}"
    )

    table.add_row(
        "Texture Mean",
        f"{result['texture_mean']:.2f}"
    )

    table.add_row(
        "Scene Changes",
        str(result["scene_changes"])
    )

    table.add_row(
        "Scene / min",
        f"{result['scene_changes_per_minute']:.3f}"
    )

    console.print(table)

    # --------------------------------------------------------
    # Intelligence
    # --------------------------------------------------------

    table2 = Table(
        title="V8.1 Intelligence Layer"
    )

    table2.add_column(
        "Component",
        style="green"
    )

    table2.add_column(
        "Score",
        justify="right"
    )

    table2.add_row(
        "RTCI",
        f"{result['temporal']['rtci']:.4f}"
    )

    table2.add_row(
        "Burst",
        f"{result['temporal']['burst_score']:.4f}"
    )

    table2.add_row(
        "Tail",
        f"{result['temporal']['tail_score']:.4f}"
    )

    table2.add_row(
        "Extreme",
        f"{result['temporal']['extreme_score']:.4f}"
    )

    table2.add_row(
        "Spatial Score",
        f"{result['spatial_score']:.4f}"
    )

    table2.add_row(
        "Temporal Score",
        f"{result['temporal_score']:.4f}"
    )

    table2.add_row(
        "Motion Score",
        f"{result['motion_score']:.4f}"
    )

    table2.add_row(
        "Event Score",
        f"{result['event_score']:.4f}"
    )

    table2.add_row(
        "V8 Fusion",
        f"{result['v8_fusion_score']:.4f}"
    )

    table2.add_row(
        "BDI",
        f"{result['bit_demand_index']:.4f}"
    )

    console.print(table2)

    # --------------------------------------------------------
    # Profile
    # --------------------------------------------------------

    level = result[
        "complexity_class"
    ]

    color = {

        "Very Low":
            "green",

        "Low":
            "green",

        "Moderate":
            "yellow",

        "High":
            "yellow",

        "Very High":
            "red",

        "Extreme":
            "bold red",

    }.get(
        level,
        "white"
    )

    profile = Text()

    profile.append(
        "CONTENT REGIME\n\n",
        style="bold cyan"
    )

    profile.append(
        f"{result['content_regime']}\n\n",
        style="bold magenta"
    )

    profile.append(
        "V8.1 COMPLEXITY\n\n",
        style="bold cyan"
    )

    profile.append(
        f"{result['v8_fusion_score']:.4f}\n",
        style="bold white"
    )

    profile.append(
        f"{level}\n",
        style=color
    )

    profile.append(
        "\n\nBIT DEMAND INDEX\n\n",
        style="bold cyan"
    )

    profile.append(
        f"{result['bit_demand_index']:.4f}\n",
        style="bold white"
    )

    profile.append(
        "\nExperimental content-demand proxy — "
        "not bitrate prediction.",
        style="dim"
    )

    console.print(
        Panel(
            profile,
            border_style="magenta",
            padding=(1, 3),
        )
    )

    success(
        f"Processed "
        f"{result['sample_count']} samples "
        f"in "
        f"{result['processing_seconds']:.2f}s"
    )


# ============================================================
# SAVE
# ============================================================

def save_csv(
    path,
    rows,
):

    if not rows:
        return

    ensure_parent(
        path
    )

    fieldnames = list(
        rows[0].keys()
    )

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        writer.writerows(
            rows
        )

    success(
        f"CSV → {path}"
    )


def save_json(
    path,
    data,
):

    ensure_parent(
        path
    )

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    success(
        f"JSON → {path}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=
        "ASTCIE V8.1 Ultimate"
    )

    parser.add_argument(
        "video",
        nargs="?",
        help="Input video",
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS,
    )

    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
    )

    parser.add_argument(
        "--segment",
        type=float,
        default=DEFAULT_SEGMENT_SECONDS,
    )

    args = parser.parse_args()

    if not args.video:

        print(
            "Usage:\n"
            "  python astcie_v8.py video.mp4\n\n"
            "Example:\n"
            "  python astcie_v8.py TV8.mp4 "
            "--fps 25 --width 640"
        )

        return 1

    print_banner()

    try:

        result = analyze_video(

            args.video,

            target_fps=args.fps,

            analysis_width=args.width,

            segment_seconds=args.segment,
        )

        print_report(
            result
        )

        # ----------------------------------------------------
        # OUTPUT
        # ----------------------------------------------------

        out_dir = (
            Path("results")
            / Path(args.video).stem
        )

        out_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # Dashboard
        create_dashboard(
            result,
            str(
                out_dir
                / "dashboard_v8.png"
            ),
        )

        # Distribution
        create_distribution_plot(
            result,
            str(
                out_dir
                / "distribution_v8.png"
            ),
        )

        # CSV
        save_csv(
            str(
                out_dir
                / "samples.csv"
            ),
            result["samples"],
        )

        save_csv(
            str(
                out_dir
                / "segments.csv"
            ),
            result["segments"],
        )

        save_csv(
            str(
                out_dir
                / "events.csv"
            ),
            result["events"],
        )

        # JSON
        json_result = {
            k: v
            for k, v in result.items()
            if k not in (
                "samples",
                "segments",
                "events",
            )
        }

        save_json(
            str(
                out_dir
                / "result.json"
            ),
            json_result,
        )

        console.print(
            Panel(
                "[bold green]"
                "V8.1 ULTIMATE COMPLETE"
                "[/bold green]\n\n"

                "• Robust MedAD\n"
                "• Spatial Information\n"
                "• Motion Occupancy\n"
                "• Motion Magnitude\n"
                "• Burst / Tail / Extreme\n"
                "• Event Intelligence\n"
                "• Texture Intelligence\n"
                "• Fixed Segment Analysis\n"
                "• Robust Normalization\n"
                "• V8 Fusion\n"
                "• Independent BDI\n"
                "• Graphical Dashboard\n"
                "• Distribution Analysis\n"
                "• CSV + JSON Export\n\n"

                f"[cyan]Folder → "
                f"{out_dir}[/cyan]",
                border_style="green",
                padding=(1, 3),
            )
        )

        return 0

    except Exception as exc:

        error(
            str(exc)
        )

        import traceback

        traceback.print_exc()

        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )