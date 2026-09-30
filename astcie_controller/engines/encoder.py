#!/usr/bin/env python3
"""
EncoderManager – starts / updates / stops per-channel FFmpeg processes.

Ownership contract:

    Allocator
        ↓
    target_bitrate
        ↓
    Encoder
        ↓
    FFmpeg

Encoder does NOT decide bitrate.
Encoder only executes the bitrate already approved by Allocator.

Responsibilities:
    - Resolve active input source
    - Build FFmpeg command
    - Start encoder
    - Verify process startup health
    - Read FFmpeg runtime progress
    - Restart encoder when Allocator-approved bitrate changes
    - Stop encoder safely

Output format:
    MPEG-TS
    *.ts
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import List

from ..models import (
    ChannelContext,
    ControllerConfig,
    EncoderStatus,
)


logger = logging.getLogger("astcie.encoder")


class EncoderManager:
    """
    FFmpeg process manager.

    The EncoderManager is an execution layer.

    It must never become a second bitrate owner.
    """

    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.encoder")

        # --------------------------------------------------------------
        # Active FFmpeg processes
        # --------------------------------------------------------------

        self._procs: dict[int, subprocess.Popen] = {}

        # --------------------------------------------------------------
        # FFmpeg progress reader threads
        # --------------------------------------------------------------

        self._progress_threads: dict[int, threading.Thread] = {}
        self._progress_stop: dict[int, threading.Event] = {}

        # --------------------------------------------------------------
        # Short grace period used only to verify that FFmpeg
        # successfully survived process startup.
        # --------------------------------------------------------------

        self.startup_grace = 0.5

    # ==================================================================
    # Source resolution
    # ==================================================================

    def _source_for(
        self,
        channel: ChannelContext,
    ) -> Path:
        """
        Resolve the actual source file for the encoder.

        Priority:

            active_source_path
                    ↓
                input_path

        This keeps Encoder consistent with SourceManager and Analysis.
        """

        source = (
            channel.active_source_path
            or channel.input_path
        )

        if not source:
            raise FileNotFoundError(
                f"Channel {channel.channel_id}: "
                "no source available"
            )

        source = Path(source)

        if not source.exists():
            raise FileNotFoundError(
                f"Channel {channel.channel_id}: "
                f"source not found: {source}"
            )

        return source

    # ==================================================================
    # Output resolution
    # ==================================================================

    def _output_for(
        self,
        channel: ChannelContext,
    ) -> Path:
        """
        Resolve the encoded MPEG-TS output path.

        Output is always:

            <channel>_enc.ts

        Never .mp4 with -f mpegts.
        """

        if channel.encoded_path is not None:
            out = Path(channel.encoded_path)

            if out.suffix.lower() != ".ts":
                out = out.with_suffix(".ts")

            channel.encoded_path = out

        else:
            out = (
                self.config.output_dir
                / f"ch{channel.channel_id:03d}_"
                  f"{channel.stem}_enc.ts"
            )

            channel.encoded_path = out

        out.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        return out

    # ==================================================================
    # Command construction
    # ==================================================================

    def _build_cmd(
        self,
        channel: ChannelContext,
    ) -> list[str]:
        """
        Build FFmpeg command for one channel.

        Video:
            H.264 / libx264

        Audio:
            AAC

        Container:
            MPEG-TS

        Runtime telemetry:
            -progress pipe:1
        """

        source = self._source_for(channel)
        out = self._output_for(channel)

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # target_bitrate is READ here.
        #
        # Encoder never modifies it.
        # --------------------------------------------------------------

        bitrate_k = max(
            1,
            int(
                round(
                    channel.state.target_bitrate * 1000
                )
            ),
        )

        audio_k = max(
            32,
            int(
                round(
                    channel.state.audio_bitrate * 1000
                )
            ),
        )

        cmd = [
            "ffmpeg",

            # ----------------------------------------------------------
            # General
            # ----------------------------------------------------------

            "-hide_banner",
            "-y",

            # ----------------------------------------------------------
            # Input
            # ----------------------------------------------------------

            "-i",
            str(source),

            # ----------------------------------------------------------
            # Stream mapping
            # ----------------------------------------------------------

            "-map",
            "0:v:0",

            "-map",
            "0:a?",

            # ----------------------------------------------------------
            # Video
            # ----------------------------------------------------------

            "-c:v",
            "libx264",

            "-preset",
            self.config.preset,

            "-b:v",
            f"{bitrate_k}k",

            "-maxrate",
            f"{bitrate_k}k",

            "-bufsize",
            f"{bitrate_k * 2}k",

            "-pix_fmt",
            "yuv420p",

            # ----------------------------------------------------------
            # Audio
            # ----------------------------------------------------------

            "-c:a",
            "aac",

            "-b:a",
            f"{audio_k}k",

            # ----------------------------------------------------------
            # Container
            # ----------------------------------------------------------

            "-f",
            "mpegts",

            # ----------------------------------------------------------
            # FFmpeg machine-readable progress
            # ----------------------------------------------------------

            "-progress",
            "pipe:1",

            "-nostats",

            # ----------------------------------------------------------
            # Output
            # ----------------------------------------------------------

            str(out),
        ]

        return cmd

    # ==================================================================
    # Start all
    # ==================================================================

    def start_all(
        self,
        channels: List[ChannelContext],
    ) -> None:
        """
        Start one encoder process per channel.
        """

        if self.config.no_encode:
            self.logger.info(
                "--no-encode set – skipping encoder start"
            )
            return

        for channel in channels:
            try:
                self._start_one(channel)

            except Exception as exc:
                channel.state.encoder_status = (
                    EncoderStatus.ERROR
                )
                channel.state.touch()

                self.logger.error(
                    "Failed to start encoder CH%03d: %s",
                    channel.channel_id,
                    exc,
                )

    # ==================================================================
    # Start one
    # ==================================================================

    def _start_one(
        self,
        channel: ChannelContext,
    ) -> None:
        """
        Start one FFmpeg encoder and verify startup health.

        State machine:

            STARTING
                ↓
              Popen
                ↓
        register process
                ↓
        start progress reader
                ↓
          startup grace
                ↓
             poll()
             /    \
          exit    alive
           ↓        ↓
         ERROR    RUNNING
        """

        channel.state.encoder_status = (
            EncoderStatus.STARTING
        )
        channel.state.touch()

        # --------------------------------------------------------------
        # Build command
        # --------------------------------------------------------------

        cmd = self._build_cmd(channel)

        self.logger.info(
            "Starting encoder CH%03d",
            channel.channel_id,
        )

        self.logger.debug(
            "FFmpeg command CH%03d: %s",
            channel.channel_id,
            " ".join(cmd),
        )

        # --------------------------------------------------------------
        # Start process
        # --------------------------------------------------------------

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,

            # stderr is intentionally discarded for now.
            #
            # FFmpeg telemetry is carried through stdout using:
            #     -progress pipe:1
            #
            stderr=subprocess.DEVNULL,

            text=True,
            encoding="utf-8",
            errors="replace",
        )

        channel.ffmpeg_proc = proc
        self._procs[channel.channel_id] = proc

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # Progress reader MUST start here.
        #
        # It is a class method, not a local function.
        # --------------------------------------------------------------

        self._start_progress_reader(
            channel,
            proc,
        )

        # --------------------------------------------------------------
        # Startup grace period
        #
        # Popen() only means the OS created the process.
        # It does NOT mean FFmpeg successfully initialized.
        # --------------------------------------------------------------

        time.sleep(self.startup_grace)

        return_code = proc.poll()

        # --------------------------------------------------------------
        # Process exited during startup
        # --------------------------------------------------------------

        if return_code is not None:

            self._stop_progress_reader(
                channel.channel_id,
                join_timeout=1.0,
            )

            self._procs.pop(
                channel.channel_id,
                None,
            )

            channel.ffmpeg_proc = None

            channel.state.encoder_status = (
                EncoderStatus.ERROR
            )
            channel.state.touch()

            self.logger.error(
                "Encoder CH%03d exited during startup "
                "with return code %s",
                channel.channel_id,
                return_code,
            )

            raise RuntimeError(
                f"FFmpeg encoder CH{channel.channel_id:03d} "
                f"failed during startup "
                f"(return code {return_code})"
            )

        # --------------------------------------------------------------
        # Process survived startup grace
        # --------------------------------------------------------------

        channel.state.encoder_status = (
            EncoderStatus.RUNNING
        )
        channel.state.touch()

        self.logger.info(
            "Encoder CH%03d is RUNNING | PID=%s | output=%s",
            channel.channel_id,
            proc.pid,
            channel.encoded_path,
        )

    # ==================================================================
    # Progress reader startup
    # ==================================================================

    def _start_progress_reader(
        self,
        channel: ChannelContext,
        proc: subprocess.Popen,
    ) -> None:
        """
        Start a background reader for FFmpeg:

            -progress pipe:1

        FFmpeg writes key=value telemetry to stdout.
        """

        # --------------------------------------------------------------
        # Stop an existing reader first.
        # This protects against duplicate threads during restart.
        # --------------------------------------------------------------

        self._stop_progress_reader(
            channel.channel_id,
            join_timeout=0.5,
        )

        stop_event = threading.Event()

        self._progress_stop[
            channel.channel_id
        ] = stop_event

        thread = threading.Thread(
            target=self._progress_loop,
            args=(
                channel,
                proc,
                stop_event,
            ),
            daemon=True,
            name=(
                f"ffmpeg-progress-"
                f"{channel.channel_id:03d}"
            ),
        )

        self._progress_threads[
            channel.channel_id
        ] = thread

        thread.start()

        self.logger.debug(
            "FFmpeg progress reader started CH%03d",
            channel.channel_id,
        )

    # ==================================================================
    # Progress reader stop
    # ==================================================================

    def _stop_progress_reader(
        self,
        channel_id: int,
        join_timeout: float = 1.0,
    ) -> None:
        """
        Stop and clean up a progress reader thread.
        """

        stop_event = self._progress_stop.get(
            channel_id
        )

        if stop_event is not None:
            stop_event.set()

        thread = self._progress_threads.get(
            channel_id
        )

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(
                timeout=join_timeout
            )

        self._progress_stop.pop(
            channel_id,
            None,
        )

        self._progress_threads.pop(
            channel_id,
            None,
        )

    # ==================================================================
    # Progress loop
    # ==================================================================

    def _progress_loop(
        self,
        channel: ChannelContext,
        proc: subprocess.Popen,
        stop_event: threading.Event,
    ) -> None:
        """
        Continuously read FFmpeg key=value progress records.

        Example:

            frame=123
            fps=25.0
            bitrate=1234.5kbits/s
            total_size=1234567
            out_time=00:00:05.120000
            speed=1.02x
            progress=continue

        Every block ends with:

            progress=continue

        and finally:

            progress=end
        """

        if proc.stdout is None:
            self.logger.warning(
                "No stdout pipe available for "
                "CH%03d progress reader",
                channel.channel_id,
            )
            return

        current: dict[str, str] = {}

        try:
            while not stop_event.is_set():

                line = proc.stdout.readline()

                if not line:
                    if proc.poll() is not None:
                        break

                    time.sleep(0.05)
                    continue

                line = line.strip()

                if not line:
                    continue

                if "=" not in line:
                    continue

                key, value = line.split(
                    "=",
                    1,
                )

                current[key] = value

                # ------------------------------------------------------
                # FFmpeg terminates every progress block with:
                #
                # progress=continue
                #
                # or:
                #
                # progress=end
                # ------------------------------------------------------

                if key != "progress":
                    continue

                self._apply_progress(
                    channel,
                    current,
                )

                current = {}

        except Exception as exc:

            self.logger.warning(
                "Progress reader failed CH%03d: %s",
                channel.channel_id,
                exc,
            )

        finally:

            self.logger.debug(
                "Progress reader stopped CH%03d",
                channel.channel_id,
            )

    # ==================================================================
    # Apply progress
    # ==================================================================

    def _apply_progress(
        self,
        channel: ChannelContext,
        data: dict[str, str],
    ) -> None:
        """
        Convert FFmpeg progress values into ChannelState metrics.

        Important:

            progress_bitrate
                =
            measured FFmpeg output bitrate

        It is NOT copied from target_bitrate.
        """

        st = channel.state

        # --------------------------------------------------------------
        # bitrate
        #
        # Example:
        #
        # bitrate=1234.5kbits/s
        #
        # Stored internally as Mbps.
        # --------------------------------------------------------------

        bitrate = data.get("bitrate")

        if bitrate:
            try:
                value = bitrate.strip().lower()

                if value.endswith("kbits/s"):

                    numeric = float(
                        value[:-7].strip()
                    )

                    st.progress_bitrate = (
                        numeric / 1000.0
                    )

                elif value.endswith("bits/s"):

                    numeric = float(
                        value[:-6].strip()
                    )

                    st.progress_bitrate = (
                        numeric / 1_000_000.0
                    )

                elif value.endswith("mbits/s"):

                    numeric = float(
                        value[:-7].strip()
                    )

                    st.progress_bitrate = numeric

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # fps
        # --------------------------------------------------------------

        fps = data.get("fps")

        if fps:
            try:
                st.progress_fps = float(fps)

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # speed
        #
        # Examples:
        #
        # 0.98x
        # 1.20x
        # --------------------------------------------------------------

        speed = data.get("speed")

        if speed:
            try:
                value = speed.strip().lower()

                if value.endswith("x"):
                    value = value[:-1]

                st.progress_speed = float(value)

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # out_time
        # --------------------------------------------------------------

        out_time = data.get("out_time")

        if out_time:

            seconds = self._parse_ffmpeg_time(
                out_time
            )

            if seconds is not None:

                st.progress_out_time = seconds
                st.progress_time = seconds

        # --------------------------------------------------------------
        # total_size
        # --------------------------------------------------------------

        total_size = data.get("total_size")

        if total_size:
            try:
                st.progress_total_size = int(
                    total_size
                )

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # drop frames
        # --------------------------------------------------------------

        drop_frames = data.get("drop_frames")

        if drop_frames:
            try:
                st.progress_drop_frames = int(
                    drop_frames
                )

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # duplicate frames
        # --------------------------------------------------------------

        dup_frames = data.get("dup_frames")

        if dup_frames:
            try:
                st.progress_dup_frames = int(
                    dup_frames
                )

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # encoded frame count
        # --------------------------------------------------------------

        frame = data.get("frame")

        if frame:
            try:
                st.progress_frame = int(
                    frame
                )

            except (ValueError, TypeError):
                pass

        # --------------------------------------------------------------
        # Timestamp
        # --------------------------------------------------------------

        st.progress_updated_at = time.time()

        st.touch()

    # ==================================================================
    # FFmpeg time parser
    # ==================================================================

    def _parse_ffmpeg_time(
        self,
        value: str,
    ) -> float | None:
        """
        Convert FFmpeg:

            HH:MM:SS.microseconds

        to seconds.
        """

        try:
            parts = value.strip().split(":")

            if len(parts) != 3:
                return None

            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])

            return (
                hours * 3600.0
                + minutes * 60.0
                + seconds
            )

        except (ValueError, TypeError):
            return None

    # ==================================================================
    # Update bitrate
    # ==================================================================

    def update_bitrate(
        self,
        channel: ChannelContext,
        new_bitrate: float,
    ) -> bool:
        """
        Restart encoder using an Allocator-approved bitrate.

        IMPORTANT:

            This function does NOT own target_bitrate.

        The caller is expected to have already obtained the final
        bitrate from BitrateAllocator.

        This method simply executes the supplied value.

        Returns:
            True  -> new encoder successfully started
            False -> restart failed
        """

        if self.config.no_encode:
            self.logger.warning(
                "Cannot update bitrate while "
                "--no-encode is active."
            )
            return False

        if new_bitrate <= 0.0:
            self.logger.error(
                "Invalid encoder bitrate for CH%03d: %.6f Mbps",
                channel.channel_id,
                new_bitrate,
            )
            return False

        # --------------------------------------------------------------
        # DO NOT DO THIS:
        #
        # channel.state.target_bitrate = new_bitrate
        #
        # Allocator owns target_bitrate.
        # --------------------------------------------------------------

        old = channel.state.target_bitrate

        channel.state.encoder_status = (
            EncoderStatus.UPDATING
        )
        channel.state.touch()

        # --------------------------------------------------------------
        # Verify Allocator has already committed the value.
        # --------------------------------------------------------------

        if abs(
            channel.state.target_bitrate
            - new_bitrate
        ) > 1e-6:

            self.logger.error(
                "Encoder bitrate mismatch CH%03d: "
                "Allocator target=%.4f Mbps, "
                "requested execution=%.4f Mbps",
                channel.channel_id,
                channel.state.target_bitrate,
                new_bitrate,
            )

            channel.state.encoder_status = (
                EncoderStatus.ERROR
            )
            channel.state.touch()

            return False

        # --------------------------------------------------------------
        # Stop current encoder
        # --------------------------------------------------------------

        self._stop_one(channel)

        # --------------------------------------------------------------
        # Start encoder using the already-approved target bitrate.
        #
        # _build_cmd() reads channel.state.target_bitrate.
        # --------------------------------------------------------------

        try:

            self._start_one(channel)

            self.logger.info(
                "CH%03d bitrate execution updated "
                "%.3f → %.3f Mbps (restart)",
                channel.channel_id,
                old,
                new_bitrate,
            )

            return (
                channel.state.encoder_status
                == EncoderStatus.RUNNING
            )

        except Exception as exc:

            channel.state.encoder_status = (
                EncoderStatus.ERROR
            )
            channel.state.touch()

            self.logger.error(
                "Failed to restart CH%03d: %s",
                channel.channel_id,
                exc,
            )

            return False

    # ==================================================================
    # Stop one
    # ==================================================================

    def _stop_one(
        self,
        channel: ChannelContext,
    ) -> None:
        """
        Safely stop one encoder and its progress reader.
        """

        proc = (
            channel.ffmpeg_proc
            or self._procs.get(
                channel.channel_id
            )
        )

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # Stop progress reader before terminating FFmpeg.
        # --------------------------------------------------------------

        self._stop_progress_reader(
            channel.channel_id,
            join_timeout=1.0,
        )

        if proc is None:
            return

        channel.state.encoder_status = (
            EncoderStatus.STOPPING
        )
        channel.state.touch()

        try:

            # ----------------------------------------------------------
            # Check whether process already exited.
            # ----------------------------------------------------------

            if proc.poll() is None:

                proc.terminate()

                try:
                    proc.wait(
                        timeout=5
                    )

                except subprocess.TimeoutExpired:

                    self.logger.warning(
                        "Encoder CH%03d did not terminate "
                        "within timeout; killing.",
                        channel.channel_id,
                    )

                    proc.kill()

                    proc.wait(
                        timeout=2
                    )

        except Exception as exc:

            self.logger.warning(
                "Error stopping CH%03d: %s",
                channel.channel_id,
                exc,
            )

        finally:

            channel.ffmpeg_proc = None

            self._procs.pop(
                channel.channel_id,
                None,
            )

            self._progress_stop.pop(
                channel.channel_id,
                None,
            )

            self._progress_threads.pop(
                channel.channel_id,
                None,
            )

            channel.state.encoder_status = (
                EncoderStatus.STOPPED
            )
            channel.state.touch()

    # ==================================================================
    # Stop all
    # ==================================================================

    def stop_all(
        self,
        channels: List[ChannelContext] | None = None,
    ) -> None:
        """
        Stop all encoder processes.

        If ChannelContext objects are supplied, their state is also
        updated correctly.
        """

        # --------------------------------------------------------------
        # Preferred path: use ChannelContext objects.
        # --------------------------------------------------------------

        if channels is not None:

            for channel in channels:
                self._stop_one(channel)

            self.logger.info(
                "All encoders stopped"
            )

            return

        # --------------------------------------------------------------
        # Fallback: process-only cleanup.
        # --------------------------------------------------------------

        for channel_id in list(
            self._procs.keys()
        ):

            proc = self._procs.get(
                channel_id
            )

            if proc is None:
                continue

            # Stop progress reader as well.
            self._stop_progress_reader(
                channel_id,
                join_timeout=1.0,
            )

            try:

                if proc.poll() is None:

                    proc.terminate()

                    try:
                        proc.wait(
                            timeout=3
                        )

                    except subprocess.TimeoutExpired:

                        proc.kill()

                        proc.wait(
                            timeout=2
                        )

            except Exception as exc:

                self.logger.warning(
                    "Error stopping encoder CH%03d: %s",
                    channel_id,
                    exc,
                )

        self._procs.clear()
        self._progress_stop.clear()
        self._progress_threads.clear()

        self.logger.info(
            "All encoders stopped"
        )

    # ==================================================================
    # Health
    # ==================================================================

    def check_health(
        self,
        channel: ChannelContext,
    ) -> bool:
        """
        Check whether the channel's FFmpeg process is currently alive.

        This is intentionally separate from startup verification so
        Monitor can periodically call it later.
        """

        proc = (
            channel.ffmpeg_proc
            or self._procs.get(
                channel.channel_id
            )
        )

        if proc is None:

            channel.state.encoder_status = (
                EncoderStatus.ERROR
            )
            channel.state.touch()

            return False

        return_code = proc.poll()

        if return_code is None:

            if (
                channel.state.encoder_status
                not in (
                    EncoderStatus.STARTING,
                    EncoderStatus.UPDATING,
                )
            ):
                channel.state.encoder_status = (
                    EncoderStatus.RUNNING
                )
                channel.state.touch()

            return True

        # --------------------------------------------------------------
        # Process has exited.
        # --------------------------------------------------------------

        channel.state.encoder_status = (
            EncoderStatus.ERROR
        )
        channel.state.touch()

        self.logger.error(
            "Encoder CH%03d is no longer running "
            "(return code=%s)",
            channel.channel_id,
            return_code,
        )

        return False
