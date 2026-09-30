"""
ASTCIE Media Controller
=======================
Adaptive Real-Time Media / Multiplexing Orchestrator
built on top of ASTCIE V8.1 + V9 engines.
"""

from .models import (
    ChannelContext,
    ChannelState,
    ControllerConfig,
    EncoderAction,
    EncoderStatus,
    OutputMode,
)

__version__ = "1.1.0"
__all__ = [
    "ChannelContext",
    "ChannelState",
    "ControllerConfig",
    "EncoderAction",
    "EncoderStatus",
    "OutputMode",
]
