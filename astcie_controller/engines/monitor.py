#!/usr/bin/env python3
"""
RuntimeMonitor – collects real FFmpeg runtime metrics.

Monitoring pipeline:

    FFmpeg
       │
       │ -progress pipe:1
       ▼
    Progress Reader
       │
       ▼
    ChannelState
       │
       ├── actual_bitrate
       ├── fps
       ├── speed
       ├── out_time
       ├── total_size
       ├── drop_frames
       ├── dup_frames
       └── encoder_status
       │
       ▼
    Feedback / Decision / ABR

Important:

    target_bitrate
        = Allocator decision

    actual_bitrate
        = measured FFmpeg output

These values must never be treated as identical.
"""

from __future__ import annotations

import logging
import time
from typing import List

from ..models import (
    ChannelContext,
    ControllerConfig,
    EncoderStatus,
)

logger = logging.getLogger("astcie.monitor")


class RuntimeMonitor:
    """
    Runtime monitoring layer.

    This class does NOT control bitrate.

    It only observes encoder/process state and FFmpeg progress.
    """

    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.monitor")

    # ==================================================================
    # Collect
    # ==================================================================

    def collect(
        self,
        channels: List[ChannelContext],
    ) -> None:
        """
        Collect current runtime metrics.

        Metrics are expected to have already been populated by the
        EncoderManager progress reader.

        This method handles:

            process health
            metric synchronization
            actual bitrate exposure
            buffer heuristic
        """

        now = time.time()

        for channel in channels:

            st = channel.state
            proc = channel.ffmpeg_proc

            # ----------------------------------------------------------
            # No process
            # ----------------------------------------------------------

            if proc is None:

                st.encoder_status = (
                    EncoderStatus.STOPPED
                )

                st.actual_bitrate = 0.0

                st.buffer_state = "no_encoder"

                st.touch()

                continue

            # ----------------------------------------------------------
            # Check actual OS process state
            # ----------------------------------------------------------

            ret = proc.poll()

            if ret is not None:

                if ret == 0:
                    st.encoder_status = (
                        EncoderStatus.STOPPED
                    )
                else:
                    st.encoder_status = (
                        EncoderStatus.ERROR
                    )

                # Do NOT fake bitrate after process exit.
                st.actual_bitrate = 0.0

                st.buffer_state = "stopped"

                st.touch()

                self.logger.warning(
                    "CH%03d encoder exited | return_code=%s",
                    channel.channel_id,
                    ret,
                )

                continue

            # ----------------------------------------------------------
            # Process is alive.
            # ----------------------------------------------------------

            st.encoder_status = (
                EncoderStatus.RUNNING
            )

            # ----------------------------------------------------------
            # IMPORTANT:
            #
            # actual_bitrate comes from FFmpeg progress.
            #
            # Never:
            #
            # actual_bitrate = target_bitrate
            #
            # If no progress sample has arrived yet, keep the previous
            # measured value rather than inventing one.
            # ----------------------------------------------------------

            if st.progress_updated_at > 0:

                st.actual_bitrate = (
                    st.progress_bitrate
                )

            # ----------------------------------------------------------
            # Buffer heuristic
            #
            # This is intentionally simple for v1.
            # Later it can be replaced by transport/network buffer
            # measurements.
            # ----------------------------------------------------------

            st.buffer_state = (
                self._buffer_state(channel)
            )

            st.touch()

        self.logger.debug(
            "Monitor collected metrics for %d channels",
            len(channels),
        )

    # ==================================================================
    # Buffer heuristic
    # ==================================================================

    def _buffer_state(
        self,
        channel: ChannelContext,
    ) -> str:
        """
        Lightweight local buffer/encoder health heuristic.

        This is NOT network buffer measurement.

        It only describes encoder progress health.
        """

        st = channel.state

        if st.progress_updated_at <= 0:
            return "unknown"

        # --------------------------------------------------------------
        # Encoder producing frames slower than realtime.
        # --------------------------------------------------------------

        if (
            st.progress_speed > 0.0
            and st.progress_speed < 0.75
        ):
            return "degrading"

        # --------------------------------------------------------------
        # Encoder around realtime.
        # --------------------------------------------------------------

        if (
            st.progress_speed > 0.0
            and st.progress_speed < 1.10
        ):
            return "ok"

        # --------------------------------------------------------------
        # Encoder comfortably ahead of realtime.
        # --------------------------------------------------------------

        if st.progress_speed >= 1.10:
            return "healthy"

        return "unknown"

    # ==================================================================
    # Channel snapshot
    # ==================================================================

    def snapshot(
        self,
        channel: ChannelContext,
    ) -> dict:
        """
        Return a monitor snapshot suitable for logging, debugging,
        feedback or future telemetry.

        No bitrate is modified here.
        """

        st = channel.state

        return {
            "channel_id": channel.channel_id,
            "encoder_status": (
                st.encoder_status.value
                if hasattr(st.encoder_status, "value")
                else str(st.encoder_status)
            ),

            # ----------------------------------------------------------
            # Allocator target
            # ----------------------------------------------------------

            "target_bitrate": st.target_bitrate,

            # ----------------------------------------------------------
            # Actual measured encoder bitrate
            # ----------------------------------------------------------

            "actual_bitrate": st.actual_bitrate,

            # ----------------------------------------------------------
            # FFmpeg progress
            # ----------------------------------------------------------

            "fps": st.progress_fps,
            "speed": st.progress_speed,
            "out_time": st.progress_out_time,
            "total_size": st.progress_total_size,
            "drop_frames": st.progress_drop_frames,
            "dup_frames": st.progress_dup_frames,
            "frame": st.progress_frame,

            "buffer_state": st.buffer_state,

            "progress_updated_at": (
                st.progress_updated_at
            ),
        }

    # ==================================================================
    # Batch snapshot
    # ==================================================================

    def snapshots(
        self,
        channels: List[ChannelContext],
    ) -> list[dict]:
        """
        Return snapshots for all channels.
        """

        return [
            self.snapshot(channel)
            for channel in channels
        ]
