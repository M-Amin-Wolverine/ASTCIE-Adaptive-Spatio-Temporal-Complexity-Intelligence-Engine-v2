#!/usr/bin/env python3
"""
CLI argument parser for ASTCIE Media Controller.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .models import ControllerConfig, OutputMode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ASTCIE Media Controller – Adaptive Real-Time Orchestrator (V8.1 + V9)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Video file(s) or folder(s)",
    )

    # Budget
    p.add_argument("--link-budget", type=float, default=30.0, help="Total link budget (Mbps)")
    p.add_argument("--audio-bitrate", type=float, default=96.0, help="Audio bitrate per channel (kbps)")
    p.add_argument("--audio-min", type=float, default=64.0, help="Min audio (kbps)")
    p.add_argument("--audio-max", type=float, default=128.0, help="Max audio (kbps)")
    p.add_argument("--bmin", type=float, default=1.5, help="Min video bitrate (Mbps)")
    p.add_argument("--bmax", type=float, default=8.0, help="Max video bitrate (Mbps)")

    # Engines
    p.add_argument("--v81", type=Path, default=Path("complexity7.py"))
    p.add_argument("--v9", type=Path, default=Path("complexity8.py"))
    p.add_argument("--weight-v81", type=float, default=0.5)
    p.add_argument("--weight-v9", type=float, default=0.5)

    # Encoding
    p.add_argument("--preset", default="medium")
    p.add_argument("--no-encode", action="store_true")

    # Output
    p.add_argument(
        "--output-mode",
        choices=[m.value for m in OutputMode],
        default="file",
    )
    p.add_argument("--udp-addr", default="127.0.0.1:1234")
    p.add_argument("--rtp-addr", default="127.0.0.1:5004")
    p.add_argument("--srt-addr", default="srt://127.0.0.1:9000")
    p.add_argument("--rist-addr", default="rist://127.0.0.1:5000")
    p.add_argument("--out", type=Path, default=Path("controller_results"))

    # Parallelism & Feedback
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--feedback-interval", type=float, default=1.0)
    p.add_argument("--hysteresis", type=float, default=0.05)
    p.add_argument("--no-parallel-engines", action="store_true")

    # Feature switches
    p.add_argument("--enable-priority", action="store_true")
    p.add_argument("--adaptive-audio", action="store_true")
    p.add_argument("--manual-psi", action="store_true")

    # Analysis tuning
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--segment", type=float, default=5.0)

    return p


def args_to_config(args: argparse.Namespace) -> ControllerConfig:
    return ControllerConfig(
        link_budget=args.link_budget,
        audio_bitrate=args.audio_bitrate / 1000.0,   # kbps → Mbps
        audio_min=args.audio_min / 1000.0,
        audio_max=args.audio_max / 1000.0,
        bmin=args.bmin,
        bmax=args.bmax,
        v81_script=args.v81,
        v9_script=args.v9,
        weight_v81=args.weight_v81,
        weight_v9=args.weight_v9,
        preset=args.preset,
        no_encode=args.no_encode,
        output_mode=OutputMode(args.output_mode),
        udp_addr=args.udp_addr,
        rtp_addr=args.rtp_addr,
        srt_addr=args.srt_addr,
        rist_addr=args.rist_addr,
        output_dir=args.out,
        max_workers=args.max_workers,
        feedback_interval=args.feedback_interval,
        hysteresis=args.hysteresis,
        enable_priority=args.enable_priority,
        adaptive_audio=args.adaptive_audio,
        manual_psi=args.manual_psi,
        no_parallel_engines=args.no_parallel_engines,
        analysis_fps=args.fps,
        analysis_width=args.width,
        segment_seconds=args.segment,
    )
