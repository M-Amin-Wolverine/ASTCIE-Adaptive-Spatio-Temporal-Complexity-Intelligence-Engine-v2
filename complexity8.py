#!/usr/bin/env python3
"""
ASTCIE V9 ULTIMATE
Adaptive Spatio-Temporal Complexity Intelligence Engine

V4  Robust MedAD Distribution
V5  Burst / Tail / Extreme Intelligence
V6  Event / Risk / Profile Intelligence
V7  Spatial + Temporal Unified Intelligence
V8  Graphical Dashboard + Sharp Visuals
V9  Distributional Intelligence / ADIM / ADCI

Core:
    ADIM-V1 — Adaptive Distributional Intelligence Model
    ADCI     — Adaptive Distributional Complexity Index

Design goals:
    - Robust distributional statistics
    - Adaptive quantiles
    - Robust shape analysis
    - Hybrid tail intelligence
    - Hill-like tail estimator
    - Sustained / Dynamic / Risk separation
    - Spatial + temporal fusion
    - Segment intelligence
    - Batch comparison
    - V8 graphical outputs retained
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

VERSION = "9.0.0-ULTIMATE"
ADIM_VERSION = "ADIM-V1"

DEFAULT_FPS = 25.0
DEFAULT_WIDTH = 640
DEFAULT_SEGMENT_SECONDS = 5.0

SCENE_THRESHOLD = 0.35
EXTREME_MULTIPLIER = 3.0

EPS = 1e-9

console = Console()


# ============================================================
# COLORS
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
    return float(a) / float(b) if abs(float(b)) > EPS else 0.0


def percentile(values, p):
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def normalize(value, low, high):
    if high <= low:
        return 0.0
    return clamp((value - low) / (high - low))


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


def finite_or_zero(x):
    return float(x) if np.isfinite(x) else 0.0


# ============================================================
# CONSOLE
# ============================================================

def print_banner():
    body = (
        "[bold cyan]ASTCIE V9 ULTIMATE[/bold cyan]\n"
        "[dim]Adaptive Spatio-Temporal Complexity Intelligence Engine[/dim]\n\n"
        "[yellow]V4[/yellow]  Robust MedAD Distribution\n"
        "[yellow]V5[/yellow]  Burst / Tail / Extreme Intelligence\n"
        "[yellow]V6[/yellow]  Event / Risk / Profile Intelligence\n"
        "[yellow]V7[/yellow]  Spatial + Temporal Fusion\n"
        "[yellow]V8[/yellow]  Graphical Dashboard + Sharp Visuals\n"
        "[bold green]V9[/bold green]  Distributional Intelligence / ADIM / ADCI"
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
# VIDEO METRICS
# ============================================================

def calculate_medad(frame1, frame2):
    diff = cv2.absdiff(frame1, frame2)
    return float(np.median(diff))


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


def calculate_changed_percentage(
    frame1,
    frame2,
    threshold=8,
):
    diff = cv2.absdiff(frame1, frame2)

    changed = diff > threshold

    return float(
        np.count_nonzero(changed)
        / changed.size
        * 100.0
    )


def calculate_scene_score(frame1, frame2):
    diff = cv2.absdiff(frame1, frame2)

    return float(
        np.mean(diff) / 255.0
    )


def calculate_laplacian_var(gray):
    return float(
        cv2.Laplacian(
            gray,
            cv2.CV_64F,
        ).var()
    )


def calculate_color_entropy(frame):
    hsv = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2HSV,
    )

    hist = cv2.calcHist(
        [hsv],
        [2],
        None,
        [64],
        [0, 256],
    )

    hist = hist.ravel()

    hist /= hist.sum() + EPS

    hist = hist[hist > 0]

    return float(
        -np.sum(hist * np.log2(hist))
    )


# ============================================================
# ADAPTIVE QUANTILES
# ============================================================

def adaptive_quantile_set(n):
    """
    Adaptive quantile selection.

    Short sequences:
        avoid unstable P99.

    Medium sequences:
        standard P99.

    Long sequences:
        add P99.5 / P99.9.
    """

    if n < 100:
        return [
            10, 25, 50, 75, 90, 95
        ]

    if n < 300:
        return [
            10, 25, 50, 75, 90, 95
        ]

    if n < 5000:
        return [
            10, 25, 50, 75,
            90, 95, 97.5, 99
        ]

    return [
        10, 25, 50, 75,
        90, 95, 97.5,
        99, 99.5, 99.9
    ]


def adaptive_quantiles(values):
    if not values:
        return {}

    arr = np.asarray(
        values,
        dtype=np.float64,
    )

    qs = adaptive_quantile_set(
        len(arr)
    )

    return {
        f"p{str(q).replace('.', '_')}":
            float(np.percentile(arr, q))
        for q in qs
    }


# ============================================================
# DISTRIBUTION METRICS
# ============================================================

def distribution_metrics(values):
    if not values:
        return {
            "n": 0,
            "p10": 0.0,
            "p25": 0.0,
            "p50": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p97_5": 0.0,
            "p99": 0.0,
            "p99_5": 0.0,
            "p99_9": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "iqr": 0.0,
            "mad": 0.0,
            "p90_p10": 0.0,
        }

    arr = np.asarray(
        values,
        dtype=np.float64,
    )

    p10 = float(np.percentile(arr, 10))
    p25 = float(np.percentile(arr, 25))
    p50 = float(np.percentile(arr, 50))
    p75 = float(np.percentile(arr, 75))
    p90 = float(np.percentile(arr, 90))
    p95 = float(np.percentile(arr, 95))
    p97_5 = float(np.percentile(arr, 97.5))
    p99 = float(np.percentile(arr, 99))
    p99_5 = float(np.percentile(arr, 99.5))
    p99_9 = float(np.percentile(arr, 99.9))

    mad = float(
        np.median(
            np.abs(arr - p50)
        )
    )

    return {
        "n": len(arr),
        "p10": p10,
        "p25": p25,
        "p50": p50,
        "p75": p75,
        "p90": p90,
        "p95": p95,
        "p97_5": p97_5,
        "p99": p99,
        "p99_5": p99_5,
        "p99_9": p99_9,
        "max": float(np.max(arr)),
        "mean": float(np.mean(arr)),
        "iqr": p75 - p25,
        "mad": mad,
        "p90_p10": p90 - p10,
    }


# ============================================================
# HILL-LIKE TAIL ESTIMATOR
# ============================================================

def hill_like_estimator(values):
    """
    Practical Hill-like tail measurement.

    The implementation uses P95 as threshold and
    the largest k observations above the threshold.

    Returned value is the mean log excess ratio:

        H = mean(log(x / threshold))

    Larger H -> heavier empirical upper tail.

    This is intentionally reported as a Hill-like
    diagnostic rather than a formal asymptotic tail
    parameter estimator.
    """

    if not values:
        return {
            "threshold": 0.0,
            "k": 0,
            "hill_log_excess": 0.0,
            "hill_alpha": 0.0,
        }

    arr = np.asarray(
        values,
        dtype=np.float64,
    )

    arr = arr[np.isfinite(arr)]

    if len(arr) < 20:
        return {
            "threshold": float(np.percentile(arr, 95))
            if len(arr) else 0.0,
            "k": 0,
            "hill_log_excess": 0.0,
            "hill_alpha": 0.0,
        }

    threshold = float(
        np.percentile(arr, 95)
    )

    upper = arr[arr > threshold]

    if len(upper) < 5:
        return {
            "threshold": threshold,
            "k": len(upper),
            "hill_log_excess": 0.0,
            "hill_alpha": 0.0,
        }

    k_target = max(
        10,
        int(0.05 * len(arr))
    )

    k = min(
        k_target,
        len(upper)
    )

    upper = np.sort(upper)[-k:]

    ratios = np.maximum(
        upper / max(threshold, EPS),
        1.0,
    )

    logs = np.log(ratios)

    hill_log_excess = float(
        np.mean(logs)
    )

    # Hill-style reciprocal representation.
    hill_alpha = (
        safe_div(
            1.0,
            hill_log_excess
        )
        if hill_log_excess > EPS
        else 0.0
    )

    return {
        "threshold": threshold,
        "k": int(k),
        "hill_log_excess": hill_log_excess,
        "hill_alpha": hill_alpha,
    }


# ============================================================
# ADIM — SHAPE
# ============================================================

def robust_shape_metrics(metrics):
    p10 = metrics["p10"]
    p25 = metrics["p25"]
    p50 = metrics["p50"]
    p75 = metrics["p75"]
    p97_5 = metrics["p97_5"]

    robust_skew = safe_div(
        p90(metrics) + p10 - 2.0 * p50,
        p90(metrics) - p10,
    )

    tail_asymmetry = safe_div(
        metrics["p99"] - p50,
        p50 - p10 + EPS,
    )

    robust_kurtosis = (
        safe_div(
            p97_5 - percentile_from_metrics(metrics, 2.5),
            p75 - p25,
        )
        - 2.91
    )

    return {
        "robust_skew": finite_or_zero(
            robust_skew
        ),
        "tail_asymmetry": finite_or_zero(
            tail_asymmetry
        ),
        "robust_kurtosis": finite_or_zero(
            robust_kurtosis
        ),
    }


def p90(metrics):
    return metrics["p90"]


def percentile_from_metrics(metrics, q):
    """
    Only P2.5 is needed by ADIM.

    It is reconstructed from the original distribution
    only when raw samples are available elsewhere.

    For the compact metrics object, use a conservative
    lower-tail approximation.
    """
    if q == 2.5:
        p10 = metrics["p10"]
        p25 = metrics["p25"]

        return max(
            0.0,
            p10 - 0.75 * (p25 - p10)
        )

    return 0.0


def robust_shape_from_values(values):
    if not values:
        return {
            "robust_skew": 0.0,
            "tail_asymmetry": 0.0,
            "robust_kurtosis": 0.0,
        }

    arr = np.asarray(
        values,
        dtype=np.float64,
    )

    p10 = np.percentile(arr, 10)
    p25 = np.percentile(arr, 25)
    p50 = np.percentile(arr, 50)
    p75 = np.percentile(arr, 75)
    p97_5 = np.percentile(arr, 97.5)
    p99 = np.percentile(arr, 99)
    p2_5 = np.percentile(arr, 2.5)
    p90 = np.percentile(arr, 90)

    skew = safe_div(
        p90 + p10 - 2 * p50,
        p90 - p10,
    )

    asym = safe_div(
        p99 - p50,
        p50 - p10,
    )

    kurt = (
        safe_div(
            p97_5 - p2_5,
            p75 - p25,
        )
        - 2.91
    )

    return {
        "robust_skew": finite_or_zero(skew),
        "tail_asymmetry": finite_or_zero(asym),
        "robust_kurtosis": finite_or_zero(kurt),
    }


# ============================================================
# ADIM — LOCATION / SCALE
# ============================================================

def location_core(metrics):
    return (
        0.25 * metrics["p25"]
        + 0.50 * metrics["p50"]
        + 0.25 * metrics["p75"]
    )


def scale_core(metrics):
    location = location_core(metrics)

    robust_sigma = (
        metrics["mad"] * 1.4826
    )

    relative_scale = safe_div(
        metrics["p90"] - metrics["p10"],
        location,
    )

    return {
        "robust_sigma": robust_sigma,
        "relative_scale": relative_scale,
        "iqr": metrics["iqr"],
        "p90_p10": metrics["p90_p10"],
    }


# ============================================================
# ADIM — QUANTILE RATIO SUITE
# ============================================================

def quantile_ratio_suite(metrics):
    p50 = metrics["p50"]

    return {
        "p90_p50": safe_div(
            metrics["p90"],
            p50,
        ),
        "p95_p50": safe_div(
            metrics["p95"],
            p50,
        ),
        "p99_p75": safe_div(
            metrics["p99"],
            metrics["p75"],
        ),
        "p99_p90": safe_div(
            metrics["p99"],
            metrics["p90"],
        ),
        "p99_p50": safe_div(
            metrics["p99"],
            p50,
        ),
        "asymmetry_ratio": safe_div(
            metrics["p90"] - p50,
            p50 - metrics["p10"],
        ),
    }


# ============================================================
# ADIM — TAIL CORE
# ============================================================

def normalize_tail_ratio(value):
    """
    Compress unbounded ratio into [0,1].

        x / (1+x)
    """

    return clamp(
        safe_div(
            value,
            1.0 + value,
        )
    )


def tail_core(values, metrics):
    ratios = quantile_ratio_suite(
        metrics
    )

    hill = hill_like_estimator(
        values
    )

    extreme_severity = safe_div(
        metrics["max"] - metrics["p99"],
        metrics["p99"] - metrics["p90"] + EPS,
    )

    q_tail = (
        0.40 * ratios["p99_p90"]
        + 0.35 * ratios["p99_p75"]
        + 0.25 * ratios["p99_p50"]
    )

    hill_score = clamp(
        hill["hill_log_excess"] / 1.5
    )

    extreme_score = clamp(
        extreme_severity / 10.0
    )

    kurtosis = robust_shape_from_values(
        values
    )["robust_kurtosis"]

    kurtosis_score = clamp(
        (kurtosis + 1.0) / 8.0
    )

    tail_strength = clamp(
        0.40 * normalize_tail_ratio(q_tail)
        + 0.30 * hill_score
        + 0.20 * extreme_score
        + 0.10 * kurtosis_score
    )

    return {
        "quantile_tail_strength": q_tail,
        "hill_log_excess": hill["hill_log_excess"],
        "hill_alpha": hill["hill_alpha"],
        "hill_threshold": hill["threshold"],
        "hill_k": hill["k"],
        "extreme_severity": extreme_severity,
        "extreme_score": extreme_score,
        "kurtosis_score": kurtosis_score,
        "tail_strength": tail_strength,
    }


# ============================================================
# ADIM — CONCENTRATION
# ============================================================

def concentration_core(metrics):
    return clamp(
        safe_div(
            metrics["p50"],
            metrics["p90"],
        )
    )


# ============================================================
# ADIM — FULL MODEL
# ============================================================

def adim_model(values):
    metrics = distribution_metrics(
        values
    )

    location = location_core(
        metrics
    )

    scale = scale_core(
        metrics
    )

    shape = robust_shape_from_values(
        values
    )

    ratios = quantile_ratio_suite(
        metrics
    )

    tail = tail_core(
        values,
        metrics
    )

    concentration = concentration_core(
        metrics
    )

    # --------------------------------------------------------
    # Normalized component scores
    # --------------------------------------------------------

    location_score = clamp(
        safe_div(
            location,
            metrics["p90"] + EPS,
        )
    )

    scale_score = clamp(
        safe_div(
            scale["relative_scale"],
            5.0,
        )
    )

    skew_score = clamp(
        abs(shape["robust_skew"]) / 2.0
    )

    asymmetry_score = clamp(
        ratios["asymmetry_ratio"] / 10.0
    )

    kurtosis_score = clamp(
        (shape["robust_kurtosis"] + 1.0)
        / 8.0
    )

    # --------------------------------------------------------
    # Sustained
    # --------------------------------------------------------

    sustained = clamp(
        0.65 * concentration
        + 0.35 * location_score
    )

    # --------------------------------------------------------
    # Dynamic
    # --------------------------------------------------------

    dynamic = clamp(
        0.35 * scale_score
        + 0.30 * skew_score
        + 0.20 * asymmetry_score
        + 0.15 * ratios["p90_p50"] / 10.0
    )

    # --------------------------------------------------------
    # Risk
    # --------------------------------------------------------

    risk = clamp(
        0.40 * tail["tail_strength"]
        + 0.20 * tail["extreme_score"]
        + 0.20 * kurtosis_score
        + 0.20 * tail["hill_log_excess"] / 1.5
    )

    # --------------------------------------------------------
    # ADCI
    # --------------------------------------------------------

    adci = clamp(
        0.35 * sustained
        + 0.35 * dynamic
        + 0.30 * risk
    )

    return {
        "distribution": metrics,
        "location": {
            "core": location,
            "score": location_score,
        },
        "scale": {
            **scale,
            "score": scale_score,
        },
        "shape": {
            **shape,
            "skew_score": skew_score,
            "kurtosis_score": kurtosis_score,
        },
        "quantile_ratios": ratios,
        "tail": tail,
        "concentration": {
            "value": concentration,
        },
        "scores": {
            "sustained": sustained,
            "dynamic": dynamic,
            "risk": risk,
            "adci": adci,
        },
    }


# ============================================================
# LEGACY V4/V5 TEMPORAL MODEL
# ============================================================

def temporal_model(metrics):
    p50 = metrics["p50"]
    p90 = metrics["p90"]
    p99 = metrics["p99"]
    iqr = metrics["iqr"]
    maximum = metrics["max"]

    sustained = clamp(
        safe_div(
            p50,
            max(p99, 1.0),
        )
    )

    burst = clamp(
        safe_div(
            safe_div(
                p90,
                max(p50, 1.0),
            ),
            10.0,
        )
    )

    tail = clamp(
        safe_div(
            safe_div(
                p99,
                max(p50, 1.0),
            ),
            20.0,
        )
    )

    extreme = clamp(
        safe_div(
            safe_div(
                maximum,
                max(p99, 1.0),
            ),
            10.0,
        )
    )

    dispersion = clamp(
        safe_div(
            iqr,
            max(p90, 1.0),
        )
    )

    rtci = clamp(
        0.50 * sustained
        + 0.25 * burst
        + 0.15 * tail
        + 0.10 * dispersion
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

    adim = adim_model(
        si_values
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
        "si_sustained": adim["scores"]["sustained"],
        "si_dynamic": adim["scores"]["dynamic"],
        "si_risk": adim["scores"]["risk"],
        "adim": adim,
    }


# ============================================================
# SCENE DETECTION
# ============================================================

def detect_scene_changes(
    scene_scores,
    timestamps,
    threshold=SCENE_THRESHOLD,
):
    events = []

    for i, score in enumerate(
        scene_scores
    ):
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


# ============================================================
# CONTENT REGIME
# ============================================================

def classify_regime(
    spatial_score,
    temporal_score,
    burst_score,
    tail_score,
    risk_score=0.0,
):
    spatial_high = spatial_score >= 0.60
    temporal_high = temporal_score >= 0.60

    burst_high = burst_score >= 0.20
    tail_high = tail_score >= 0.20
    risk_high = risk_score >= 0.60

    if spatial_high and temporal_high:
        if risk_high:
            return "HIGH-SPATIO-TEMPORAL-RISK"

        if burst_high:
            return "HIGH-SPATIO-TEMPORAL-BURST"

        return "HIGH-SPATIO-TEMPORAL"

    if spatial_high:
        return "SPATIAL-HEAVY"

    if temporal_high:
        return "MOTION-HEAVY"

    if risk_high:
        return "HEAVY-TAIL-RISK"

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
# V9 FUSION
# ============================================================

def calculate_v9_fusion(
    temporal_adim,
    spatial_adim,
    scene_score,
    texture_score,
):
    t = temporal_adim["scores"]
    s = spatial_adim["scores"]

    temporal_score = clamp(
        0.45 * t["adci"]
        + 0.25 * t["dynamic"]
        + 0.30 * t["risk"]
    )

    spatial_score = clamp(
        0.50 * s["adci"]
        + 0.30 * s["dynamic"]
        + 0.20 * s["risk"]
    )

    fusion = clamp(
        0.30 * temporal_score
        + 0.25 * spatial_score
        + 0.15 * t["sustained"]
        + 0.15 * s["sustained"]
        + 0.07 * t["risk"]
        + 0.05 * scene_score
        + 0.03 * texture_score
    )

    return {
        "temporal_score": temporal_score,
        "spatial_score": spatial_score,
        "fusion": fusion,
    }


def calculate_bdi(
    fusion,
    temporal_adim,
    spatial_adim,
):
    t = temporal_adim["scores"]
    s = spatial_adim["scores"]

    return clamp(
        0.35 * fusion
        + 0.25 * t["dynamic"]
        + 0.15 * t["risk"]
        + 0.15 * s["dynamic"]
        + 0.10 * s["risk"]
    )


# ============================================================
# SEGMENT ANALYSIS
# ============================================================

def analyze_segment(
    medad_values,
    si_values,
    changed_values,
    scene_values,
    segment_id,
    start_time,
    end_time,
    texture_values=None,
):
    if not medad_values:
        return None

    medad_metrics = distribution_metrics(
        medad_values
    )

    temporal_legacy = temporal_model(
        medad_metrics
    )

    temporal_adim = adim_model(
        medad_values
    )

    spatial_adim = adim_model(
        si_values
    )

    changed = (
        float(np.mean(changed_values))
        if changed_values
        else 0.0
    )

    scene = (
        float(np.mean(scene_values))
        if scene_values
        else 0.0
    )

    texture = (
        float(np.mean(texture_values))
        if texture_values
        else 0.0
    )

    texture_score = clamp(
        texture / 500.0
    )

    scene_score = clamp(
        scene
    )

    fusion = calculate_v9_fusion(
        temporal_adim,
        spatial_adim,
        scene_score,
        texture_score,
    )

    bdi = calculate_bdi(
        fusion["fusion"],
        temporal_adim,
        spatial_adim,
    )

    regime = classify_regime(
        fusion["spatial_score"],
        fusion["temporal_score"],
        temporal_legacy["burst_score"],
        temporal_legacy["tail_score"],
        temporal_adim["scores"]["risk"],
    )

    return {
        "segment": segment_id,
        "start": start_time,
        "end": end_time,

        "medad_p50": medad_metrics["p50"],
        "medad_p90": medad_metrics["p90"],
        "medad_p99": medad_metrics["p99"],

        "si_p50": (
            percentile(si_values, 50)
            if si_values else 0.0
        ),

        "si_p90": (
            percentile(si_values, 90)
            if si_values else 0.0
        ),

        "changed_pct": changed,

        "rtci": temporal_legacy["rtci"],
        "burst_score": temporal_legacy["burst_score"],
        "tail_score": temporal_legacy["tail_score"],
        "extreme_score": temporal_legacy["extreme_score"],

        "adim_sustained": temporal_adim["scores"]["sustained"],
        "adim_dynamic": temporal_adim["scores"]["dynamic"],
        "adim_risk": temporal_adim["scores"]["risk"],

        "spatial_adim": spatial_adim["scores"]["adci"],

        "scene_activity": scene,
        "texture": texture,

        "spatial_score": fusion["spatial_score"],
        "temporal_score": fusion["temporal_score"],

        "v9_fusion": fusion["fusion"],
        "adci_temporal": temporal_adim["scores"]["adci"],
        "adci_spatial": spatial_adim["scores"]["adci"],

        "bdi": bdi,

        "regime": regime,
        "class": classify_level(
            fusion["fusion"]
        ),
    }


# ============================================================
# VIDEO ANALYZER
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
        cap.get(cv2.CAP_PROP_FRAME_COUNT)
    )

    source_width = int(
        cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    )

    source_height = int(
        cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    )

    duration = (
        frame_count / source_fps
        if source_fps > 0
        else 0.0
    )

    sample_interval = 1.0 / target_fps

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
    changed_values = []
    scene_values = []
    texture_values = []
    timestamps = []

    sample_rows = []
    segment_rows = []

    current_segment = {
        "id": 0,
        "start": 0.0,
        "medad": [],
        "si": [],
        "changed": [],
        "scene": [],
        "texture": [],
    }

    next_sample_time = 0.0
    previous_gray = None

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

            timestamp = (
                cap.get(
                    cv2.CAP_PROP_POS_MSEC
                ) / 1000.0
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
                        int(h * scale),
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
                cv2.COLOR_BGR2GRAY,
            )

            if previous_gray is not None:

                medad = calculate_medad(
                    previous_gray,
                    gray,
                )

                si = calculate_si(
                    gray
                )

                changed = calculate_changed_percentage(
                    previous_gray,
                    gray,
                )

                scene = calculate_scene_score(
                    previous_gray,
                    gray,
                )

                texture = calculate_laplacian_var(
                    gray
                )

                medad_values.append(
                    medad
                )

                si_values.append(
                    si
                )

                changed_values.append(
                    changed
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

                sample_rows.append({
                    "timestamp": timestamp,
                    "medad": medad,
                    "si": si,
                    "changed_pct": changed,
                    "scene_score": scene,
                    "texture": texture,
                })

                if (
                    timestamp
                    >= current_segment["start"]
                    + segment_seconds
                ):

                    segment = analyze_segment(
                        current_segment["medad"],
                        current_segment["si"],
                        current_segment["changed"],
                        current_segment["scene"],
                        current_segment["id"],
                        current_segment["start"],
                        timestamp,
                        current_segment["texture"],
                    )

                    if segment:
                        segment_rows.append(
                            segment
                        )

                    current_segment = {
                        "id": (
                            current_segment["id"]
                            + 1
                        ),
                        "start": timestamp,
                        "medad": [],
                        "si": [],
                        "changed": [],
                        "scene": [],
                        "texture": [],
                    }

                current_segment[
                    "medad"
                ].append(medad)

                current_segment[
                    "si"
                ].append(si)

                current_segment[
                    "changed"
                ].append(changed)

                current_segment[
                    "scene"
                ].append(scene)

                current_segment[
                    "texture"
                ].append(texture)

            previous_gray = gray.copy()

            next_sample_time += sample_interval

            progress.update(
                task,
                advance=1,
                samples=len(medad_values),
            )

    cap.release()

    if current_segment["medad"]:

        segment = analyze_segment(
            current_segment["medad"],
            current_segment["si"],
            current_segment["changed"],
            current_segment["scene"],
            current_segment["id"],
            current_segment["start"],
            duration,
            current_segment["texture"],
        )

        if segment:
            segment_rows.append(
                segment
            )

    processing_time = (
        time.time() - start_wall
    )

    # ========================================================
    # GLOBAL DISTRIBUTIONS
    # ========================================================

    medad_metrics = distribution_metrics(
        medad_values
    )

    temporal_legacy = temporal_model(
        medad_metrics
    )

    temporal_adim = adim_model(
        medad_values
    )

    spatial = spatial_model(
        si_values
    )

    spatial_adim = spatial["adim"]

    changed_pct = (
        float(np.mean(changed_values))
        if changed_values
        else 0.0
    )

    texture_mean = (
        float(np.mean(texture_values))
        if texture_values
        else 0.0
    )

    texture_score = clamp(
        texture_mean / 500.0
    )

    # ========================================================
    # SCENE / EVENTS
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
        duration / 60.0,
    )

    scene_score = clamp(
        scene_per_minute / 30.0
    )

    # ========================================================
    # V9 FUSION
    # ========================================================

    fusion = calculate_v9_fusion(
        temporal_adim,
        spatial_adim,
        scene_score,
        texture_score,
    )

    bdi = calculate_bdi(
        fusion["fusion"],
        temporal_adim,
        spatial_adim,
    )

    regime = classify_regime(
        fusion["spatial_score"],
        fusion["temporal_score"],
        temporal_legacy["burst_score"],
        temporal_legacy["tail_score"],
        temporal_adim["scores"]["risk"],
    )

    # ========================================================
    # EXTREME EVENTS
    # ========================================================

    events = []

    p99 = medad_metrics["p99"]

    for i, value in enumerate(
        medad_values
    ):

        if (
            p99 > 0
            and value
            >= p99 * EXTREME_MULTIPLIER
        ):

            ts = (
                timestamps[i]
                if i < len(timestamps)
                else 0.0
            )

            events.append({
                "type": "EXTREME_TEMPORAL",
                "timestamp": float(ts),
                "medad": float(value),
                "severity": float(
                    safe_div(
                        value,
                        max(p99, EPS),
                    )
                ),
            })

    for ev in scene_events:

        events.append({
            "type": "SCENE_CHANGE",
            "timestamp": ev["timestamp"],
            "medad": 0.0,
            "severity": ev["score"],
        })

    events.sort(
        key=lambda x: x["timestamp"]
    )

    # ========================================================
    # RESULT
    # ========================================================

    result = {

        "version": VERSION,

        "adim_version": ADIM_VERSION,

        "video": str(video_path),

        "resolution": {
            "width": source_width,
            "height": source_height,
        },

        "source_fps": source_fps,

        "analysis_fps": target_fps,

        "frame_count": frame_count,

        "duration": duration,

        "analysis_width": analysis_width,

        "sample_count": len(
            medad_values
        ),

        # --------------------------------------------
        # Legacy V4/V5 distribution
        # --------------------------------------------

        "medad": medad_metrics,

        "temporal": temporal_legacy,

        # --------------------------------------------
        # Spatial
        # --------------------------------------------

        "spatial": spatial,

        # --------------------------------------------
        # ADIM
        # --------------------------------------------

        "adim": {

            "temporal": temporal_adim,

            "spatial": spatial_adim,

        },

        # --------------------------------------------
        # Global scores
        # --------------------------------------------

        "spatial_score":
            fusion["spatial_score"],

        "temporal_score":
            fusion["temporal_score"],

        "v9_fusion_score":
            fusion["fusion"],

        "adci_temporal":
            temporal_adim["scores"]["adci"],

        "adci_spatial":
            spatial_adim["scores"]["adci"],

        "bit_demand_index":
            bdi,

        # --------------------------------------------
        # Other intelligence
        # --------------------------------------------

        "changed_pixel_pct":
            changed_pct,

        "texture_mean":
            texture_mean,

        "scene_changes":
            scene_count,

        "scene_changes_per_minute":
            scene_per_minute,

        "content_regime":
            regime,

        "complexity_class":
            classify_level(
                fusion["fusion"]
            ),

        "event_count":
            len(events),

        "processing_seconds":
            processing_time,

        # --------------------------------------------
        # Raw
        # --------------------------------------------

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

    changed = np.array([
        s["changed_pct"]
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
        f"ASTCIE V9 ULTIMATE  •  "
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
    # MedAD
    # ========================================================

    ax1 = fig.add_subplot(
        gs[0, :2]
    )

    ax1.plot(
        ts,
        medad,
        color=COLORS["cyan"],
        linewidth=1.4,
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
        "Temporal Complexity — MedAD",
        color=COLORS["cyan"],
    )

    ax1.set_ylabel(
        "MedAD"
    )

    ax1.legend(
        loc="upper right",
        framealpha=0.3,
    )

    ax1.grid(
        True,
        alpha=0.4,
    )

    ax1.set_xlim(
        ts[0],
        ts[-1],
    )

    # ========================================================
    # ADIM SCORE PANEL
    # ========================================================

    ax2 = fig.add_subplot(
        gs[0, 2]
    )

    labels = [
        "T-Sustained",
        "T-Dynamic",
        "T-Risk",
        "S-Sustained",
        "S-Dynamic",
        "S-Risk",
        "ADCI-T",
        "ADCI-S",
        "V9 Fusion",
        "BDI",
    ]

    t_scores = result[
        "adim"
    ]["temporal"]["scores"]

    s_scores = result[
        "adim"
    ]["spatial"]["scores"]

    values = [
        t_scores["sustained"],
        t_scores["dynamic"],
        t_scores["risk"],
        s_scores["sustained"],
        s_scores["dynamic"],
        s_scores["risk"],
        result["adci_temporal"],
        result["adci_spatial"],
        result["v9_fusion_score"],
        result["bit_demand_index"],
    ]

    y_pos = np.arange(
        len(labels)
    )

    bars = ax2.barh(
        y_pos,
        values,
        color=COLORS["cyan"],
        edgecolor=COLORS["white"],
        linewidth=0.5,
    )

    ax2.set_yticks(
        y_pos
    )

    ax2.set_yticklabels(
        labels,
        fontsize=7,
    )

    ax2.set_xlim(
        0,
        1.05,
    )

    ax2.set_title(
        "ADIM / V9 Intelligence",
        color=COLORS["magenta"],
    )

    for bar, value in zip(
        bars,
        values,
    ):

        ax2.text(
            min(value + 0.02, 1.0),
            bar.get_y()
            + bar.get_height() / 2,
            f"{value:.3f}",
            va="center",
            fontsize=7,
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
        linewidth=1.3,
    )

    ax3.fill_between(
        ts,
        si,
        alpha=0.18,
        color=COLORS["blue"],
    )

    ax3.set_title(
        "Spatial Information",
        color=COLORS["blue"],
    )

    ax3.set_ylabel(
        "SI"
    )

    ax3.grid(
        True,
        alpha=0.4,
    )

    ax3.set_xlim(
        ts[0],
        ts[-1],
    )

    # ========================================================
    # Changed Pixels
    # ========================================================

    ax4 = fig.add_subplot(
        gs[1, 1]
    )

    ax4.plot(
        ts,
        changed,
        color=COLORS["orange"],
        linewidth=1.3,
    )

    ax4.fill_between(
        ts,
        changed,
        alpha=0.18,
        color=COLORS["orange"],
    )

    ax4.set_title(
        "Changed Pixels %",
        color=COLORS["orange"],
    )

    ax4.set_ylabel(
        "%"
    )

    ax4.grid(
        True,
        alpha=0.4,
    )

    ax4.set_xlim(
        ts[0],
        ts[-1],
    )

    # ========================================================
    # Texture
    # ========================================================

    ax5 = fig.add_subplot(
        gs[1, 2]
    )

    ax5.plot(
        ts,
        texture,
        color=COLORS["purple"],
        linewidth=1.3,
    )

    ax5.fill_between(
        ts,
        texture,
        alpha=0.18,
        color=COLORS["purple"],
    )

    ax5.set_title(
        "Texture / Laplacian Variance",
        color=COLORS["purple"],
    )

    ax5.set_ylabel(
        "Variance"
    )

    ax5.grid(
        True,
        alpha=0.4,
    )

    ax5.set_xlim(
        ts[0],
        ts[-1],
    )

    # ========================================================
    # Segment ADCI
    # ========================================================

    ax6 = fig.add_subplot(
        gs[2, :2]
    )

    if segments:

        seg_ids = [
            s["segment"]
            for s in segments
        ]

        fusions = [
            s["v9_fusion"]
            for s in segments
        ]

        ax6.bar(
            seg_ids,
            fusions,
            color=COLORS["cyan"],
            edgecolor=COLORS["white"],
            linewidth=0.5,
        )

        ax6.axhline(
            result["v9_fusion_score"],
            color=COLORS["magenta"],
            linestyle="--",
            linewidth=1.5,
            label="Global V9",
        )

        ax6.set_title(
            "Per-Segment V9 Fusion",
            color=COLORS["cyan"],
        )

        ax6.set_xlabel(
            "Segment"
        )

        ax6.set_ylabel(
            "ADCI / Fusion"
        )

        ax6.set_ylim(
            0,
            1.05,
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
    # Summary
    # ========================================================

    ax7 = fig.add_subplot(
        gs[2, 2]
    )

    ax7.axis(
        "off"
    )

    summary = (
        f"DURATION     "
        f"{fmt_time(result['duration'])}\n"
        f"SAMPLES      "
        f"{result['sample_count']}\n"
        f"SCENE Δ      "
        f"{result['scene_changes']}\n"
        f"SCENE/MIN    "
        f"{result['scene_changes_per_minute']:.2f}\n\n"
        f"REGIME\n"
        f"{result['content_regime']}\n\n"
        f"V9 COMPLEXITY\n"
        f"{result['v9_fusion_score']:.4f}\n"
        f"{result['complexity_class']}\n\n"
        f"ADCI-T       "
        f"{result['adci_temporal']:.4f}\n"
        f"ADCI-S       "
        f"{result['adci_spatial']:.4f}\n"
        f"BDI          "
        f"{result['bit_demand_index']:.4f}"
    )

    ax7.text(
        0.05,
        0.95,
        summary,
        transform=ax7.transAxes,
        fontsize=9,
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
# DISTRIBUTION PLOT
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

    fig, axes = plt.subplots(
        2,
        2,
        figsize=(14, 9),
        dpi=150,
    )

    fig.suptitle(
        "ASTCIE V9 — Distributional Intelligence",
        fontsize=14,
        fontweight="bold",
        color=COLORS["cyan"],
    )

    # MedAD
    axes[0, 0].hist(
        medad,
        bins=50,
        color=COLORS["cyan"],
        edgecolor=COLORS["bg"],
        alpha=0.9,
    )

    for q, color, label in [
        ("p50", COLORS["green"], "P50"),
        ("p90", COLORS["yellow"], "P90"),
        ("p99", COLORS["magenta"], "P99"),
    ]:

        axes[0, 0].axvline(
            result["medad"][q],
            color=color,
            linestyle="--",
            label=label,
        )

    axes[0, 0].set_title(
        "MedAD Distribution"
    )

    axes[0, 0].legend(
        framealpha=0.3
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
        edgecolor=COLORS["bg"],
        alpha=0.9,
    )

    axes[0, 1].set_title(
        "SI Distribution"
    )

    axes[0, 1].grid(
        True,
        alpha=0.4,
    )

    # CDF
    sorted_m = np.sort(
        medad
    )

    cdf = (
        np.arange(
            1,
            len(sorted_m) + 1,
        )
        / len(sorted_m)
    )

    axes[1, 0].plot(
        sorted_m,
        cdf,
        color=COLORS["cyan"],
        linewidth=2,
    )

    axes[1, 0].set_title(
        "MedAD CDF"
    )

    axes[1, 0].set_ylabel(
        "CDF"
    )

    axes[1, 0].grid(
        True,
        alpha=0.4,
    )

    # ADIM summary
    axes[1, 1].axis(
        "off"
    )

    adim = result[
        "adim"
    ]["temporal"]

    shape = adim[
        "shape"
    ]

    tail = adim[
        "tail"
    ]

    ratios = adim[
        "quantile_ratios"
    ]

    text = (
        "ADIM — TEMPORAL\n\n"
        f"Location      "
        f"{adim['location']['core']:.3f}\n"
        f"Robust Sigma  "
        f"{adim['scale']['robust_sigma']:.3f}\n"
        f"IQR           "
        f"{adim['scale']['iqr']:.3f}\n\n"
        f"Skew          "
        f"{shape['robust_skew']:.4f}\n"
        f"Kurtosis      "
        f"{shape['robust_kurtosis']:.4f}\n"
        f"Tail Asym     "
        f"{shape['tail_asymmetry']:.4f}\n\n"
        f"P90/P50       "
        f"{ratios['p90_p50']:.3f}\n"
        f"P95/P50       "
        f"{ratios['p95_p50']:.3f}\n"
        f"P99/P90       "
        f"{ratios['p99_p90']:.3f}\n"
        f"P99/P50       "
        f"{ratios['p99_p50']:.3f}\n\n"
        f"Hill Log      "
        f"{tail['hill_log_excess']:.4f}\n"
        f"Hill Alpha    "
        f"{tail['hill_alpha']:.4f}\n"
        f"Extreme       "
        f"{tail['extreme_severity']:.4f}\n\n"
        f"Sustained     "
        f"{adim['scores']['sustained']:.4f}\n"
        f"Dynamic       "
        f"{adim['scores']['dynamic']:.4f}\n"
        f"Risk          "
        f"{adim['scores']['risk']:.4f}\n"
        f"ADCI          "
        f"{adim['scores']['adci']:.4f}"
    )

    axes[1, 1].text(
        0.08,
        0.96,
        text,
        transform=axes[1, 1].transAxes,
        fontsize=9,
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
# CONSOLE REPORT
# ============================================================

def print_report(result):

    console.print()

    table = Table(
        title="V9 Distributional Intelligence"
    )

    table.add_column(
        "Metric",
        style="cyan",
    )

    table.add_column(
        "Value",
        justify="right",
    )

    m = result[
        "medad"
    ]

    table.add_row(
        "P10",
        f"{m['p10']:.4f}",
    )

    table.add_row(
        "P25",
        f"{m['p25']:.4f}",
    )

    table.add_row(
        "P50",
        f"{m['p50']:.4f}",
    )

    table.add_row(
        "P75",
        f"{m['p75']:.4f}",
    )

    table.add_row(
        "P90",
        f"{m['p90']:.4f}",
    )

    table.add_row(
        "P95",
        f"{m['p95']:.4f}",
    )

    table.add_row(
        "P99",
        f"{m['p99']:.4f}",
    )

    table.add_row(
        "P99.5",
        f"{m['p99_5']:.4f}",
    )

    table.add_row(
        "P99.9",
        f"{m['p99_9']:.4f}",
    )

    table.add_row(
        "Max",
        f"{m['max']:.4f}",
    )

    table.add_row(
        "IQR",
        f"{m['iqr']:.4f}",
    )

    table.add_row(
        "MAD",
        f"{m['mad']:.4f}",
    )

    table.add_row(
        "Changed %",
        f"{result['changed_pixel_pct']:.2f}%",
    )

    table.add_row(
        "Scene Changes",
        str(result["scene_changes"]),
    )

    table.add_row(
        "Scene / min",
        f"{result['scene_changes_per_minute']:.3f}",
    )

    console.print(
        table
    )

    # ========================================================
    # ADIM
    # ========================================================

    adim = result[
        "adim"
    ]["temporal"]

    table2 = Table(
        title="ADIM-V1 — Temporal"
    )

    table2.add_column(
        "Component",
        style="green",
    )

    table2.add_column(
        "Value",
        justify="right",
    )

    table2.add_row(
        "Location Core",
        f"{adim['location']['core']:.4f}",
    )

    table2.add_row(
        "Robust Sigma",
        f"{adim['scale']['robust_sigma']:.4f}",
    )

    table2.add_row(
        "Relative Scale",
        f"{adim['scale']['relative_scale']:.4f}",
    )

    table2.add_row(
        "Robust Skew",
        f"{adim['shape']['robust_skew']:.4f}",
    )

    table2.add_row(
        "Robust Kurtosis",
        f"{adim['shape']['robust_kurtosis']:.4f}",
    )

    table2.add_row(
        "Tail Asymmetry",
        f"{adim['shape']['tail_asymmetry']:.4f}",
    )

    table2.add_row(
        "P90 / P50",
        f"{adim['quantile_ratios']['p90_p50']:.4f}",
    )

    table2.add_row(
        "P95 / P50",
        f"{adim['quantile_ratios']['p95_p50']:.4f}",
    )

    table2.add_row(
        "P99 / P90",
        f"{adim['quantile_ratios']['p99_p90']:.4f}",
    )

    table2.add_row(
        "P99 / P50",
        f"{adim['quantile_ratios']['p99_p50']:.4f}",
    )

    table2.add_row(
        "Hill Log Excess",
        f"{adim['tail']['hill_log_excess']:.4f}",
    )

    table2.add_row(
        "Hill Alpha",
        f"{adim['tail']['hill_alpha']:.4f}",
    )

    table2.add_row(
        "Extreme Severity",
        f"{adim['tail']['extreme_severity']:.4f}",
    )

    table2.add_row(
        "Concentration",
        f"{adim['concentration']['value']:.4f}",
    )

    table2.add_row(
        "Sustained",
        f"{adim['scores']['sustained']:.4f}",
    )

    table2.add_row(
        "Dynamic",
        f"{adim['scores']['dynamic']:.4f}",
    )

    table2.add_row(
        "Risk",
        f"{adim['scores']['risk']:.4f}",
    )

    table2.add_row(
        "ADCI",
        f"{adim['scores']['adci']:.4f}",
    )

    console.print(
        table2
    )

    # ========================================================
    # FINAL
    # ========================================================

    level = result[
        "complexity_class"
    ]

    color = {
        "Very Low": "green",
        "Low": "green",
        "Moderate": "yellow",
        "High": "yellow",
        "Very High": "red",
        "Extreme": "bold red",
    }.get(
        level,
        "white",
    )

    profile = Text()

    profile.append(
        "CONTENT REGIME\n\n",
        style="bold cyan",
    )

    profile.append(
        f"{result['content_regime']}\n\n",
        style="bold magenta",
    )

    profile.append(
        "V9 COMPLEXITY\n\n",
        style="bold cyan",
    )

    profile.append(
        f"{result['v9_fusion_score']:.4f}\n",
        style="bold white",
    )

    profile.append(
        f"{level}\n",
        style=color,
    )

    profile.append(
        "\n\nADCI TEMPORAL\n\n",
        style="bold cyan",
    )

    profile.append(
        f"{result['adci_temporal']:.4f}\n",
        style="bold white",
    )

    profile.append(
        "\nADCI SPATIAL\n\n",
        style="bold cyan",
    )

    profile.append(
        f"{result['adci_spatial']:.4f}\n",
        style="bold white",
    )

    profile.append(
        "\nBDI\n\n",
        style="bold cyan",
    )

    profile.append(
        f"{result['bit_demand_index']:.4f}\n",
        style="bold white",
    )

    profile.append(
        "\nExperimental content-demand proxy.",
        style="dim",
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
# BATCH
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".ts",
    ".m2ts",
    ".webm",
}


def discover_videos(folder):
    folder = Path(folder)

    return sorted([
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower()
        in VIDEO_EXTENSIONS
    ])


def build_master_dataset(
    results,
):
    rows = []

    for result in results:

        rows.append({

            "channel":
                Path(
                    result["video"]
                ).stem,

            "duration":
                result["duration"],

            "samples":
                result["sample_count"],

            "p10":
                result["medad"]["p10"],

            "p50":
                result["medad"]["p50"],

            "p75":
                result["medad"]["p75"],

            "p90":
                result["medad"]["p90"],

            "p95":
                result["medad"]["p95"],

            "p99":
                result["medad"]["p99"],

            "p99_5":
                result["medad"]["p99_5"],

            "p99_9":
                result["medad"]["p99_9"],

            "iqr":
                result["medad"]["iqr"],

            "changed_pct":
                result["changed_pixel_pct"],

            "scene_changes":
                result["scene_changes"],

            "scene_per_min":
                result[
                    "scene_changes_per_minute"
                ],

            "temporal_adci":
                result["adci_temporal"],

            "spatial_adci":
                result["adci_spatial"],

            "v9_fusion":
                result["v9_fusion_score"],

            "bdi":
                result["bit_demand_index"],

            "regime":
                result["content_regime"],

            "class":
                result["complexity_class"],
        })

    return rows


def rank_results(results):
    return sorted(
        results,
        key=lambda x:
            x["v9_fusion_score"],
        reverse=True,
    )


def save_batch_outputs(
    results,
    output_dir,
):
    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    rows = build_master_dataset(
        results
    )

    save_csv(
        str(
            output_dir
            / "master_v9.csv"
        ),
        rows,
    )

    ranking = rank_results(
        results
    )

    ranking_rows = []

    for i, result in enumerate(
        ranking,
        1,
    ):

        ranking_rows.append({

            "rank": i,

            "channel":
                Path(
                    result["video"]
                ).stem,

            "v9_fusion":
                result[
                    "v9_fusion_score"
                ],

            "adci_temporal":
                result[
                    "adci_temporal"
                ],

            "adci_spatial":
                result[
                    "adci_spatial"
                ],

            "bdi":
                result[
                    "bit_demand_index"
                ],

            "class":
                result[
                    "complexity_class"
                ],

            "regime":
                result[
                    "content_regime"
                ],
        })

    save_csv(
        str(
            output_dir
            / "v9_ranking.csv"
        ),
        ranking_rows,
    )

    save_json(
        str(
            output_dir
            / "master_v9.json"
        ),
        {
            "version": VERSION,
            "adim_version": ADIM_VERSION,
            "videos": [
                {
                    k: v
                    for k, v
                    in result.items()
                    if k not in (
                        "samples",
                        "segments",
                        "events",
                    )
                }
                for result in results
            ],
        },
    )


def print_batch_ranking(
    results,
):
    table = Table(
        title="ASTCIE V9 — Dataset Ranking"
    )

    table.add_column(
        "#"
    )

    table.add_column(
        "Channel"
    )

    table.add_column(
        "ADCI",
        justify="right",
    )

    table.add_column(
        "V9 Fusion",
        justify="right",
    )

    table.add_column(
        "BDI",
        justify="right",
    )

    table.add_column(
        "Class"
    )

    table.add_column(
        "Regime"
    )

    for i, result in enumerate(
        rank_results(results),
        1,
    ):

        table.add_row(
            str(i),
            Path(
                result["video"]
            ).stem,
            f"{result['adci_temporal']:.4f}",
            f"{result['v9_fusion_score']:.4f}",
            f"{result['bit_demand_index']:.4f}",
            result["complexity_class"],
            result["content_regime"],
        )

    console.print(
        table
    )


# ============================================================
# SINGLE VIDEO
# ============================================================

def process_single(
    video,
    args,
):
    result = analyze_video(
        video,
        target_fps=args.fps,
        analysis_width=args.width,
        segment_seconds=args.segment,
    )

    print_report(
        result
    )

    out_dir = (
        Path(args.output)
        / Path(video).stem
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    create_dashboard(
        result,
        str(
            out_dir
            / "dashboard_v9.png"
        ),
    )

    create_distribution_plot(
        result,
        str(
            out_dir
            / "distribution_v9.png"
        ),
    )

    save_csv(
        str(
            out_dir
            / "samples_v9.csv"
        ),
        result["samples"],
    )

    save_csv(
        str(
            out_dir
            / "segments_v9.csv"
        ),
        result["segments"],
    )

    save_csv(
        str(
            out_dir
            / "events_v9.csv"
        ),
        result["events"],
    )

    clean_result = {
        k: v
        for k, v
        in result.items()
        if k not in (
            "samples",
            "segments",
            "events",
        )
    }

    save_json(
        str(
            out_dir
            / "result_v9.json"
        ),
        clean_result,
    )

    console.print(
        Panel(
            "[bold green]"
            "V9 ULTIMATE COMPLETE"
            "[/bold green]\n\n"
            "• V4 MedAD distribution\n"
            "• V5 burst / tail / extreme\n"
            "• V6 event intelligence\n"
            "• V7 spatial / temporal fusion\n"
            "• V8 graphical dashboard\n"
            "• ADIM-V1 distributional model\n"
            "• Robust shape intelligence\n"
            "• Hybrid tail intelligence\n"
            "• Hill-like tail estimator\n"
            "• Sustained / Dynamic / Risk\n"
            "• ADCI generated\n\n"
            f"[cyan]Folder → {out_dir}[/cyan]",
            border_style="green",
            padding=(1, 3),
        )
    )

    return result


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=
        "ASTCIE V9 Ultimate"
    )

    parser.add_argument(
        "video",
        nargs="?",
        help="Input video or folder",
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

    parser.add_argument(
        "--batch",
        action="store_true",
        help="Analyze all videos in folder",
    )

    parser.add_argument(
        "--output",
        default="results_v9",
    )

    args = parser.parse_args()

    if not args.video:

        print(
            "Usage:\n"
            "  python astcie_v9_ultimate.py video.mp4\n\n"
            "Batch:\n"
            "  python astcie_v9_ultimate.py "
            "videos --batch"
        )

        return 1

    print_banner()

    try:

        if args.batch:

            videos = discover_videos(
                args.video
            )

            if not videos:

                error(
                    "No supported videos found."
                )

                return 1

            console.print(
                Panel(
                    f"Videos discovered: "
                    f"{len(videos)}\n"
                    f"Sampling: "
                    f"{args.fps:.1f} FPS\n"
                    f"Analysis width: "
                    f"{args.width}px\n"
                    f"Segment: "
                    f"{args.segment:.1f}s",
                    title=
                    "V9 BATCH ANALYSIS",
                    border_style="cyan",
                )
            )

            results = []

            for i, video in enumerate(
                videos,
                1,
            ):

                console.print()

                info(
                    f"[{i}/{len(videos)}] "
                    f"Analyzing "
                    f"{video.name}"
                )

                try:

                    result = analyze_video(
                        video,
                        target_fps=args.fps,
                        analysis_width=args.width,
                        segment_seconds=args.segment,
                    )

                    results.append(
                        result
                    )

                    print_report(
                        result
                    )

                    video_dir = (
                        Path(args.output)
                        / video.stem
                    )

                    video_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    create_dashboard(
                        result,
                        str(
                            video_dir
                            / "dashboard_v9.png"
                        ),
                    )

                    create_distribution_plot(
                        result,
                        str(
                            video_dir
                            / "distribution_v9.png"
                        ),
                    )

                    save_csv(
                        str(
                            video_dir
                            / "samples_v9.csv"
                        ),
                        result["samples"],
                    )

                    save_csv(
                        str(
                            video_dir
                            / "segments_v9.csv"
                        ),
                        result["segments"],
                    )

                    save_csv(
                        str(
                            video_dir
                            / "events_v9.csv"
                        ),
                        result["events"],
                    )

                    save_json(
                        str(
                            video_dir
                            / "result_v9.json"
                        ),
                        {
                            k: v
                            for k, v
                            in result.items()
                            if k not in (
                                "samples",
                                "segments",
                                "events",
                            )
                        },
                    )

                except Exception as exc:

                    error(
                        f"{video.name}: "
                        f"{exc}"
                    )

            if results:

                print_batch_ranking(
                    results
                )

                save_batch_outputs(
                    results,
                    args.output,
                )

                console.print(
                    Panel(
                        "[bold green]"
                        "V9 BATCH COMPLETE"
                        "[/bold green]\n\n"
                        f"Processed: "
                        f"{len(results)}/"
                        f"{len(videos)}\n"
                        f"Output → "
                        f"{args.output}\n\n"
                        "ADIM + ADCI available "
                        "for cross-video comparison.",
                        border_style="green",
                        padding=(1, 3),
                    )
                )

            return 0

        # ----------------------------------------------------
        # Single
        # ----------------------------------------------------

        process_single(
            args.video,
            args,
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