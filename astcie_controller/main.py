#!/usr/bin/env python3
"""
ASTCIE Media Controller – Main entry point

Combines the analysis → hybrid → allocation → encode → mux → transport →
quality/feedback pipeline into a single supervised controller.

Stages (each logged + reversible):
    1. discover    – collect inputs, build ChannelContext list
    2. analyze     – run V8.1 + V9 per channel
    3. fuse        – HybridEngine
    4. allocate    – GlobalIntelligence + BitrateAllocator (initial)
    5. encode      – EncoderManager starts per-channel ffmpeg
    6. sources     – SourceManager decides real vs. Color Wall fallback
    7. mux         – MPEGTSMuxer produces file + FIFO
    8. transport   – TransportManager fans out to configured outputs
    9. supervise   – quality sampling + feedback loop (Ctrl+C to stop)
"""

from __future__ import annotations

import atexit
import logging
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .cli import build_parser, args_to_config
from .models import (
    ChannelContext,
    ControllerConfig,
    EncoderAction,
    OutputMode,
)
from .engines.analysis import AnalysisEngine
from .engines.hybrid import HybridEngine
from .engines.intelligence import GlobalIntelligence
from .engines.allocator import BitrateAllocator
from .engines.encoder import EncoderManager
from .engines.muxer import MPEGTSMuxer
from .engines.transport import (
    TransportManager,
    TransportEndpoint,
    OutputMode as TransportOutputMode,
)
from .engines.monitor import RuntimeMonitor
from .engines.decision import DecisionEngine
from .engines.source_manager import SourceManager, FallbackConfig
from .engines.quality import QualityEngine
from .engines.feedback import FeedbackController

console = Console()
logger = logging.getLogger("astcie.main")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".m4v", ".ts", ".flv", ".m2ts",
}

# How often the quality engine is invoked (expensive).  Every N feedback
# ticks we do a real measurement; the other ticks reuse the previous sample.
QUALITY_EVERY_N_TICKS = 5


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(out_dir: Path, *, verbose: bool = False) -> logging.Logger:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "controller.log"

    root = logging.getLogger("astcie")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)-7s | %(message)s"))
    root.addHandler(sh)

    return root


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

def collect_videos(inputs: List[Path]) -> List[Path]:
    videos: List[Path] = []
    seen: set[Path] = set()
    for p in inputs:
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                videos.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                    rp = f.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        videos.append(f)
    return videos


def create_channels(videos: List[Path], out_dir: Path) -> List[ChannelContext]:
    channels: List[ChannelContext] = []
    for i, vid in enumerate(videos, 1):
        ch = ChannelContext(
            channel_id=i,
            input_path=vid,
            stem=vid.stem,
        )
        ch.analysis_dir = out_dir / "analysis" / f"{i:03d}_{vid.stem}"
        ch.analysis_dir.mkdir(parents=True, exist_ok=True)
        channels.append(ch)
    return channels


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_header(n: int, mode: str) -> None:
    console.print(Panel.fit(
        f"[bold cyan]ASTCIE MEDIA CONTROLLER v1.2[/bold cyan]\n"
        f"[white]Channels: {n}  |  Output: {mode}[/white]",
        border_style="cyan",
    ))


def render_allocation(channels: List[ChannelContext]) -> None:
    table = Table(title="Bitrate Allocation", box=box.DOUBLE_EDGE)
    table.add_column("CH", style="cyan", justify="right")
    table.add_column("Video", style="white")
    table.add_column("Hybrid", justify="right")
    table.add_column("BDI", justify="right")
    table.add_column("Video Mbps", justify="right")
    table.add_column("Audio kbps", justify="right")
    table.add_column("Regime")

    for ch in channels:
        st = ch.state
        table.add_row(
            f"{ch.channel_id:03d}",
            ch.stem[:30],
            f"{st.complexity:.4f}" if st.complexity is not None else "-",
            f"{st.bdi:.4f}" if st.bdi is not None else "-",
            f"{st.target_bitrate:.3f}" if st.target_bitrate else "-",
            f"{st.audio_bitrate * 1000:.0f}" if st.audio_bitrate else "-",
            str(st.regime or "-"),
        )
    console.print(table)


def render_status(
    channels: List[ChannelContext],
    source_mgr: SourceManager,
    transport: TransportManager,
    muxer: MPEGTSMuxer,
) -> Panel:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, expand=True)
    table.add_column("CH", style="cyan", justify="right")
    table.add_column("Status")
    table.add_column("Target", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Encoder")

    for ch in channels:
        st = ch.state
        state = source_mgr.probe(ch.channel_id)
        state_str = state.value if hasattr(state, "value") else str(state)
        color = {"real": "green", "fallback": "yellow",
                 "failed": "red", "recovering": "yellow",
                 "blacklisted": "red"}.get(state_str, "white")
        table.add_row(
            f"{ch.channel_id:03d}",
            f"[{color}]{state_str}[/{color}]",
            f"{(st.target_bitrate or 0):.2f}",
            f"{(st.actual_bitrate or 0):.2f}",
            str(getattr(st, "encoder_status", "") or "-"),
        )

    mux_stats = muxer.get_stats()
    footer = (
        f"mux: [bold]{mux_stats.status}[/bold]  "
        f"restarts={mux_stats.restarts}  "
        f"inputs={mux_stats.n_inputs}  "
        f"bytes={mux_stats.bytes_written}"
    )
    body = Table.grid()
    body.add_row(table)
    body.add_row(footer)
    return Panel(body, title="Live Status", border_style="green")


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

@dataclass
class _Stage:
    name: str
    done: bool = False


@dataclass
class Controller:
    config: ControllerConfig
    channels: List[ChannelContext] = field(default_factory=list)

    # engines, built lazily in run()
    analysis: Optional[AnalysisEngine] = None
    hybrid: Optional[HybridEngine] = None
    intelligence: Optional[GlobalIntelligence] = None
    allocator: Optional[BitrateAllocator] = None
    encoder: Optional[EncoderManager] = None
    source_mgr: Optional[SourceManager] = None
    muxer: Optional[MPEGTSMuxer] = None
    transport: Optional[TransportManager] = None
    monitor: Optional[RuntimeMonitor] = None
    decision: Optional[DecisionEngine] = None
    quality: Optional[QualityEngine] = None
    feedback: Optional[FeedbackController] = None

    _stop: threading.Event = field(default_factory=threading.Event)
    _closed: bool = False
    _installed_signals: bool = False
    _last_quality_samples: Optional[List] = None

    # ---------------------------------------------------------------- signals
    def install_signal_handlers(self) -> None:
        if self._installed_signals:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum, _frame):
            logger.info("Received signal %s – shutting down", signum)
            self.request_stop()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except Exception:
                pass
        self._installed_signals = True
        atexit.register(self.shutdown)

    def request_stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        logger.info("Shutting down controller")

        # Reverse order of construction.
        for name, obj, method in (
            ("transport", self.transport, "stop"),
            ("muxer", self.muxer, "stop"),
            ("encoder", self.encoder, "stop_all"),
        ):
            if obj is None:
                continue
            fn = getattr(obj, method, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception as exc:
                logger.warning("Error stopping %s: %s", name, exc)

        # Best-effort placeholder cleanup.
        if self.source_mgr is not None:
            try:
                self.source_mgr.cleanup_placeholders()
            except Exception:
                pass

        logger.info("Controller finished cleanly")

    # ---------------------------------------------------------------- stages
    def _ensure_tools(self) -> bool:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                console.print(f"[red]{tool} not found in PATH[/red]")
                return False
        if not self.config.v81_script.exists():
            console.print(
                f"[red]V8.1 script not found: {self.config.v81_script}[/red]")
            return False
        if not self.config.v9_script.exists():
            console.print(
                f"[red]V9 script not found: {self.config.v9_script}[/red]")
            return False
        return True

    def stage_analyze(self) -> None:
        self.analysis = AnalysisEngine(self.config)
        self.analysis.run_parallel(self.channels)

    def stage_fuse(self) -> None:
        self.hybrid = HybridEngine(self.config)
        self.hybrid.compute(self.channels)

    def stage_allocate(self) -> float:
        self.intelligence = GlobalIntelligence(self.config)
        video_budget = self.intelligence.calculate_budget(self.channels)

        self.allocator = BitrateAllocator(self.config, self.intelligence)
        self.allocator.initial_allocate(self.channels, video_budget)
        return video_budget

    def stage_encode(self) -> None:
        self.encoder = EncoderManager(self.config)
        self.encoder.start_all(self.channels)

    def stage_sources(self) -> None:
        self.source_mgr = SourceManager(self.config, FallbackConfig())
        self.source_mgr.refresh(self.channels)
        logger.info("\n%s", self.source_mgr.status_summary())

    def stage_mux(self) -> None:
        self.muxer = MPEGTSMuxer(self.config)
        self.muxer.start(
            self.channels,
            manual_psi=self.config.manual_psi,
            use_file=True,
            use_fifo=True,
        )

    def stage_transport(self) -> None:
        self.transport = TransportManager(self.config)
        source_ts = (
            self.muxer.get_output_for_transport()
            if self.muxer is not None
            else None
        ) or (self.muxer.output_path if self.muxer else None)
        logger.info("Transport source: %s", source_ts)

        # NOTE: `TransportManager.start(...)` is shadowed by a later
        # single-argument definition in transport_v3, so the high-level
        # convenience API is unusable.  Build an endpoint explicitly and
        # go through add_endpoint + start_all.
        try:
            mode = TransportOutputMode.from_str(self.config.output_mode.value)
        except Exception:
            logger.warning(
                "OutputMode %r not understood by TransportManager; "
                "skipping transport", self.config.output_mode)
            return

        ep = TransportEndpoint(mode=mode)
        # Fill in sane defaults based on mode.  The operator can override
        # via the CLI in a future revision.
        if mode == TransportOutputMode.FILE:
            ep.output_path = self.config.output_dir / "final.ts"
            ep.target = str(ep.output_path)
        elif mode == TransportOutputMode.UDP:
            ep.target = getattr(self.config, "udp_addr",
                                "239.0.0.1:1234")
        elif mode == TransportOutputMode.RTP:
            ep.target = getattr(self.config, "rtp_addr",
                                "127.0.0.1:5004")
            ep.sdp_path = self.config.output_dir / "vectra.sdp"
        elif mode == TransportOutputMode.SRT:
            ep.target = getattr(self.config, "srt_addr",
                                "srt://127.0.0.1:9000")
        elif mode in (TransportOutputMode.RTMP, TransportOutputMode.RTMPS):
            ep.target = getattr(self.config, "rtmp_addr",
                                "rtmp://127.0.0.1/live/stream")
        elif mode in (TransportOutputMode.HTTP, TransportOutputMode.HTTPS):
            ep.target = getattr(self.config, "http_addr",
                                "http://127.0.0.1:8080/feed")

        # Feed source into the manager.
        if source_ts is not None:
            self.transport.set_source_ts(Path(source_ts))

        try:
            self.transport.add_endpoint(ep, auto_start=False)
            self.transport.start_all()
        except Exception as exc:
            logger.error("Transport failed to start: %s", exc)
            return

        logger.info("\n%s", self.transport.status_summary())

    def stage_supervise(self) -> None:
        self.monitor = RuntimeMonitor(self.config)
        self.quality = QualityEngine(self.config)
        self.feedback = FeedbackController(self.config)
        self.decision = DecisionEngine(self.config)

        if self.allocator is None or self.encoder is None:
            logger.error(
                "Supervise requires allocator + encoder; "
                "allocate/encode stages must run first"
            )
            return

        console.print(
            "\n[bold green]Live quality-aware feedback loop "
            "(Ctrl+C to stop)[/bold green]\n"
        )

        tick = 0
        last_samples = None

        with Live(
            render_status(
                self.channels, self.source_mgr, self.transport, self.muxer
            ),
            console=console,
            refresh_per_second=1,
            transient=False,
        ) as live:
            while not self._stop.is_set():
                if self._stop.wait(self.config.feedback_interval):
                    break
                tick += 1

                # 1) Runtime → State
                try:
                    self.monitor.collect(self.channels)
                except Exception as exc:
                    logger.warning("Monitor tick failed: %s", exc)

                try:
                    self.source_mgr.refresh(self.channels)
                except Exception as exc:
                    logger.warning("Source refresh failed: %s", exc)

                # 2) Quality → State (metric + confidence)
                if tick % QUALITY_EVERY_N_TICKS == 0:
                    try:
                        last_samples = self.quality.measure_all(self.channels)
                        self._last_quality_samples = last_samples
                    except Exception as exc:
                        logger.warning("Quality pass failed: %s", exc)

                # 3) Feedback → proposals only (does NOT write target)
                fb_decisions = []
                try:
                    fb_decisions = (
                        self.feedback.evaluate(self.channels, last_samples)
                        or []
                    )
                except Exception as exc:
                    logger.warning("Feedback evaluate failed: %s", exc)

                # 4) Decision → final actions
                try:
                    actions = self.decision.decide(
                        self.channels,
                        feedback_decisions=fb_decisions,
                    )
                except Exception as exc:
                    logger.warning("Decision failed: %s", exp)
                    actions = [EncoderAction.KEEP] * len(self.channels)

                if len(actions) != len(self.channels):
                    logger.warning(
                        "Decision returned %d actions for %d channels",
                        len(actions), len(self.channels),
                    )
                    actions = [EncoderAction.KEEP] * len(self.channels)

                # Snapshot targets BEFORE allocator commits
                prev_targets = {
                    ch.channel_id: float(ch.state.target_bitrate or 0.0)
                    for ch in self.channels
                }

                suggestions = {
                    int(d.channel_id): float(d.new_bitrate)
                    for d in fb_decisions
                    if d.new_bitrate is not None
                }

                # 5) Allocator — sole owner of target_bitrate (once only)
                try:
                    self.allocator.reallocate(
                        self.channels,
                        actions,
                        suggestions=suggestions,
                    )
                except TypeError:
                    try:
                        self.allocator.reallocate(self.channels, actions)
                    except Exception as exc:
                        logger.warning("Allocation tick failed: %s", exp)
                except Exception as exp:
                    logger.warning("Allocation tick failed: %s", exp)

                # 6) Encoder — final State targets only when changed
                for ch, action in zip(self.channels, actions):
                    if action not in (
                        EncoderAction.INCREASE,
                        EncoderAction.DECREASE,
                        EncoderAction.RECOVER,
                    ):
                        continue
                    new_br = float(getattr(ch.state, "target_bitrate", 0.0) or 0.0)
                    if new_br <= 0:
                        continue
                    old_br = prev_targets.get(ch.channel_id, 0.0)
                    if old_br > 0 and abs(new_br - old_br) / old_br < 0.01:
                        continue
                    try:
                        self.encoder.update_bitrate(ch, new_br)
                    except Exception as exc:
                        logger.warning(
                            "Encoder update CH%03d failed: %s",
                            ch.channel_id, exp,
                        )

                live.update(
                    render_status(
                        self.channels, self.source_mgr,
                        self.transport, self.muxer,
                    )
                )

    def _channel_by_id(self, cid: int) -> Optional[ChannelContext]:
        for ch in self.channels:
            if ch.channel_id == cid:
                return ch
        return None

    # ---------------------------------------------------------------- run
    def run(self, *, dry_run: bool = False) -> int:
        self.install_signal_handlers()

        if not self._ensure_tools():
            return 1

        render_header(len(self.channels), self.config.output_mode.value)

        stages = [
            ("analyze", self.stage_analyze),
            ("fuse", self.stage_fuse),
            ("allocate", self.stage_allocate),
        ]
        if dry_run:
            stages = stages[:2]  # analysis + fusion only

        for name, fn in stages:
            try:
                logger.info("Stage: %s", name)
                result = fn()
                if name == "allocate" and result is not None:
                    render_allocation(self.channels)
            except Exception as exc:
                logger.exception("Stage %s failed: %s", name, exc)
                return 1

        if not dry_run:
            render_allocation(self.channels)

        if self.config.no_encode:
            console.print(
                "[yellow]--no-encode set – stopping after allocation[/yellow]"
            )
            return 0

        if dry_run:
            console.print("[yellow]--dry-run set – stopping[/yellow]")
            return 0

        run_stages = [
            ("sources", self.stage_sources),
            ("encode", self.stage_encode),
            ("mux", self.stage_mux),
            ("transport", self.stage_transport),
        ]
        for name, fn in run_stages:
            try:
                logger.info("Stage: %s", name)
                fn()
            except Exception as exc:
                logger.exception("Stage %s failed: %s", name, exc)
                self.shutdown()
                return 1

        try:
            self.stage_supervise()
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted[/yellow]")
        except Exception as exc:
            logger.exception("Supervisor failed: %s", exc)
            return 1
        finally:
            self.shutdown()

        console.print("[bold green]Done.[/bold green]")
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = args_to_config(args)

    logger_root = setup_logging(
        config.output_dir,
        verbose=getattr(args, "verbose", False),
    )
    logger_root.info("Controller starting")

    videos = collect_videos(args.inputs)
    if not videos:
        console.print("[red]No valid video files found[/red]")
        return 1

    channels = create_channels(videos, config.output_dir)
    logger_root.info("Created %d ChannelContext objects", len(channels))

    ctrl = Controller(config=config, channels=channels)
    dry_run = bool(getattr(args, "dry_run", False))
    return ctrl.run(dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
