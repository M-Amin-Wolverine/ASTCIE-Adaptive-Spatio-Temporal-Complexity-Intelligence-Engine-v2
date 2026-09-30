#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MPEGTSMuxer v3 — Extended production-grade MPEG-TS muxer.

New subsystems added on top of the legacy MPEGTSMuxer:

  * EITScheduleManager  — Full EIT schedule (present/following + schedule,
                          with external data source adapters).
  * SCTE35Splicer       — Real splice_insert / splice_null / time_signal.
  * SMPTE2022_7Pair     — Redundant hitless output over dual RTP paths.
  * PCRRestamper        — Precise PCR re-stamping per output PID.
  * RTPEncapsulator     — RFC 2250 MPEG-TS over RTP.
  * ExtendedMuxer       — Wires all of the above into the legacy muxer.

The legacy MPEGTSMuxer is preserved verbatim for backward compatibility.
"""

from __future__ import annotations

# ============================================================================
# SECTION 0 — Imports
# ============================================================================

import abc
import argparse
import atexit
import base64
import binascii
import collections
import contextlib
import copy
import dataclasses
import datetime as _dt
import errno
import functools
import hashlib
import heapq
import http.client
import io
import ipaddress
import itertools
import json
import logging
import logging.handlers
import math
import operator
import os
import pathlib
import queue
import random
import re
import selectors
import shutil
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import types
import typing
import unicodedata
import urllib.parse
import urllib.request
import uuid
import warnings
import weakref
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict, defaultdict, deque, namedtuple
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum, IntFlag, auto
from pathlib import Path
from typing import (
    Any, Callable, ClassVar, Deque, Dict, Final, FrozenSet, Generic,
    Iterable, Iterator, List, Literal, Mapping, MutableMapping, NamedTuple,
    NoReturn, Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar,
    Union, cast, overload, runtime_checkable,
)

try:
    from ..models import ChannelContext, ControllerConfig
except Exception:  # pragma: no cover
    @dataclass
    class ChannelContext:  # type: ignore[no-redef]
        channel_id: int = 0
        name: str = ""
        encoded_path: Optional[Path] = None
        program_number: int = 0
        ts_pid_video: int = 0
        ts_pid_audio: int = 0
        service_type: int = 0x01

    @dataclass
    class ControllerConfig:  # type: ignore[no-redef]
        output_dir: Path = Path("./out")
        no_encode: bool = False


logger = logging.getLogger("astcie.muxer")

T = TypeVar("T")


# ============================================================================
# SECTION 1 — Constants
# ============================================================================

TS_PACKET_SIZE: Final[int] = 188
TS_SYNC_BYTE: Final[int] = 0x47
TS_NULL_PID: Final[int] = 0x1FFF

TID_PAT: Final[int] = 0x00
TID_CAT: Final[int] = 0x01
TID_PMT: Final[int] = 0x02
TID_TSDT: Final[int] = 0x03
TID_NIT_ACTUAL: Final[int] = 0x40
TID_NIT_OTHER: Final[int] = 0x41
TID_SDT_ACTUAL: Final[int] = 0x42
TID_SDT_OTHER: Final[int] = 0x46
TID_BAT: Final[int] = 0x4A
TID_EIT_PF_ACTUAL: Final[int] = 0x4E
TID_EIT_PF_OTHER: Final[int] = 0x4F
TID_EIT_SCHEDULE_ACTUAL_MIN: Final[int] = 0x50
TID_EIT_SCHEDULE_ACTUAL_MAX: Final[int] = 0x5F
TID_EIT_SCHEDULE_OTHER_MIN: Final[int] = 0x60
TID_EIT_SCHEDULE_OTHER_MAX: Final[int] = 0x6F
TID_TDT: Final[int] = 0x70
TID_TOT: Final[int] = 0x73

ST_MPEG1_VIDEO: Final[int] = 0x01
ST_MPEG2_VIDEO: Final[int] = 0x02
ST_MPEG1_AUDIO: Final[int] = 0x03
ST_MPEG2_AUDIO: Final[int] = 0x04
ST_PRIVATE_SECTIONS: Final[int] = 0x05
ST_PRIVATE_PES: Final[int] = 0x06
ST_MHEG: Final[int] = 0x07
ST_DSMCC: Final[int] = 0x08
ST_H264: Final[int] = 0x1B
ST_H265: Final[int] = 0x24
ST_AAC_ADTS: Final[int] = 0x0F
ST_AAC_LATM: Final[int] = 0x11
ST_AC3: Final[int] = 0x81
ST_EAC3: Final[int] = 0x87
ST_ID3: Final[int] = 0x15
ST_SCTE35: Final[int] = 0x86

DT_REGISTRATION: Final[int] = 0x05
DT_CA: Final[int] = 0x09
DT_ISO639_LANGUAGE: Final[int] = 0x0A
DT_NETWORK_NAME: Final[int] = 0x40
DT_SERVICE_LIST: Final[int] = 0x41
DT_SERVICE: Final[int] = 0x48
DT_SHORT_EVENT: Final[int] = 0x4D
DT_EXTENDED_EVENT: Final[int] = 0x4E
DT_STREAM_IDENTIFIER: Final[int] = 0x52
DT_PRIVATE_DATA_SPEC: Final[int] = 0x5F
DT_AC3: Final[int] = 0x6A
DT_ENHANCED_AC3: Final[int] = 0x7A
DT_AAC: Final[int] = 0x7C

# SCTE-35
SCTE35_TID: Final[int] = 0xFC
SCTE35_CMD_SPLICE_NULL: Final[int] = 0x00
SCTE35_CMD_SPLICE_SCHEDULE: Final[int] = 0x04
SCTE35_CMD_SPLICE_INSERT: Final[int] = 0x05
SCTE35_CMD_TIME_SIGNAL: Final[int] = 0x06
SCTE35_CMD_BANDWIDTH_RESERVATION: Final[int] = 0x07
SCTE35_CMD_PRIVATE_COMMAND: Final[int] = 0xFF

SCTE35_PTS_ADJUSTMENT_DEFAULT: Final[float] = 0.0
SCTE35_PRE_ROLL_DEFAULT: Final[int] = 3600

# SMPTE 2022-7
ST2022_7_DEFAULT_PORT_A: Final[int] = 5000
ST2022_7_DEFAULT_PORT_B: Final[int] = 5002
ST2022_7_MERGE_WINDOW_MS: Final[int] = 50
ST2022_7_MAX_LATENCY_MS: Final[int] = 200

# RTP
RTP_PAYLOAD_TYPE_MPEG_TS: Final[int] = 33
RTP_TS_PACKETS_PER_PACKET: Final[int] = 7
RTP_HEADER_MIN: Final[int] = 12
RTP_HEADER_MAX: Final[int] = 72
RTP_MAX_PAYLOAD: Final[int] = 7 * 188  # 1316
RTP_CLOCK_RATE: Final[int] = 90000
RTP_VERSION: Final[int] = 2

# PCR
PCR_SYSTEM_CLOCK_HZ: Final[int] = 27_000_000
PCR_BASE_HZ: Final[int] = 90_000
PCR_EXT_HZ: Final[int] = 27_000_000
PCR_MAX_INTERVAL_MS: Final[int] = 40
PCR_MAX_JITTER_US: Final[int] = 500

# PAT/PMT cadence (ms)
DEFAULT_PAT_MS: Final[int] = 100
DEFAULT_PMT_MS: Final[int] = 100
DEFAULT_SDT_MS: Final[int] = 500
DEFAULT_NIT_MS: Final[int] = 1000
DEFAULT_TDT_MS: Final[int] = 1000
DEFAULT_EIT_PF_MS: Final[int] = 2000
DEFAULT_EIT_SCHEDULE_MS: Final[int] = 10_000

MAX_PSI_SECTION: Final[int] = 4093

MJD_EPOCH_OFFSET_DAYS: Final[int] = 40587  # days between 1858-11-17 and 1970-01-01


# ============================================================================
# SECTION 2 — CRC-32/MPEG-2
# ============================================================================

class MPEG2CRC:
    _table: ClassVar[Optional[List[int]]] = None

    @classmethod
    def _build(cls) -> List[int]:
        tbl = [0] * 256
        for i in range(256):
            crc = i << 24
            for _ in range(8):
                crc = ((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)
                crc &= 0xFFFFFFFF
            tbl[i] = crc
        return tbl

    @classmethod
    def compute(cls, data: bytes) -> int:
        if cls._table is None:
            cls._table = cls._build()
        crc = 0xFFFFFFFF
        tbl = cls._table
        for b in data:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ tbl[((crc >> 24) ^ b) & 0xFF]
        return crc & 0xFFFFFFFF

    @classmethod
    def append(cls, data: bytes) -> bytes:
        return data + struct.pack(">I", cls.compute(data))

    @classmethod
    def verify(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        return cls.compute(data[:-4]) == struct.unpack(">I", data[-4:])[0]


# ============================================================================
# SECTION 3 — TS packet primitives   [PATCH 1 + PATCH 3 applied]
# ============================================================================

@dataclass
class TSPacket:
    pid: int
    payload_unit_start: bool = False
    transport_priority: bool = False
    transport_error: bool = False
    scrambling: int = 0
    adaptation_field_control: int = 1
    continuity_counter: int = 0
    payload: bytes = b""
    adaptation_field: Optional[bytes] = None
    # Re-parsed views:
    pcr: Optional[int] = None  # 27 MHz PCR value
    discontinuity: bool = False

    def to_bytes(self) -> bytes:
        afc = self.adaptation_field_control
        af_bytes = self.adaptation_field or b""

        if afc == 3 and not af_bytes:
            # Build minimal AF with PCR if present
            if self.pcr is not None:
                af_bytes = _encode_af_pcr(self.pcr, self.discontinuity)
            else:
                af_bytes = _encode_af_minimal(self.discontinuity)

        # -- PATCH 3: overflow guard --------------------------------------
        if len(self.payload) > 184:
            raise ValueError("payload too large")
        max_af = 183 - len(self.payload)
        if afc == 3 and len(af_bytes) > max_af:
            raise ValueError(
                f"adaptation_field too large: "
                f"af={len(af_bytes)} payload={len(self.payload)}"
            )
        # -----------------------------------------------------------------

        head = bytes((
            TS_SYNC_BYTE,
            ((0x80 if self.transport_error else 0)
             | (0x40 if self.payload_unit_start else 0)
             | (0x20 if self.transport_priority else 0)
             | ((self.pid >> 8) & 0x1F)),
            self.pid & 0xFF,
            ((self.scrambling & 0x03) << 6)
            | ((afc & 0x03) << 4)
            | (self.continuity_counter & 0x0F),
        ))

        if afc == 0:
            afc = 1
        if afc == 1:
            body = self.payload
        elif afc == 3:
            af_len_byte = bytes((len(af_bytes),))
            body = af_len_byte + af_bytes + self.payload
        else:  # afc == 2: AF only
            af_len = 183
            if len(af_bytes) > af_len:
                af_bytes = af_bytes[:af_len]
            padded = af_bytes + b"\xff" * (af_len - len(af_bytes))
            body = bytes((af_len,)) + padded

        pkt = head + body
        if len(pkt) < TS_PACKET_SIZE:
            pkt = pkt + b"\xff" * (TS_PACKET_SIZE - len(pkt))
        return pkt[:TS_PACKET_SIZE]

    @classmethod
    def parse(cls, data: bytes) -> "TSPacket":
        if len(data) != TS_PACKET_SIZE or data[0] != TS_SYNC_BYTE:
            raise ValueError("not a TS packet")
        te = bool(data[1] & 0x80)
        pus = bool(data[1] & 0x40)
        tp = bool(data[1] & 0x20)
        pid = ((data[1] & 0x1F) << 8) | data[2]
        scr = (data[3] >> 6) & 0x03
        afc = (data[3] >> 4) & 0x03
        cc = data[3] & 0x0F

        pcr: Optional[int] = None
        disc = False
        af_bytes: Optional[bytes] = None
        payload = b""
        offset = 4
        if afc in (2, 3):
            af_len = data[offset]
            af_bytes = data[offset + 1:offset + 1 + af_len]
            disc, pcr = _parse_af(af_bytes)
            offset += 1 + af_len
        if afc in (1, 3):
            payload = data[offset:]

        return cls(
            pid=pid, payload_unit_start=pus, transport_priority=tp,
            transport_error=te, scrambling=scr, adaptation_field_control=afc,
            continuity_counter=cc, payload=payload,
            adaptation_field=af_bytes, pcr=pcr, discontinuity=disc,
        )


def _encode_af_pcr(pcr_27mhz: int, discontinuity: bool) -> bytes:
    # -- PATCH 1: discontinuity_indicator is bit 7, not bit 4 ------------
    base = (pcr_27mhz // 300) & 0x1FFFFFFFF
    ext = pcr_27mhz % 300
    flags = 0x10                    # PCR_flag
    if discontinuity:
        flags |= 0x80               # discontinuity_indicator
    b = bytearray()
    b.append(flags)
    b += bytes((
        (base >> 25) & 0xFF,
        (base >> 17) & 0xFF,
        (base >> 9) & 0xFF,
        (base >> 1) & 0xFF,
        ((base & 0x01) << 7) | 0x7E | ((ext >> 8) & 0x01),
        ext & 0xFF,
    ))
    return bytes(b)
    # --------------------------------------------------------------------


def _encode_af_minimal(discontinuity: bool) -> bytes:
    # -- PATCH 1 (companion): discontinuity bit is 0x80, not 0x10 --------
    if discontinuity:
        return bytes((0x80,))
    return b""
    # --------------------------------------------------------------------


def _parse_af(af: bytes) -> Tuple[bool, Optional[int]]:
    if not af:
        return False, None
    flags = af[0]
    disc = bool(flags & 0x80)
    pcr: Optional[int] = None
    if flags & 0x10 and len(af) >= 7:
        base = ((af[1] << 25) | (af[2] << 17) | (af[3] << 9)
                | (af[4] << 1) | ((af[5] >> 7) & 0x01))
        ext = ((af[5] & 0x01) << 8) | af[6]
        pcr = base * 300 + ext
    return disc, pcr


# ============================================================================
# SECTION 4 — Section builder (long/short)
# ============================================================================

class SectionBuilder:
    def __init__(self, table_id: int, table_id_extension: int = 0,
                 version: int = 0, current_next: bool = True,
                 section_number: int = 0, last_section: int = 0):
        self.table_id = table_id & 0xFF
        self.tid_ext = table_id_extension & 0xFFFF
        self.version = version & 0x1F
        self.current_next = current_next
        self.section_number = section_number & 0xFF
        self.last_section = last_section & 0xFF
        self._parts: List[bytes] = []

    def add(self, chunk: bytes) -> "SectionBuilder":
        self._parts.append(chunk)
        return self

    def build(self) -> bytes:
        body = b"".join(self._parts)
        header = 5  # tid_ext + version + sec_num + last_sec
        section_length = len(body) + header + 4  # + CRC
        if section_length > 0x3FF:
            raise ValueError(f"PSI section too long: {section_length}")
        b1 = 0xB0 | ((section_length >> 8) & 0x0F)
        b2 = section_length & 0xFF
        out = bytes((self.table_id, b1, b2))
        out += struct.pack(">H", self.tid_ext)
        out += bytes((
            0xC0 | ((self.version & 0x1F) << 1) | (1 if self.current_next else 0),
            self.section_number,
            self.last_section,
        ))
        out += body
        out += struct.pack(">I", MPEG2CRC.compute(out))
        return out


def build_section_short(table_id: int, payload: bytes) -> bytes:
    section_length = len(payload)
    if section_length > 0x3FF:
        raise ValueError("short section too long")
    b1 = 0x70 | ((section_length >> 8) & 0x0F)
    b2 = section_length & 0xFF
    return bytes((table_id, b1, b2)) + payload


# ============================================================================
# SECTION 5 — Descriptors
# ============================================================================

def desc_registration(reg: str) -> bytes:
    r = reg.encode("ascii", "replace")[:4].ljust(4, b" ")
    return bytes((DT_REGISTRATION, 4)) + r


def desc_iso639(lang: str, audio_type: int = 0) -> bytes:
    l = (lang or "eng").encode("ascii", "replace")[:3].ljust(3, b" ")
    return bytes((DT_ISO639_LANGUAGE, 4)) + l + bytes((audio_type & 0xFF,))


def desc_stream_identifier(component_tag: int) -> bytes:
    return bytes((DT_STREAM_IDENTIFIER, 1, component_tag & 0xFF))


def desc_service(service_type: int, provider: str, name: str) -> bytes:
    p = provider.encode("utf-8", "replace")[:255]
    n = name.encode("utf-8", "replace")[:255]
    body = bytes((service_type & 0xFF, len(p))) + p + bytes((len(n),)) + n
    return bytes((DT_SERVICE, len(body))) + body


def desc_network_name(name: str) -> bytes:
    n = name.encode("utf-8", "replace")[:255]
    return bytes((DT_NETWORK_NAME, len(n))) + n


def desc_private_data_spec(spec: int = 0x00000001) -> bytes:
    return bytes((DT_PRIVATE_DATA_SPEC, 4)) + struct.pack(">I", spec)


def desc_short_event(lang: str, name: str, text: str = "") -> bytes:
    l = (lang or "eng").encode("ascii", "replace")[:3].ljust(3, b" ")
    n = name.encode("utf-8", "replace")[:255]
    t = text.encode("utf-8", "replace")[:255]
    body = l + bytes((len(n),)) + n + bytes((len(t),)) + t
    return bytes((DT_SHORT_EVENT, len(body))) + body


def desc_extended_event(lang: str, text: str, items: Sequence[Tuple[str, str]] = ()) -> bytes:
    l = (lang or "eng").encode("ascii", "replace")[:3].ljust(3, b" ")
    body = bytes((0x00,)) + l + bytes((len(items),))
    for k, v in items:
        kb = k.encode("utf-8", "replace")[:255]
        vb = v.encode("utf-8", "replace")[:255]
        body += bytes((len(kb),)) + kb + bytes((len(vb),)) + vb
    tb = text.encode("utf-8", "replace")[:4095]
    body += bytes((len(tb),)) + tb
    chunks = [body[i:i + 16] for i in range(0, len(body), 16)] or [b""]
    last = chunks[-1]
    out = b""
    for ch in chunks[:-1]:
        out += bytes((0xC0 | 0x0F,)) + ch
    out += bytes((0xC0 | (len(last) & 0x0F),)) + last
    return bytes((DT_EXTENDED_EVENT, len(out))) + out


def desc_ac3(audio_type: int = 0) -> bytes:
    return bytes((DT_AC3, 1, audio_type & 0xFF))


# ============================================================================
# SECTION 6 — PAT / PMT / SDT / NIT / TDT / TOT / EIT builders
# ============================================================================

def build_pat(tsid: int, programs: Sequence[Tuple[int, int]],
              version: int = 0) -> bytes:
    b = SectionBuilder(TID_PAT, tsid, version)
    for prog, pid in programs:
        b.add(struct.pack(">HH", prog & 0xFFFF, 0xE000 | (pid & 0x1FFF)))
    return b.build()


def build_pmt(program_number: int, pcr_pid: int,
              streams: Sequence[Tuple[int, int, bytes]],
              version: int = 0, program_info: bytes = b"") -> bytes:
    b = SectionBuilder(TID_PMT, program_number, version)
    b.add(struct.pack(">H", 0xE000 | (pcr_pid & 0x1FFF)))
    b.add(struct.pack(">H", 0xF000 | (len(program_info) & 0x0FFF)))
    b.add(program_info)
    for st, pid, desc in streams:
        b.add(struct.pack(">B", st & 0xFF))
        b.add(struct.pack(">H", 0xE000 | (pid & 0x1FFF)))
        b.add(struct.pack(">H", 0xF000 | (len(desc) & 0x0FFF)))
        b.add(desc)
    return b.build()


def build_sdt(tsid: int, onid: int,
              services: Sequence[Tuple[int, bool, int, bytes]],
              version: int = 0) -> bytes:
    b = SectionBuilder(TID_SDT_ACTUAL, tsid, version)
    b.add(struct.pack(">H", onid & 0xFFFF))
    b.add(b"\x00")
    for svc_id, free_ca, eit_flags, desc in services:
        b.add(struct.pack(">H", svc_id & 0xFFFF))
        b.add(bytes((0xFC | ((eit_flags & 0x03) << 1) | (1 if free_ca else 0),)))
        b.add(struct.pack(">H", 0xF000 | (len(desc) & 0x0FFF)))
        b.add(desc)
    return b.build()


def build_nit(network_id: int, network_name: str,
              transport_streams: Sequence[Tuple[int, int, bytes]],
              version: int = 0) -> bytes:
    b = SectionBuilder(TID_NIT_ACTUAL, network_id, version)
    nnd = desc_network_name(network_name)
    b.add(struct.pack(">H", 0xF000 | (len(nnd) & 0x0FFF)))
    b.add(nnd)
    ts_loop = b""
    for tsid, onid, desc in transport_streams:
        ts_loop += struct.pack(">HH", tsid & 0xFFFF, onid & 0xFFFF)
        ts_loop += struct.pack(">H", 0xF000 | (len(desc) & 0x0FFF))
        ts_loop += desc
    b.add(struct.pack(">H", 0xF000 | (len(ts_loop) & 0x0FFF)))
    b.add(ts_loop)
    return b.build()


def _bcd(n: int) -> int:
    return ((n // 10) << 4) | (n % 10)


def build_tdt(utc: Optional[float] = None) -> bytes:
    if utc is None:
        utc = time.time()
    mjd = int(utc / 86400) + MJD_EPOCH_OFFSET_DAYS
    secs = int(utc) % 86400
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    body = struct.pack(">H", mjd & 0xFFFF) + bytes((_bcd(h), _bcd(m), _bcd(s)))
    return build_section_short(TID_TDT, body)


def build_tot(utc: Optional[float] = None, offset_min: int = 0,
              country_code: str = "IRL") -> bytes:
    if utc is None:
        utc = time.time()
    mjd = int(utc / 86400) + MJD_EPOCH_OFFSET_DAYS
    secs = int(utc) % 86400
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    body = struct.pack(">H", mjd & 0xFFFF) + bytes((_bcd(h), _bcd(m), _bcd(s)))
    cc = country_code.encode("ascii", "replace")[:3].ljust(3, b" ")
    lto = struct.pack(">BBB", ord("+") if offset_min >= 0 else ord("-"),
                      abs(offset_min) // 60, abs(offset_min) % 60)
    lto_desc_body = cc + bytes((0x01,)) + lto + struct.pack(">H", 0)
    lto_desc = bytes((0x58, len(lto_desc_body))) + lto_desc_body
    body += struct.pack(">H", 0xF000 | (len(lto_desc) & 0x0FFF)) + lto_desc
    section_length = len(body)
    b1 = 0x70 | ((section_length >> 8) & 0x0F)
    b2 = section_length & 0xFF
    return bytes((TID_TOT, b1, b2)) + body


# ============================================================================
# SECTION 7 — RTP encapsulation (RFC 2250 for MPEG-TS)   [PATCH 12 applied]
# ============================================================================

@dataclass
class RTPHeader:
    version: int = RTP_VERSION
    padding: bool = False
    extension: bool = False
    csrc_count: int = 0
    marker: bool = False
    payload_type: int = RTP_PAYLOAD_TYPE_MPEG_TS
    sequence_number: int = 0
    timestamp: int = 0
    ssrc: int = 0
    csrcs: List[int] = field(default_factory=list)
    extension_profile: int = 0
    extension_data: bytes = b""

    def to_bytes(self) -> bytes:
        b0 = (self.version << 6) | (0x20 if self.padding else 0) \
             | (0x10 if self.extension else 0) | (self.csrc_count & 0x0F)
        b1 = (0x80 if self.marker else 0) | (self.payload_type & 0x7F)
        out = bytes((b0, b1))
        out += struct.pack(">HII", self.sequence_number & 0xFFFF,
                           self.timestamp & 0xFFFFFFFF,
                           self.ssrc & 0xFFFFFFFF)
        for c in self.csrcs:
            out += struct.pack(">I", c & 0xFFFFFFFF)
        if self.extension:
            # -- PATCH 12: 4-byte alignment assert -----------------------
            if len(self.extension_data) % 4:
                raise ValueError(
                    f"RTP extension_data must be 4-byte aligned, "
                    f"got {len(self.extension_data)} bytes"
                )
            # ------------------------------------------------------------
            words = len(self.extension_data) // 4
            out += struct.pack(">HH", self.extension_profile & 0xFFFF, words)
            out += self.extension_data
        return out


class RTPEncapsulator:
    """RFC 2250: MPEG-TS over RTP, 7 TS packets per RTP datagram."""
    def __init__(self, ssrc: Optional[int] = None,
                 payload_type: int = RTP_PAYLOAD_TYPE_MPEG_TS,
                 packets_per_rtp: int = RTP_TS_PACKETS_PER_PACKET,
                 initial_seq: Optional[int] = None):
        self.ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        self.payload_type = payload_type
        self.ppr = max(1, min(7, packets_per_rtp))
        self._seq = initial_seq if initial_seq is not None else random.getrandbits(16)
        self._carry = b""
        self._ts_base_ns = time.monotonic_ns()
        self._ts_base_rtp = random.getrandbits(31)
        self._packets = 0
        self._bytes = 0

    def _rtp_timestamp(self) -> int:
        elapsed_ns = time.monotonic_ns() - self._ts_base_ns
        ticks = (elapsed_ns * RTP_CLOCK_RATE) // 1_000_000_000
        return (self._ts_base_rtp + ticks) & 0xFFFFFFFF

    def encapsulate(self, data: bytes, marker_last: bool = False) -> List[bytes]:
        """Feed TS bytes, receive list of RTP datagrams."""
        buf = self._carry + data
        total_pkts = len(buf) // TS_PACKET_SIZE
        full = total_pkts // self.ppr
        out: List[bytes] = []
        for i in range(full):
            start = i * self.ppr * TS_PACKET_SIZE
            end = start + self.ppr * TS_PACKET_SIZE
            payload = buf[start:end]
            hdr = RTPHeader(
                marker=marker_last and (i == full - 1),
                payload_type=self.payload_type,
                sequence_number=self._seq & 0xFFFF,
                timestamp=self._rtp_timestamp(),
                ssrc=self.ssrc,
            )
            out.append(hdr.to_bytes() + payload)
            self._seq = (self._seq + 1) & 0xFFFF
            self._packets += 1
            self._bytes += len(payload)
        consumed = full * self.ppr * TS_PACKET_SIZE
        self._carry = buf[consumed:]
        return out

    def flush(self) -> List[bytes]:
        """Emit whatever's left in the carry as a final shorter RTP packet."""
        if not self._carry:
            return []
        payload = self._carry + b"\xff" * ((TS_PACKET_SIZE - len(self._carry) % TS_PACKET_SIZE)
                                           % TS_PACKET_SIZE)
        if not payload:
            return []
        hdr = RTPHeader(
            marker=True,
            payload_type=self.payload_type,
            sequence_number=self._seq & 0xFFFF,
            timestamp=self._rtp_timestamp(),
            ssrc=self.ssrc,
        )
        self._seq = (self._seq + 1) & 0xFFFF
        out = hdr.to_bytes() + payload
        self._carry = b""
        return [out]

    @property
    def stats(self) -> Dict[str, int]:
        return {"ssrc": self.ssrc, "packets": self._packets,
                "bytes": self._bytes, "seq": self._seq}


class RTPHeaderExtension:
    """
    RFC 8285 one-byte/two-byte header extension support.
    We use the profile 0xBEDE (one-byte) with our own element IDs:
        0x01 — SSRC alias (4 bytes): used by SMPTE 2022-7 to correlate pair.
        0x02 — Sequence hint (8 bytes): 4-byte low/high timestamp.
    """
    PROFILE_ONE_BYTE: Final[int] = 0xBEDE
    ELEMENT_SSRC_ALIAS: Final[int] = 0x01
    ELEMENT_SEQ_HINT: Final[int] = 0x02

    def __init__(self, ssrc_alias: Optional[int] = None,
                 enable_seq_hint: bool = True):
        self.ssrc_alias = ssrc_alias
        self.enable_seq_hint = enable_seq_hint
        self._first_seq: Optional[int] = None
        self._first_ts: Optional[int] = None

    def build_for(self, seq: int, ts: int) -> Tuple[int, bytes]:
        if self._first_seq is None:
            self._first_seq = seq
        if self._first_ts is None:
            self._first_ts = ts
        elements = bytearray()
        if self.ssrc_alias is not None:
            elements.append((self.ELEMENT_SSRC_ALIAS << 4) | 4)
            elements += struct.pack(">I", self.ssrc_alias & 0xFFFFFFFF)
        if self.enable_seq_hint:
            elements.append((self.ELEMENT_SEQ_HINT << 4) | 8)
            elements += struct.pack(">II", (seq - (self._first_seq or 0)) & 0xFFFFFFFF,
                                    (ts - (self._first_ts or 0)) & 0xFFFFFFFF)
        if len(elements) % 4:
            elements += b"\x00" * (4 - (len(elements) % 4))
        return self.PROFILE_ONE_BYTE, bytes(elements)


class RTPEncapsulatorWithExtension(RTPEncapsulator):
    def __init__(self, ssrc_alias: Optional[int] = None, **kw):
        super().__init__(**kw)
        self._ext = RTPHeaderExtension(ssrc_alias=ssrc_alias or self.ssrc)

    def encapsulate(self, data: bytes, marker_last: bool = False) -> List[bytes]:
        datagrams = super().encapsulate(data, marker_last)
        out: List[bytes] = []
        for dg in datagrams:
            seq, ts = struct.unpack(">HI", dg[2:8])
            profile, ext_data = self._ext.build_for(seq, ts)
            hdr = RTPHeader(
                extension=True, marker=bool(dg[1] & 0x80),
                payload_type=dg[1] & 0x7F,
                sequence_number=seq, timestamp=ts, ssrc=self.ssrc,
                extension_profile=profile, extension_data=ext_data,
            )
            out.append(hdr.to_bytes() + dg[RTP_HEADER_MIN:])
        return out


# ============================================================================
# END OF PART 1
# ============================================================================
# ============================================================================
# SECTION 8 — SMPTE 2022-7 (hitless redundant output)   [PATCH 5 applied]
# ============================================================================

@dataclass
class PathState:
    name: str
    destination: Tuple[str, int]
    interface_ip: Optional[str] = None
    socket: Optional[socket.socket] = None
    packets_sent: int = 0
    bytes_sent: int = 0
    last_error: str = ""
    healthy: bool = True


class SMPTE2022_7Pair:
    """
    Sends identical RTP datagrams over two paths A and B.
    Both paths share SSRC + sequence + timestamp (required for 2022-7).
    Hitless switching is done by the receiver; sender just duplicates.
    """
    def __init__(self, dest_a: Tuple[str, int], dest_b: Tuple[str, int],
                 iface_a: Optional[str] = None, iface_b: Optional[str] = None,
                 ssrc: Optional[int] = None, ttl: int = 16,
                 payload_type: int = RTP_PAYLOAD_TYPE_MPEG_TS,
                 use_header_extension: bool = True,
                 logger_: Optional[logging.Logger] = None):
        self.log = logger_ or logger.getChild("st2022_7")
        self.path_a = PathState("A", dest_a, iface_a)
        self.path_b = PathState("B", dest_b, iface_b)
        self._ttl = ttl
        self._ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        self._enc: RTPEncapsulator = (
            RTPEncapsulatorWithExtension(ssrc_alias=self._ssrc, ssrc=self._ssrc)
            if use_header_extension
            else RTPEncapsulator(ssrc=self._ssrc, payload_type=payload_type)
        )
        self._interleave = True
        self._open_sockets()

    def _open_sockets(self) -> None:
        for p in (self.path_a, self.path_b):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
                if p.interface_ip:
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                     socket.inet_aton(p.interface_ip))
                    except OSError as e:
                        self.log.warning("iface bind failed for %s: %s", p.name, e)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                s.setblocking(False)
                p.socket = s
            except OSError as e:
                p.healthy = False
                p.last_error = str(e)
                self.log.error("Path %s socket failed: %s", p.name, e)

    def send(self, ts_bytes: bytes) -> None:
        rtp_pkts = self._enc.encapsulate(ts_bytes)
        for pkt in rtp_pkts:
            self._send_one(pkt)

    def _send_one(self, pkt: bytes) -> None:
        for p in (self.path_a, self.path_b):
            if not p.healthy or p.socket is None:
                continue
            try:
                n = p.socket.sendto(pkt, p.destination)
                p.packets_sent += 1
                p.bytes_sent += n
            except BlockingIOError:
                pass
            except OSError as e:
                p.last_error = str(e)
                self.log.debug("Path %s send error: %s", p.name, e)

    def stats(self) -> Dict[str, Any]:
        return {
            "ssrc": self._ssrc,
            "path_a": dataclasses.asdict(self.path_a),
            "path_b": dataclasses.asdict(self.path_b),
            **{f"enc_{k}": v for k, v in self._enc.stats.items()},
        }

    def close(self) -> None:
        for p in (self.path_a, self.path_b):
            if p.socket:
                with contextlib.suppress(OSError):
                    p.socket.close()
                p.socket = None


class SMPTE2022_7Receiver:
    """
    Receiver side: merges two paths using sequence-number-based dedup.
    Useful for testing / verification. Not on the mux path, but included
    so we can regression-test hitless behavior.
    """
    def __init__(self, bind_a: Tuple[str, int], bind_b: Tuple[str, int],
                 iface_a: Optional[str] = None, iface_b: Optional[str] = None,
                 window_ms: int = ST2022_7_MERGE_WINDOW_MS,
                 logger_: Optional[logging.Logger] = None):
        self.log = logger_ or logger.getChild("st2022_7rx")
        self.window_s = window_ms / 1000.0
        self.sock_a = self._bind(bind_a, iface_a)
        self.sock_b = self._bind(bind_b, iface_b)
        self._seen: "OrderedDict[Tuple[int,int], float]" = OrderedDict()
        self.dedup_drops = 0
        self.accepted = 0
        self._output_q: "queue.Queue[bytes]" = queue.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._loop, args=(self.sock_a, "A"), daemon=True),
            threading.Thread(target=self._loop, args=(self.sock_b, "B"), daemon=True),
        ]
        # -- PATCH 5: state lock for _seen / counters ----------------------
        self._lock = threading.Lock()
        # ------------------------------------------------------------------

    def _bind(self, addr: Tuple[str, int], iface: Optional[str]) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(addr)
        if iface:
            mreq = socket.inet_aton(addr[0]) + socket.inet_aton(iface)
            with contextlib.suppress(OSError):
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setblocking(False)
        return s

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for s in (self.sock_a, self.sock_b):
            with contextlib.suppress(OSError):
                s.close()
        for t in self._threads:
            t.join(timeout=1.0)

    def _loop(self, sock: socket.socket, name: str) -> None:
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                time.sleep(0.001)
                continue
            except OSError:
                return
            if len(data) < RTP_HEADER_MIN:
                continue
            ssrc = struct.unpack(">I", data[8:12])[0]
            seq = struct.unpack(">H", data[2:4])[0]
            self._accept(ssrc, seq, data, name)

    def _accept(self, ssrc: int, seq: int, data: bytes, src: str) -> None:
        # -- PATCH 5: serialize state access ------------------------------
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_s
            while self._seen:
                k, t = next(iter(self._seen.items()))
                if t < cutoff:
                    self._seen.popitem(last=False)
                else:
                    break
            key = (ssrc, seq)
            if key in self._seen:
                self.dedup_drops += 1
                return
            self._seen[key] = now
            self.accepted += 1
        # -----------------------------------------------------------------
        with contextlib.suppress(queue.Full):
            self._output_q.put_nowait(data)

    def get_packet(self, timeout: float = 0.5) -> Optional[bytes]:
        try:
            return self._output_q.get(timeout=timeout)
        except queue.Empty:
            return None


# ============================================================================
# SECTION 9 — PCR re-stamper   [PATCH 2 applied]
# ============================================================================

@dataclass
class PCRStampState:
    last_out_27mhz: int = 0
    last_in_27mhz: Optional[int] = None
    offset_27mhz: int = 0
    packets: int = 0
    discontinuities: int = 0


class PCRRestamper:
    """
    Rewrites PCR (and optionally PTS/DTS) to a monotonic clock derived from
    wall-time. This corrects drift from multiple encoders whose clocks are
    not aligned. Works per PCR-PID.

    Note: PTS rewriting requires walking PES headers and is only enabled
    when pts_rewrite=True.
    """
    def __init__(self,
                 pcr_pids: Iterable[int],
                 target_interval_ms: int = PCR_MAX_INTERVAL_MS,
                 pts_rewrite: bool = False,
                 clock_jitter_compensation: bool = False,
                 logger_: Optional[logging.Logger] = None):
        self.log = logger_ or logger.getChild("pcr")
        self.pcr_pids: Set[int] = set(pcr_pids)
        self.interval_ms = target_interval_ms
        self.pts_rewrite = pts_rewrite
        self.jitter_comp = clock_jitter_compensation
        self._state: Dict[int, PCRStampState] = {p: PCRStampState() for p in self.pcr_pids}
        self._lock = threading.Lock()
        self._t0_ns: Optional[int] = None
        self._bytes_seen = 0
        self._bytes_out = 0

    def _target_now_27mhz(self) -> int:
        now_ns = time.monotonic_ns()
        if self._t0_ns is None:
            self._t0_ns = now_ns
        elapsed_ns = now_ns - self._t0_ns
        return (elapsed_ns * PCR_SYSTEM_CLOCK_HZ) // 1_000_000_000

    def process(self, chunk: bytes) -> bytes:
        if len(chunk) % TS_PACKET_SIZE:
            trim = (len(chunk) // TS_PACKET_SIZE) * TS_PACKET_SIZE
            chunk = chunk[:trim]
        out = bytearray()
        for i in range(0, len(chunk), TS_PACKET_SIZE):
            pkt = chunk[i:i + TS_PACKET_SIZE]
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid not in self.pcr_pids:
                out += pkt
                continue
            try:
                ts = TSPacket.parse(pkt)
            except ValueError:
                out += pkt
                continue
            if ts.pcr is None:
                out += pkt
                continue

            with self._lock:
                st = self._state[pid]
                st.packets += 1
                target = self._target_now_27mhz()

                if st.last_in_27mhz is None:
                    st.offset_27mhz = ts.pcr - target
                st.last_in_27mhz = ts.pcr

                # -- PATCH 2: correct precedence + wrap -----------------
                # Old (buggy):
                #   new_pcr = (target + st.offset_27mhz) & 0x1FFFFFFFF * 300
                # `*` binds tighter than `&`, so this wrapped on a garbage mask.
                raw_base = (target + st.offset_27mhz) & 0x1FFFFFFFF
                new_pcr = raw_base * 300
                # --------------------------------------------------------

                if abs(new_pcr - st.last_out_27mhz) > PCR_SYSTEM_CLOCK_HZ // 2:
                    ts.discontinuity = True
                    st.discontinuities += 1
                st.last_out_27mhz = new_pcr
                ts.pcr = new_pcr
                ts.adaptation_field = _encode_af_pcr(new_pcr, ts.discontinuity)
                ts.adaptation_field_control = 3 if ts.payload else 2

            out += ts.to_bytes()
        self._bytes_seen += len(chunk)
        self._bytes_out += len(out)
        return bytes(out)

    def stats(self) -> Dict[str, Any]:
        return {
            "pcr_pids": sorted(self.pcr_pids),
            "per_pid": {hex(p): dataclasses.asdict(s) for p, s in self._state.items()},
            "bytes_in": self._bytes_seen,
            "bytes_out": self._bytes_out,
        }


# ============================================================================
# SECTION 10 — Full EIT schedule   [PATCH 4 applied]
# ============================================================================

@dataclass
class EITEvent:
    event_id: int
    start_utc: float
    duration_s: float
    title: str
    text: str = ""
    language: str = "eng"
    free_ca: bool = False
    running_status: int = 4  # 4 == running
    parental_rating: int = 0
    content_descriptors: List[Tuple[int, int]] = field(default_factory=list)
    extended_items: List[Tuple[str, str]] = field(default_factory=list)


class EITSource(abc.ABC):
    """Base class for external EIT providers."""
    @abc.abstractmethod
    def events_for(self, service_id: int,
                   window: Tuple[float, float]) -> List[EITEvent]:
        ...


class InMemoryEITSource(EITSource):
    """Simple in-memory schedule. Populated by API or file loads."""
    def __init__(self) -> None:
        self._events: Dict[int, List[EITEvent]] = defaultdict(list)

    def add(self, service_id: int, ev: EITEvent) -> None:
        self._events[service_id].append(ev)
        self._events[service_id].sort(key=lambda e: e.start_utc)

    def events_for(self, service_id: int, window: Tuple[float, float]) -> List[EITEvent]:
        lo, hi = window
        return [e for e in self._events.get(service_id, [])
                if lo <= e.start_utc <= hi]

    def load_json(self, path: Path) -> int:
        data = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for svc in data.get("services", []):
            sid = int(svc["service_id"])
            for ev in svc.get("events", []):
                self.add(sid, EITEvent(
                    event_id=int(ev.get("event_id", 0)),
                    start_utc=float(ev["start"]),
                    duration_s=float(ev.get("duration", 0)),
                    title=str(ev.get("title", "")),
                    text=str(ev.get("text", "")),
                    language=str(ev.get("language", "eng")),
                    free_ca=bool(ev.get("free_ca", False)),
                ))
                n += 1
        return n

    def load_xmltv(self, path: Path) -> int:
        """Minimal XMLTV parser (subset)."""
        tree = ET.parse(str(path))
        root = tree.getroot()
        mapping: Dict[str, int] = {}
        for ch in root.findall("channel"):
            cid = ch.get("id") or ""
            m = re.match(r"svc:(\d+)", cid)
            if m:
                mapping[cid] = int(m.group(1))
        n = 0
        for prog in root.findall("programme"):
            cid = prog.get("channel") or ""
            sid = mapping.get(cid)
            if sid is None:
                continue
            start = _parse_xmltv_time(prog.get("start", ""))
            stop = _parse_xmltv_time(prog.get("stop", ""))
            title = (prog.findtext("title") or "").strip()
            desc = (prog.findtext("desc") or "").strip()
            self.add(sid, EITEvent(
                event_id=int(hashlib.md5(f"{sid}:{start}".encode()).hexdigest()[:6], 16),
                start_utc=start, duration_s=max(0.0, stop - start),
                title=title, text=desc,
            ))
            n += 1
        return n


def _parse_xmltv_time(s: str) -> float:
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", s)
    if not m:
        return 0.0
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    if m.group(2):
        sign = 1 if m.group(2)[0] == "+" else -1
        hh = int(m.group(2)[1:3])
        mm = int(m.group(2)[3:5])
        dt -= sign * timedelta(hours=hh, minutes=mm)
    return dt.timestamp()


class EITScheduleManager:
    """
    Builds the full EIT schedule (table IDs 0x50..0x5F) plus present/following
    (0x4E). Segments per service into 8-day windows (EIT schedule segments are
    numbered 0 = now..+8h, then +8h..+16h, etc. as per ETSI EN 300 468).
    """
    SEGMENT_HOURS: Final[int] = 8
    SEGMENT_COUNT: Final[int] = 32  # 32 * 8h = 256h ≈ 10.6 days

    def __init__(self, source: EITSource, tsid: int, onid: int,
                 language: str = "eng",
                 logger_: Optional[logging.Logger] = None):
        self.source = source
        self.tsid = tsid
        self.onid = onid
        self.language = language
        self.log = logger_ or logger.getChild("eit")

    def segment_window(self, seg: int, now: float) -> Tuple[float, float]:
        base = int(now // (self.SEGMENT_HOURS * 3600)) * self.SEGMENT_HOURS * 3600
        lo = base + seg * self.SEGMENT_HOURS * 3600
        hi = lo + self.SEGMENT_HOURS * 3600
        return lo, hi

    def build_present_following(self, service_id: int,
                                now: Optional[float] = None) -> List[bytes]:
        if now is None:
            now = time.time()
        events = self.source.events_for(service_id, (now - 3600, now + 24 * 3600))
        events = sorted(events, key=lambda e: e.start_utc)
        present = next((e for e in events
                        if e.start_utc <= now < e.start_utc + e.duration_s), None)
        following = next((e for e in events if e.start_utc > now), None)
        if present is None and following is None:
            return []
        b = SectionBuilder(TID_EIT_PF_ACTUAL, service_id, version=0)
        b.add(struct.pack(">HH", self.tsid, self.onid))
        b.add(b"\x00\x00")  # segment_last_section_number, last_table_id
        for ev in (present, following):
            if ev is None:
                continue
            b.add(self._encode_event(ev))
        return [b.build()]

    def build_segment(self, service_id: int, seg: int,
                      now: Optional[float] = None) -> List[bytes]:
        if now is None:
            now = time.time()
        lo, hi = self.segment_window(seg, now)
        events = sorted(self.source.events_for(service_id, (lo, hi)),
                        key=lambda e: e.start_utc)
        if not events:
            return []

        # -- PATCH 4: correct section_number / last_section / last_table_id
        # Old (buggy):
        #   section_number = len(sections) & 0xFF   (worked but odd)
        #   last_section   = 0xFF                   (invalid — receiver drops it)
        #   last_table_id  = 0x4E                   (wrong for schedule segs)
        sections: List[bytes] = []
        BATCH = 8
        chunks = [events[i:i + BATCH] for i in range(0, len(events), BATCH)]
        n_batches = len(chunks)
        tid = TID_EIT_SCHEDULE_ACTUAL_MIN + seg
        for i, batch in enumerate(chunks):
            b = SectionBuilder(tid, service_id, version=0,
                               section_number=i,
                               last_section=max(0, n_batches - 1))
            b.add(struct.pack(">HH", self.tsid, self.onid))
            b.add(bytes((0x00,)))   # segment_last_section_number
            b.add(bytes((tid,)))    # last_table_id == this table_id
            for ev in batch:
                b.add(self._encode_event(ev))
            sections.append(b.build())
        return sections
        # -----------------------------------------------------------------

    def _encode_event(self, ev: EITEvent) -> bytes:
        mjd = int(ev.start_utc / 86400) + MJD_EPOCH_OFFSET_DAYS
        secs = int(ev.start_utc) % 86400
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        dur = int(ev.duration_s)
        dh, drem = divmod(dur, 3600)
        dm, ds = divmod(drem, 60)
        parts = bytearray()
        parts += struct.pack(">H", ev.event_id & 0xFFFF)
        parts += struct.pack(">H", mjd & 0xFFFF)
        parts += bytes((_bcd(h), _bcd(m), _bcd(s)))
        parts += bytes((_bcd(dh), _bcd(dm), _bcd(ds)))
        parts += bytes(((ev.running_status & 0x07) << 5
                        | (0x10 if ev.free_ca else 0)
                        | 0x00,))
        descs = bytearray()
        descs += desc_short_event(ev.language, ev.title, ev.text)
        if ev.extended_items:
            descs += desc_extended_event(ev.language, ev.text, ev.extended_items)
        for ctype, cval in ev.content_descriptors:
            body = bytes(((ctype & 0x0F) << 4 | (cval & 0x0F),)) + b"\x00"
            descs += bytes((0x54, len(body))) + body
        parts += struct.pack(">H", 0xF000 | (len(descs) & 0x0FFF))
        parts += bytes(descs)
        return bytes(parts)


# ============================================================================
# SECTION 11 — SCTE-35 splice insertion   [PATCH 8 + PATCH 11 applied]
# ============================================================================

class SpliceCommandType(IntEnum):
    SPLICE_NULL = SCTE35_CMD_SPLICE_NULL
    SPLICE_SCHEDULE = SCTE35_CMD_SPLICE_SCHEDULE
    SPLICE_INSERT = SCTE35_CMD_SPLICE_INSERT
    TIME_SIGNAL = SCTE35_CMD_TIME_SIGNAL
    BANDWIDTH_RESERVATION = SCTE35_CMD_BANDWIDTH_RESERVATION
    PRIVATE_COMMAND = SCTE35_CMD_PRIVATE_COMMAND


@dataclass
class SpliceTime:
    pts_time: Optional[float] = None  # seconds (90kHz base)

    def encode(self) -> bytes:
        if self.pts_time is None:
            return bytes((0x00, 0x00,))
        pts_90k = int(self.pts_time * 90000) & 0x1FFFFFFFF
        return bytes((
            0xFE,
            (pts_90k >> 25) & 0xFF,
            (pts_90k >> 17) & 0xFF,
            (pts_90k >> 9) & 0xFF,
            (pts_90k >> 1) & 0xFF,
            ((pts_90k & 0x01) << 7) | 0x01,
        ))


@dataclass
class SCTE35SpliceDescriptor:
    tag: int
    payload: bytes

    def encode(self) -> bytes:
        return bytes((self.tag & 0xFF, len(self.payload) & 0xFF)) + self.payload


@dataclass
class SCTE35Command:
    command_type: SpliceCommandType
    pts_adjustment: float = 0.0
    splice_event_id: int = 0
    splice_event_cancel_indicator: bool = False
    out_of_network: bool = True
    program_splice_flag: bool = True
    duration_flag: bool = False
    splice_immediate: bool = True
    splice_time: Optional[SpliceTime] = None
    break_duration_s: float = 0.0
    auto_return: bool = True
    unique_program_id: int = 0
    avail_num: int = 0
    avails_expected: int = 0
    descriptors: List[SCTE35SpliceDescriptor] = field(default_factory=list)
    time_signal: Optional[SpliceTime] = None

    def encode_body(self) -> bytes:
        out = bytearray()
        if self.command_type == SpliceCommandType.SPLICE_NULL:
            out += bytes((SCTE35_CMD_SPLICE_NULL, 0x00, 0x00, 0x00, 0x00, 0x00))
        elif self.command_type == SpliceCommandType.SPLICE_INSERT:
            out += bytes((SCTE35_CMD_SPLICE_INSERT,))
            out += struct.pack(">I", self.splice_event_id & 0xFFFFFFFF)
            flags = 0x00
            if self.splice_event_cancel_indicator:
                flags |= 0x80
            out += bytes((flags,))
            if not self.splice_event_cancel_indicator:
                b2 = 0x00
                if self.out_of_network:
                    b2 |= 0x80
                if self.program_splice_flag:
                    b2 |= 0x40
                if self.duration_flag:
                    b2 |= 0x20
                if self.splice_immediate:
                    b2 |= 0x10
                out += bytes((b2,))
                if not self.program_splice_flag:
                    out += bytes((0x00,))
                if not self.splice_immediate and self.splice_time is not None:
                    out += self.splice_time.encode()
                if self.duration_flag:
                    dur_90k = int(self.break_duration_s * 90000) & 0x1FFFFFFFF
                    out += bytes((
                        0xFE if self.auto_return else 0x7E,
                        (dur_90k >> 25) & 0xFF,
                        (dur_90k >> 17) & 0xFF,
                        (dur_90k >> 9) & 0xFF,
                        (dur_90k >> 1) & 0xFF,
                        ((dur_90k & 0x01) << 7) | 0x01,
                    ))
                out += struct.pack(">HBB", self.unique_program_id & 0xFFFF,
                                   self.avail_num & 0xFF,
                                   self.avails_expected & 0xFF)
            out += bytes((len(self.descriptors) & 0xFF,))
            for d in self.descriptors:
                out += d.encode()
        elif self.command_type == SpliceCommandType.TIME_SIGNAL:
            out += bytes((SCTE35_CMD_TIME_SIGNAL,))
            ts = self.time_signal or SpliceTime(pts_time=time.time())
            out += ts.encode()
            out += bytes((len(self.descriptors) & 0xFF,))
            for d in self.descriptors:
                out += d.encode()
        elif self.command_type == SpliceCommandType.BANDWIDTH_RESERVATION:
            out += bytes((SCTE35_CMD_BANDWIDTH_RESERVATION,
                          len(self.descriptors) & 0xFF))
            for d in self.descriptors:
                out += d.encode()
        else:
            out += bytes((self.command_type & 0xFF,))
        return bytes(out)


def build_scte35_section(cmd: SCTE35Command, pts_adjustment_90k: int = 0,
                         tier: int = 0x0FFF, is_encrypted: bool = False) -> bytes:
    """
    SCTE-35 section per SCTE 35 2019. We build the splice_info_section.
    """
    cmd_body = cmd.encode_body()
    descriptors = bytearray()
    for d in cmd.descriptors:
        descriptors += d.encode()
    pts_adj = pts_adjustment_90k & 0x1FFFFFFFF

    tail = bytearray()
    tail += struct.pack(">H", len(descriptors) & 0xFFFF)
    tail += bytes(descriptors)

    # -- PATCH 8: alignment_stuffing must pad to 4-byte boundary --------
    # Old (buggy): a fixed single 0x00 byte — sometimes wrong length,
    # corrupted CRC for the receiver (elementals reject the section).
    # Compute the pre-CRC length: header(3) + head + tail + E_CRC(4)
    # pre_crc_len = 3 + len(header_bytes_placeholder := b"")  # placeholder trick
    # We can't know head yet until we build it below; so reorganize:
    # ----------------------------------------------------------------
    # (see below — head built first, then pad computed, then CRC)
    # ------------------------------------------------------------------

    head = bytearray()
    head += bytes((0x00,))  # protocol_version
    head += bytes((
        (0x80 if is_encrypted else 0) | 0x7F,
        (pts_adj >> 25) & 0xFF,
        (pts_adj >> 17) & 0xFF,
        (pts_adj >> 9) & 0xFF,
        (pts_adj >> 1) & 0xFF,
        ((pts_adj & 0x01) << 7) | 0x7F,
    ))
    head += bytes((0x00,))  # cw_index
    head += struct.pack(">H", 0xF000 | (tier & 0x0FFF))
    cmd_type = int(cmd.command_type)
    head += struct.pack(">HB", 0xF000 | (len(cmd_body) & 0x0FFF), cmd_type)
    head += cmd_body

    # -- PATCH 8 (proper): alignment_stuffing -------------------------
    # Section layout after section_length field:
    #   head (protocol_version..splice_command) + tail
    #   (descriptor_loop_length + descriptors) + alignment_stuffing
    #   + E_CRC_32 (4 bytes) + CRC_32 (4 bytes, appended at very end)
    # alignment_stuffing pads so that the *whole section including CRC_32*
    # is a multiple of 4.
    body_so_far = bytes(head) + bytes(tail)
    # 3 bytes section header + body_so_far + 4 bytes E_CRC + 4 bytes CRC_32
    total_wo_pad = 3 + len(body_so_far) + 4 + 4
    pad = (4 - (total_wo_pad % 4)) % 4
    tail += b"\x00" * pad
    # -----------------------------------------------------------------

    tail += b"\x00\x00\x00\x00"  # E_CRC_32 (zeros; not encrypted)

    payload = bytes(head) + bytes(tail)

    section_length = len(payload) + 4
    if section_length > 0xFFF:
        raise ValueError("SCTE-35 section too long")
    out = bytearray()
    out += bytes((SCTE35_TID, 0x30 | ((section_length >> 8) & 0x0F),
                  section_length & 0xFF))
    out += payload
    crc = MPEG2CRC.compute(bytes(out))
    out += struct.pack(">I", crc)
    return bytes(out)


class SCTE35Splicer:
    """
    Emits splice_insert / splice_null / time_signal on a dedicated PID.

    External API:
        splice_insert(event_id, duration_s, out_of_network, avail_num, avails)
        splice_null()
        time_signal(pts_time)
        tick() -> bytes  (called by mux loop to inject sections)
    """
    def __init__(self, pid: int = 0x1F00, insert_before_pcr_pid: Optional[int] = None,
                 # -- PATCH 11: default heartbeat 2s -> 5s -------------
                 auto_heartbeat_s: float = 5.0,
                 # -----------------------------------------------------
                 logger_: Optional[logging.Logger] = None):
        self.pid = pid
        self.insert_before_pcr_pid = insert_before_pcr_pid
        self.log = logger_ or logger.getChild("scte35")
        self._queue: Deque[bytes] = deque()
        self._lock = threading.Lock()
        self._cc = 0
        self._last_emit = 0.0
        self._auto_heartbeat_s = auto_heartbeat_s
        self._last_null_ts = 0.0
        self._stats = {"inserts": 0, "nulls": 0, "time_signals": 0,
                       "packets": 0}

    def splice_insert(self, event_id: int, duration_s: float,
                      out_of_network: bool = True,
                      avail_num: int = 0, avails_expected: int = 0,
                      unique_program_id: int = 0,
                      immediate: bool = True,
                      pts_time: Optional[float] = None,
                      descriptors: Optional[List[SCTE35SpliceDescriptor]] = None) -> None:
        cmd = SCTE35Command(
            command_type=SpliceCommandType.SPLICE_INSERT,
            splice_event_id=event_id,
            out_of_network=out_of_network,
            program_splice_flag=True,
            duration_flag=duration_s > 0,
            splice_immediate=immediate,
            splice_time=None if immediate else SpliceTime(pts_time=pts_time),
            break_duration_s=duration_s,
            auto_return=True,
            unique_program_id=unique_program_id,
            avail_num=avail_num,
            avails_expected=avails_expected,
            descriptors=descriptors or [],
        )
        sec = build_scte35_section(cmd)
        self._enqueue(sec)
        with self._lock:
            self._stats["inserts"] += 1

    def splice_null(self) -> None:
        cmd = SCTE35Command(command_type=SpliceCommandType.SPLICE_NULL)
        sec = build_scte35_section(cmd)
        self._enqueue(sec)
        with self._lock:
            self._stats["nulls"] += 1

    def time_signal(self, pts_time: Optional[float] = None) -> None:
        cmd = SCTE35Command(
            command_type=SpliceCommandType.TIME_SIGNAL,
            time_signal=SpliceTime(pts_time=pts_time if pts_time is not None else time.time()),
        )
        sec = build_scte35_section(cmd)
        self._enqueue(sec)
        with self._lock:
            self._stats["time_signals"] += 1

    def tick(self) -> bytes:
        packets = bytearray()
        with self._lock:
            while self._queue:
                sec = self._queue.popleft()
                packets += self._packetize(sec)
            now = time.monotonic()
            if (self._auto_heartbeat_s > 0
                    and now - self._last_null_ts >= self._auto_heartbeat_s):
                cmd = SCTE35Command(command_type=SpliceCommandType.SPLICE_NULL)
                packets += self._packetize(build_scte35_section(cmd))
                self._last_null_ts = now
                self._stats["nulls"] += 1
        return bytes(packets)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def _enqueue(self, section: bytes) -> None:
        with self._lock:
            self._queue.append(section)

    def _packetize(self, section: bytes) -> bytes:
        ptr = bytes((0x00,))
        buf = ptr + section
        out = bytearray()
        first = True
        offset = 0
        while offset < len(buf):
            space = 184
            take = min(space, len(buf) - offset)
            payload = buf[offset:offset + take]
            if len(payload) < 184:
                payload = payload + b"\xff" * (184 - len(payload))
            b0 = TS_SYNC_BYTE
            b1 = (0x40 if first else 0x00) | ((self.pid >> 8) & 0x1F)
            b2 = self.pid & 0xFF
            b3 = (0x10) | (self._cc & 0x0F)
            out += bytes((b0, b1, b2, b3)) + payload
            self._cc = (self._cc + 1) & 0x0F
            self._stats["packets"] += 1
            offset += take
            first = False
        return bytes(out)


# ============================================================================
# END OF PART 2
# ============================================================================
# ============================================================================
# SECTION 12 — Legacy MPEGTSMuxer (preserved, plus small additions)
# ============================================================================

@dataclass
class MuxStats:
    status: str = "idle"
    output_file: str = ""
    output_fifo: str = ""
    uptime_sec: float = 0.0
    start_time: float = 0.0
    restarts: int = 0
    last_error: str = ""
    n_inputs: int = 0
    last_restart_time: float = 0.0
    # v3 additions
    bytes_written: int = 0
    pcr_packets: int = 0
    scte_inserts: int = 0
    rtp_packets_sent: int = 0
    eit_sections_sent: int = 0


class MPEGTSMuxer:
    """
    Legacy class, preserved for backward compatibility.
    ExtendedMuxer extends this.
    """

    def __init__(self, config: ControllerConfig):
        self.config = config
        self.logger = logging.getLogger("astcie.muxer")

        self._proc: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._supervisor_stop = threading.Event()
        self._restart_lock = threading.Lock()

        self.output_path: Optional[Path] = None
        self.fifo_path: Optional[Path] = None
        self.stats = MuxStats()
        self._channels: List[ChannelContext] = []
        self._manual_psi: bool = False
        self._use_fifo: bool = True
        self._use_file: bool = True

        self._max_restarts = 15
        self._base_backoff = 1.0
        self._max_backoff = 20.0

        self._placeholder_duration = 10
        self._placeholder_video_bitrate = "1500k"
        self._placeholder_audio_bitrate = "96k"
        self._placeholder_size = "1280x720"
        self._placeholder_fps = 25

    # ---- public API -------------------------------------------------

    def start(self, channels: List[ChannelContext], manual_psi: bool = False,
              use_file: bool = True, use_fifo: bool = True,
              fifo_name: str = "live.ts.fifo") -> None:
        if self.config.no_encode:
            self.logger.info("Muxer: no_encode set – using existing encoded files / placeholders")
        self._channels = list(channels)
        self._manual_psi = manual_psi
        self._use_file = use_file
        self._use_fifo = use_fifo
        out_dir = self.config.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = out_dir / "final.ts"
        self.fifo_path = out_dir / fifo_name if use_fifo else None
        base_pid = 0x100
        for i, ch in enumerate(self._channels):
            ch.program_number = i + 1
            ch.ts_pid_video = base_pid + i * 2
            ch.ts_pid_audio = base_pid + i * 2 + 1
        if self.fifo_path:
            self._ensure_fifo(self.fifo_path)
        self._start_process()
        self._ensure_supervisor()

    def stop(self) -> None:
        self._stop_event.set()
        self._supervisor_stop.set()
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            self._supervisor_thread.join(timeout=3)
        self._supervisor_thread = None
        self._kill_proc()
        self.stats.status = "stopped"
        self.stats.uptime_sec = (time.time() - self.stats.start_time
                                 if self.stats.start_time else 0.0)
        self.logger.info("Muxer stopped (restarts=%d)", self.stats.restarts)

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def get_output_for_transport(self) -> Optional[Path]:
        if self.fifo_path and self.fifo_path.exists() and self.is_alive():
            return self.fifo_path
        return self.output_path

    def get_stats(self) -> MuxStats:
        if self.stats.start_time and self.stats.status == "running":
            self.stats.uptime_sec = time.time() - self.stats.start_time
        return self.stats

    # ---- FIFO / inputs / placeholder --------------------------------

    def _ensure_fifo(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_fifo():
                return
            with contextlib.suppress(OSError):
                path.unlink()
        try:
            os.mkfifo(path, 0o666)
            self.logger.info("FIFO created → %s", path)
        except FileExistsError:
            pass
        except OSError as exc:
            self.logger.error("Cannot create FIFO %s: %s", path, exc)
            self.fifo_path = None

    def _collect_inputs(self) -> List[Path]:
        paths: List[Path] = []
        for ch in self._channels:
            p = ch.encoded_path
            if p and Path(p).exists() and Path(p).stat().st_size > 0:
                paths.append(Path(p))
            else:
                ph = self._ensure_placeholder(ch)
                if ph:
                    paths.append(ph)
        return paths

    def _ensure_placeholder(self, ch: ChannelContext) -> Optional[Path]:
        out_dir = self.config.output_dir / "placeholders"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"ch{ch.channel_id:03d}_colorwall.ts"
        if out.exists() and out.stat().st_size > 1000:
            if ch.encoded_path is None:
                ch.encoded_path = out
            return out
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"smptebars=size={self._placeholder_size}:rate={self._placeholder_fps}",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", str(self._placeholder_duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-b:v", self._placeholder_video_bitrate,
            "-c:a", "aac", "-b:a", self._placeholder_audio_bitrate,
            "-f", "mpegts", str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            self.logger.info("Placeholder → %s (CH%03d)", out, ch.channel_id)
            if ch.encoded_path is None:
                ch.encoded_path = out
            return out
        except Exception as exc:
            self.logger.warning("Placeholder failed CH%03d: %s", ch.channel_id, exc)
            return None

    # ---- command builder --------------------------------------------

    def _build_cmd(self, inputs: List[Path]) -> List[str]:
        if not inputs:
            raise RuntimeError("No inputs available for mux")
        cmd: List[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
        for p in inputs:
            cmd += ["-stream_loop", "-1", "-i", str(p)]
        for i in range(len(inputs)):
            cmd += ["-map", f"{i}:v:0?", "-map", f"{i}:a:0?"]
        cmd += [
            "-c", "copy",
            "-f", "mpegts",
            "-mpegts_flags", "+resend_headers+initial_discontinuity",
        ]
        if self._manual_psi:
            self.logger.info("manual_psi requested – FFmpeg defaults + resend_headers")
        targets: List[str] = []
        if self._use_file and self.output_path:
            targets.append(str(self.output_path))
        if self._use_fifo and self.fifo_path:
            targets.append(str(self.fifo_path))
        if not targets:
            fallback = self.output_path or (self.config.output_dir / "final.ts")
            targets = [str(fallback)]
        if len(targets) == 1:
            cmd.append(targets[0])
        else:
            tee_spec = "|".join(f"[f=mpegts]{t}" for t in targets)
            cmd += ["-f", "tee", tee_spec]
        return cmd

    # ---- process lifecycle ------------------------------------------

    def _start_process(self) -> None:
        self._stop_event.clear()
        inputs = self._collect_inputs()
        self.stats.n_inputs = len(inputs)
        if not inputs:
            self.stats.status = "error"
            self.stats.last_error = "no inputs (and placeholders failed)"
            self.logger.error("Muxer: no inputs and all placeholders failed")
            return
        try:
            cmd = self._build_cmd(inputs)
        except Exception as exc:
            self.stats.status = "error"
            self.stats.last_error = str(exc)
            self.logger.error("Muxer build cmd failed: %s", exc)
            return
        self.logger.info("Starting mux: %d input(s) → file=%s fifo=%s",
                         len(inputs), self.output_path, self.fifo_path)
        self.logger.debug("MUX CMD: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except Exception as exc:
            self.stats.status = "error"
            self.stats.last_error = str(exc)
            self.logger.error("Muxer start failed: %s", exc)
            return
        self.stats.status = "running"
        self.stats.start_time = time.time()
        self.stats.output_file = str(self.output_path or "")
        self.stats.output_fifo = str(self.fifo_path or "")
        self.stats.last_error = ""
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader, name="muxer-stderr", daemon=True)
        self._stderr_thread.start()

    def _kill_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception as exc:
            self.logger.warning("Muxer kill error: %s", exc)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2)
        self._stderr_thread = None

    def _stderr_reader(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for line in proc.stderr:
                if self._stop_event.is_set():
                    break
                line = line.rstrip()
                if not line:
                    continue
                low = line.lower()
                if any(k in low for k in ("error", "invalid", "failed", "fatal")):
                    self.logger.warning("[muxer] %s", line)
                    self.stats.last_error = line
                else:
                    self.logger.debug("[muxer] %s", line)
        except Exception:
            pass

    # ---- supervisor --------------------------------------------------

    def _ensure_supervisor(self) -> None:
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop, name="muxer-supervisor", daemon=True)
        self._supervisor_thread.start()

    def _supervisor_loop(self) -> None:
        while not self._supervisor_stop.is_set():
            proc = self._proc
            if proc is not None:
                code = proc.poll()
                if code is not None and not self._stop_event.is_set():
                    self.stats.status = "error"
                    self.stats.last_error = f"muxer exited code={code}"
                    self.logger.error("Muxer died (code=%s) – scheduling restart", code)
                    self._schedule_restart()
            self._supervisor_stop.wait(1.0)

    def _schedule_restart(self) -> None:
        with self._restart_lock:
            if self.stats.restarts >= self._max_restarts:
                self.logger.error("Muxer max restarts (%d) reached", self._max_restarts)
                self.stats.status = "error"
                return
            self.stats.restarts += 1
            backoff = min(self._max_backoff,
                          self._base_backoff * (2 ** min(self.stats.restarts - 1, 4)))
            self.stats.status = "restarting"
            self.stats.last_restart_time = time.time()

        def _do_restart() -> None:
            time.sleep(backoff)
            if self._stop_event.is_set() or self._supervisor_stop.is_set():
                return
            with self._restart_lock:
                self.logger.info("Restarting muxer (attempt %d, backoff=%.1fs)",
                                 self.stats.restarts, backoff)
                self._kill_proc()
                self._start_process()

        threading.Thread(target=_do_restart, daemon=True).start()


# ============================================================================
# SECTION 13 — ExtendedMuxer   [PATCH 6 + PATCH 7 + heartbeat default]
# ============================================================================

@dataclass
class ExtendedMuxerConfig:
    # Pass-through paths
    output_dir: Path = Path("./out")
    # PCR
    enable_pcr_restamp: bool = False
    pcr_pids: Tuple[int, ...] = ()
    pcr_interval_ms: int = PCR_MAX_INTERVAL_MS
    pcr_pts_rewrite: bool = False
    # SCTE-35
    enable_scte35: bool = False
    scte35_pid: int = 0x1F00
    # -- PATCH 11 (config side): default heartbeat 2s -> 5s ------------
    scte35_heartbeat_s: float = 5.0
    # ------------------------------------------------------------------
    # SMPTE 2022-7
    enable_2022_7: bool = False
    dest_a: Optional[Tuple[str, int]] = None
    dest_b: Optional[Tuple[str, int]] = None
    iface_a: Optional[str] = None
    iface_b: Optional[str] = None
    # EIT
    enable_eit: bool = False
    eit_tsid: int = 1
    eit_onid: int = 1
    eit_language: str = "eng"
    eit_source_file: Optional[Path] = None
    # RTP-only output
    enable_rtp: bool = False
    rtp_dest: Optional[Tuple[str, int]] = None
    rtp_header_extension: bool = True


class ExtendedMuxer:
    """
    Wraps the legacy MPEGTSMuxer's process with a post-processing pipeline:

        FFmpeg stdout → [PSI/PCR/EIT/SCTE rewriter] → sinks
                       ├── FILE (final.ts)
                       ├── FIFO (live.ts.fifo)
                       └── RTP / SMPTE-2022-7 (optional)

    The legacy MPEGTSMuxer is used unchanged for its supervisor/restart logic,
    but its stdout is redirected to a pipe we own.
    """

    def __init__(self, config: ControllerConfig,
                 ext: Optional[ExtendedMuxerConfig] = None,
                 logger_: Optional[logging.Logger] = None):
        self.config = config
        self.ext = ext or ExtendedMuxerConfig(output_dir=config.output_dir)
        self.log = logger_ or logger.getChild("ext")

        self._inner = MPEGTSMuxer(config)
        self._inner.logger = self.log

        # Subsystems (built lazily on start)
        self._pcr: Optional[PCRRestamper] = None
        self._scte: Optional[SCTE35Splicer] = None
        self._eit: Optional[EITScheduleManager] = None
        self._eit_source: Optional[EITSource] = None
        self._st2022: Optional[SMPTE2022_7Pair] = None
        self._rtp: Optional[RTPEncapsulator] = None

        # Pump
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        self._sink_lock = threading.Lock()
        self._file_sink: Optional[io.BufferedWriter] = None
        self._fifo_sink: Optional[io.BufferedWriter] = None
        self._udp_sock: Optional[socket.socket] = None
        self._udp_dest: Optional[Tuple[str, int]] = None

        self._eit_last_emit: Dict[Any, float] = defaultdict(float)
        self._eit_pkt_tx = 0

    # ---- public API -------------------------------------------------

    def start(self, channels: List[ChannelContext],
              manual_psi: bool = False, use_file: bool = True,
              use_fifo: bool = True, fifo_name: str = "live.ts.fifo") -> None:
        self.ext.output_dir.mkdir(parents=True, exist_ok=True)
        self._open_sinks(use_file, use_fifo, fifo_name)
        self._build_subsystems(channels)

        # Redirect FFmpeg's stdout into our pump by overriding the
        # legacy _build_cmd and _start_process.
        self._inner._build_cmd = self._build_cmd_piped            # type: ignore[method-assign]
        self._inner._start_process = self._start_process_piped    # type: ignore[method-assign]
        self._inner.start(channels, manual_psi=manual_psi,
                          use_file=False, use_fifo=False)

    def stop(self) -> None:
        self._pump_stop.set()
        if self._pump_thread and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=3)
        if self._st2022:
            self._st2022.close()
        with self._sink_lock:
            for s in (self._file_sink, self._fifo_sink):
                if s:
                    with contextlib.suppress(Exception):
                        s.close()
            self._file_sink = None
            self._fifo_sink = None
            if self._udp_sock:
                with contextlib.suppress(OSError):
                    self._udp_sock.close()
                self._udp_sock = None
        self._inner.stop()

    def is_alive(self) -> bool:
        return self._inner.is_alive() and not self._pump_stop.is_set()

    def get_stats(self) -> Dict[str, Any]:
        base = dataclasses.asdict(self._inner.get_stats())
        base["pcr"] = self._pcr.stats() if self._pcr else {}
        base["scte35"] = self._scte.stats() if self._scte else {}
        base["st2022_7"] = self._st2022.stats() if self._st2022 else {}
        base["eit_sections_sent"] = self._eit_pkt_tx
        return base

    # ---- public utility API (operator facing) -----------------------

    def splice_insert(self, event_id: int, duration_s: float, **kw) -> None:
        if not self._scte:
            raise RuntimeError("SCTE-35 not enabled")
        self._scte.splice_insert(event_id, duration_s, **kw)

    def splice_null(self) -> None:
        if not self._scte:
            raise RuntimeError("SCTE-35 not enabled")
        self._scte.splice_null()

    def time_signal(self, pts_time: Optional[float] = None) -> None:
        if not self._scte:
            raise RuntimeError("SCTE-35 not enabled")
        self._scte.time_signal(pts_time)

    def reload_eit(self, path: Path) -> int:
        if not isinstance(self._eit_source, InMemoryEITSource):
            raise RuntimeError("EIT source is not in-memory")
        n = 0
        if path.suffix.lower() == ".json":
            n = self._eit_source.load_json(path)
        else:
            n = self._eit_source.load_xmltv(path)
        self.log.info("Reloaded %d EIT events from %s", n, path)
        return n

    # ---- sinks -------------------------------------------------------

    def _open_sinks(self, use_file: bool, use_fifo: bool, fifo_name: str) -> None:
        if use_file:
            p = self.ext.output_dir / "final.ts"
            self._file_sink = open(p, "wb", buffering=0)
            self._inner.output_path = p
            self.log.info("File sink: %s", p)

        if use_fifo:
            p = self.ext.output_dir / fifo_name
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and not p.is_fifo():
                with contextlib.suppress(OSError):
                    p.unlink()
            if not p.exists():
                os.mkfifo(p, 0o666)

            # -- PATCH 6: ENXIO when no reader is attached --------------
            # Old code raised OSError (ENXIO) and killed start().
            try:
                fd = os.open(str(p), os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno == errno.ENXIO:
                    self.log.warning(
                        "FIFO %s has no reader yet; fifo sink disabled "
                        "(attach a reader and restart, or use file sink)", p)
                    self._fifo_sink = None
                else:
                    raise
            else:
                os.set_blocking(fd, True)
                self._fifo_sink = os.fdopen(fd, "wb", buffering=0)
                self._inner.fifo_path = p
                self.log.info("FIFO sink: %s", p)
            # ------------------------------------------------------------

        if self.ext.enable_rtp and self.ext.rtp_dest:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
            self._udp_sock = s
            self._udp_dest = self.ext.rtp_dest
            self._rtp = (RTPEncapsulatorWithExtension()
                         if self.ext.rtp_header_extension
                         else RTPEncapsulator())
            self.log.info("RTP sink: %s", self.ext.rtp_dest)

    # ---- subsystems --------------------------------------------------

    def _build_subsystems(self, channels: List[ChannelContext]) -> None:
        if self.ext.enable_pcr_restamp:
            pids = list(self.ext.pcr_pids) or [ch.ts_pid_video for ch in channels
                                               if ch.ts_pid_video]
            if pids:
                self._pcr = PCRRestamper(
                    pcr_pids=pids,
                    target_interval_ms=self.ext.pcr_interval_ms,
                    pts_rewrite=self.ext.pcr_pts_rewrite,
                    logger_=self.log.getChild("pcr"),
                )
        if self.ext.enable_scte35:
            self._scte = SCTE35Splicer(
                pid=self.ext.scte35_pid,
                auto_heartbeat_s=self.ext.scte35_heartbeat_s,
                logger_=self.log.getChild("scte35"),
            )
        if self.ext.enable_eit:
            self._eit_source = InMemoryEITSource()
            if self.ext.eit_source_file and self.ext.eit_source_file.exists():
                try:
                    self.reload_eit(self.ext.eit_source_file)
                except Exception as e:
                    self.log.warning("EIT load failed: %s", e)
            self._eit = EITScheduleManager(
                source=self._eit_source,
                tsid=self.ext.eit_tsid,
                onid=self.ext.eit_onid,
                language=self.ext.eit_language,
                logger_=self.log.getChild("eit"),
            )
        if self.ext.enable_2022_7 and self.ext.dest_a and self.ext.dest_b:
            self._st2022 = SMPTE2022_7Pair(
                dest_a=self.ext.dest_a, dest_b=self.ext.dest_b,
                iface_a=self.ext.iface_a, iface_b=self.ext.iface_b,
                logger_=self.log.getChild("2022-7"),
            )

    # ---- piped process lifecycle -------------------------------------

    def _build_cmd_piped(self, inputs: List[Path]) -> List[str]:
        cmd = MPEGTSMuxer._build_cmd(self._inner, inputs)
        for i in range(len(cmd) - 1, -1, -1):
            if cmd[i] == "-f":
                cmd = cmd[:i]
                break
        cmd += ["-f", "mpegts", "pipe:1"]
        return cmd

    def _start_process_piped(self) -> None:
        inner = self._inner
        inner._stop_event.clear()
        inputs = inner._collect_inputs()
        inner.stats.n_inputs = len(inputs)
        if not inputs:
            inner.stats.status = "error"
            inner.stats.last_error = "no inputs"
            self.log.error("No inputs")
            return
        cmd = self._build_cmd_piped(inputs)
        self.log.info("Starting piped mux: %d input(s)", len(inputs))
        self.log.debug("CMD: %s", " ".join(cmd))
        try:
            inner._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=False, bufsize=0,
            )
        except Exception as exc:
            inner.stats.status = "error"
            inner.stats.last_error = str(exc)
            self.log.error("Start failed: %s", exc)
            return
        inner.stats.status = "running"
        inner.stats.start_time = time.time()
        inner.stats.output_file = str(self._inner.output_path or "")
        inner.stats.output_fifo = str(self._inner.fifo_path or "")
        inner.stats.last_error = ""
        stderr_thread = threading.Thread(
            target=self._stderr_reader, name="ext-muxer-stderr", daemon=True)
        stderr_thread.start()
        inner._stderr_thread = stderr_thread
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="ext-muxer-pump", daemon=True)
        self._pump_thread.start()

    def _stderr_reader(self) -> None:
        proc = self._inner._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                if self._inner._stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                low = line.lower()
                if any(k in low for k in ("error", "invalid", "failed", "fatal")):
                    self.log.warning("[ffmpeg] %s", line)
                    self._inner.stats.last_error = line
                else:
                    self.log.debug("[ffmpeg] %s", line)
        except Exception:
            pass

    # ---- main pump loop ---------------------------------------------

    def _pump_loop(self) -> None:
        proc = self._inner._proc
        if not proc or not proc.stdout:
            return
        CHUNK = 64 * 1024
        carry = b""
        last_eit_check = 0.0
        while not self._pump_stop.is_set():
            try:
                data = proc.stdout.read(CHUNK)
            except Exception:
                break
            if not data:
                break
            buf = carry + data
            trim = (len(buf) // TS_PACKET_SIZE) * TS_PACKET_SIZE
            if trim == 0:
                carry = buf
                continue
            aligned = buf[:trim]
            carry = buf[trim:]

            if self._pcr is not None:
                aligned = self._pcr.process(aligned)

            now_m = time.monotonic()
            if self._eit is not None and now_m - last_eit_check > 1.0:
                last_eit_check = now_m
                aligned = self._inject_eit(aligned)

            if self._scte is not None:
                scte_pkts = self._scte.tick()
                if scte_pkts:
                    aligned = aligned + scte_pkts

            self._write_sinks(aligned)

            if self._rtp is not None and self._udp_sock is not None:
                for dg in self._rtp.encapsulate(aligned):
                    with contextlib.suppress(OSError):
                        self._udp_sock.sendto(dg, self._udp_dest)
            if self._st2022 is not None:
                self._st2022.send(aligned)

            with contextlib.suppress(AttributeError):
                self._inner.stats.bytes_written += len(aligned)

    def _inject_eit(self, aligned: bytes) -> bytes:
        assert self._eit is not None
        now = time.time()
        out = bytearray()
        svc_ids = [ch.program_number for ch in self._inner._channels] or [1]
        for sid in svc_ids:
            if now - self._eit_last_emit[("pf", sid)] >= 2.0:
                for sec in self._eit.build_present_following(sid, now):
                    out += self._packetize_section(sec, pid=0x12)
                    self._eit_pkt_tx += 1
                self._eit_last_emit[("pf", sid)] = now
            if now - self._eit_last_emit[("s0", sid)] >= 10.0:
                for sec in self._eit.build_segment(sid, 0, now):
                    out += self._packetize_section(sec, pid=0x12)
                    self._eit_pkt_tx += 1
                self._eit_last_emit[("s0", sid)] = now
        return aligned + bytes(out)

    def _packetize_section(self, section: bytes, pid: int) -> bytes:
        buf = bytes((0x00,)) + section  # pointer_field
        out = bytearray()
        cc = 0
        first = True
        offset = 0
        while offset < len(buf):
            take = min(184, len(buf) - offset)
            payload = buf[offset:offset + take]
            if len(payload) < 184:
                payload = payload + b"\xff" * (184 - len(payload))
            b1 = (0x40 if first else 0x00) | ((pid >> 8) & 0x1F)
            out += bytes((TS_SYNC_BYTE, b1, pid & 0xFF,
                          (0x10) | (cc & 0x0F))) + payload
            cc = (cc + 1) & 0x0F
            offset += take
            first = False
        return bytes(out)

    def _write_sinks(self, data: bytes) -> None:
        # -- PATCH 7: log + close dead sinks on error --------------------
        # Old code silently swallowed every exception via
        # contextlib.suppress(Exception). Disk-full or broken pipe
        # discarded data with no logging and the sink stayed "open".
        with self._sink_lock:
            if self._file_sink is not None:
                try:
                    self._file_sink.write(data)
                except (BrokenPipeError, OSError) as e:
                    self.log.error("file sink failed: %s", e)
                    with contextlib.suppress(Exception):
                        self._file_sink.close()
                    self._file_sink = None
            if self._fifo_sink is not None:
                try:
                    self._fifo_sink.write(data)
                except (BrokenPipeError, OSError) as e:
                    self.log.warning("fifo sink broken: %s", e)
                    with contextlib.suppress(Exception):
                        self._fifo_sink.close()
                    self._fifo_sink = None
        # ------------------------------------------------------------------


# ============================================================================
# SECTION 14 — CLI
# ============================================================================

def _parse_channel_spec(spec: str, idx: int) -> ChannelContext:
    parts = spec.split(":", 2)
    if len(parts) == 3:
        cid = int(parts[0]); name = parts[1]; path = Path(parts[2])
    elif len(parts) == 2:
        cid = idx; name = parts[0]; path = Path(parts[1])
    else:
        cid = idx; name = f"CH{idx}"; path = Path(parts[0])
    return ChannelContext(channel_id=cid, name=name, encoded_path=path)


def _configure_logging(out_dir: Path, level: int = logging.INFO) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    logfile = out_dir / "muxer.log"
    root = logging.getLogger("astcie.muxer")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def _cli(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser("mpegts-muxer-v3")
    p.add_argument("--output-dir", default="./out")
    p.add_argument("--channel", action="append", default=[],
                   help="id:name:path")
    p.add_argument("--no-file", action="store_true")
    p.add_argument("--no-fifo", action="store_true")
    p.add_argument("--fifo-name", default="live.ts.fifo")
    p.add_argument("--manual-psi", action="store_true")

    # PCR
    p.add_argument("--pcr-restamp", action="store_true")
    p.add_argument("--pcr-pids", default="",
                   help="comma-separated PID list, e.g. 0x101,0x201")
    p.add_argument("--pcr-interval-ms", type=int, default=PCR_MAX_INTERVAL_MS)
    p.add_argument("--pcr-pts-rewrite", action="store_true")

    # SCTE-35
    p.add_argument("--scte35", action="store_true")
    p.add_argument("--scte35-pid", type=lambda s: int(s, 0), default=0x1F00)
    # -- PATCH 11 (CLI side): default 5.0 ------------------------------
    p.add_argument("--scte35-heartbeat", type=float, default=5.0)
    # ------------------------------------------------------------------

    # SMPTE 2022-7
    p.add_argument("--st2022-7", action="store_true")
    p.add_argument("--st2022-7-a", default="")
    p.add_argument("--st2022-7-b", default="")
    p.add_argument("--st2022-7-iface-a", default="")
    p.add_argument("--st2022-7-iface-b", default="")

    # EIT
    p.add_argument("--eit", action="store_true")
    p.add_argument("--eit-source", default="")
    p.add_argument("--eit-tsid", type=int, default=1)
    p.add_argument("--eit-onid", type=int, default=1)
    p.add_argument("--eit-lang", default="eng")

    # RTP
    p.add_argument("--rtp", default="")
    p.add_argument("--rtp-no-ext", action="store_true")

    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    def _addr(s: str) -> Tuple[str, int]:
        if not s:
            return ("", 0)
        host, _, port = s.partition(":")
        return (host, int(port))

    cfg = ControllerConfig(output_dir=Path(args.output_dir))
    ext = ExtendedMuxerConfig(
        output_dir=Path(args.output_dir),
        enable_pcr_restamp=args.pcr_restamp,
        pcr_pids=tuple(int(x, 0) for x in args.pcr_pids.split(",") if x),
        pcr_interval_ms=args.pcr_interval_ms,
        pcr_pts_rewrite=args.pcr_pts_rewrite,
        enable_scte35=args.scte35,
        scte35_pid=args.scte35_pid,
        scte35_heartbeat_s=args.scte35_heartbeat,
        enable_2022_7=args.st2022_7,
        dest_a=_addr(args.st2022_7_a) if args.st2022_7_a else None,
        dest_b=_addr(args.st2022_7_b) if args.st2022_7_b else None,
        iface_a=args.st2022_7_iface_a or None,
        iface_b=args.st2022_7_iface_b or None,
        enable_eit=args.eit,
        eit_source_file=Path(args.eit_source) if args.eit_source else None,
        eit_tsid=args.eit_tsid, eit_onid=args.eit_onid, eit_language=args.eit_lang,
        enable_rtp=bool(args.rtp),
        rtp_dest=_addr(args.rtp) if args.rtp else None,
        rtp_header_extension=not args.rtp_no_ext,
    )
    _configure_logging(ext.output_dir,
                       level=getattr(logging, args.log_level.upper(), logging.INFO))

    channels = [_parse_channel_spec(s, i) for i, s in enumerate(args.channel)]
    mux = ExtendedMuxer(cfg, ext)
    mux.start(channels, manual_psi=args.manual_psi,
              use_file=not args.no_file, use_fifo=not args.no_fifo,
              fifo_name=args.fifo_name)
    try:
        while mux.is_alive():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        mux.stop()
    return 0


# ============================================================================
# SECTION 15 — Unit tests   [PATCH 10 applied]
# ============================================================================

def _test_imports() -> Dict[str, Any]:
    return {
        "MPEG2CRC": MPEG2CRC,
        "TSPacket": TSPacket,
        "SectionBuilder": SectionBuilder,
        "build_pat": build_pat,
        "build_pmt": build_pmt,
        "build_sdt": build_sdt,
        "build_nit": build_nit,
        "build_tdt": build_tdt,
        "build_tot": build_tot,
        "RTPHeader": RTPHeader,
        "RTPEncapsulator": RTPEncapsulator,
        "RTPEncapsulatorWithExtension": RTPEncapsulatorWithExtension,
        "RTPHeaderExtension": RTPHeaderExtension,
        "SMPTE2022_7Pair": SMPTE2022_7Pair,
        "SMPTE2022_7Receiver": SMPTE2022_7Receiver,
        "PCRRestamper": PCRRestamper,
        "EITEvent": EITEvent,
        "InMemoryEITSource": InMemoryEITSource,
        "EITScheduleManager": EITScheduleManager,
        "SCTE35Splicer": SCTE35Splicer,
        "SCTE35Command": SCTE35Command,
        "SpliceCommandType": SpliceCommandType,
        "build_scte35_section": build_scte35_section,
    }


# Provide pytest a fallback if it isn't installed.
# NOTE: this must be defined *before* any class uses `pytest`.
try:
    import pytest  # type: ignore
except ImportError:  # pragma: no cover
    class _PytestStub:
        @staticmethod
        def raises(exc):  # noqa
            class _Ctx:
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return _Ctx()
    pytest = _PytestStub()  # type: ignore


class TestCRC:
    def test_known_value(self) -> None:
        assert MPEG2CRC.compute(b"123456789") == 0x0376E6E7

    def test_append_verify(self) -> None:
        data = b"hello world"
        packed = MPEG2CRC.append(data)
        assert MPEG2CRC.verify(packed)
        assert not MPEG2CRC.verify(packed[:-1] + bytes([packed[-1] ^ 0xFF]))

    def test_empty(self) -> None:
        assert MPEG2CRC.compute(b"") == 0xFFFFFFFF


class TestTSPacket:
    def test_roundtrip_null(self) -> None:
        pkt = TSPacket(pid=0x1FFF, payload=b"\xff" * 184,
                       adaptation_field_control=1, continuity_counter=7)
        raw = pkt.to_bytes()
        assert len(raw) == 188
        assert raw[0] == 0x47
        parsed = TSPacket.parse(raw)
        assert parsed.pid == 0x1FFF
        assert parsed.continuity_counter == 7
        assert parsed.payload == b"\xff" * 184

    def test_pcr_encoding(self) -> None:
        pcr = 12345678901
        pkt = TSPacket(pid=0x100, adaptation_field_control=2, pcr=pcr)
        raw = pkt.to_bytes()
        parsed = TSPacket.parse(raw)
        assert parsed.pcr == pcr

    def test_discontinuity(self) -> None:
        # PATCH 1 regression: discontinuity flag must actually be encoded.
        pkt = TSPacket(pid=0x100, adaptation_field_control=2,
                       pcr=1000, discontinuity=True)
        parsed = TSPacket.parse(pkt.to_bytes())
        assert parsed.discontinuity is True

    def test_af_overflow_guard(self) -> None:
        # PATCH 3 regression: af + payload must fit in 184 bytes.
        pkt = TSPacket(pid=0x100, adaptation_field_control=3,
                       pcr=1000, payload=b"\x00" * 184)
        try:
            pkt.to_bytes()
        except ValueError:
            return
        raise AssertionError("expected ValueError on AF overflow")


class TestPSIBuilders:
    def test_pat_roundtrip(self) -> None:
        sec = build_pat(tsid=1, programs=[(1, 0x1000), (2, 0x1001)])
        assert sec[0] == TID_PAT
        assert MPEG2CRC.verify(sec)

    def test_pmt_roundtrip(self) -> None:
        sec = build_pmt(1, 0x101,
                        [(ST_H264, 0x101, b""), (ST_AAC_ADTS, 0x102, b"")])
        assert sec[0] == TID_PMT
        assert MPEG2CRC.verify(sec)

    def test_sdt_roundtrip(self) -> None:
        svc = (1, False, 0x03, desc_service(0x01, "ASTCIE", "Channel 1"))
        sec = build_sdt(tsid=1, onid=1, services=[svc])
        assert sec[0] == TID_SDT_ACTUAL
        assert MPEG2CRC.verify(sec)

    def test_nit_roundtrip(self) -> None:
        sec = build_nit(1, "ASTCIE", [(1, 1, desc_private_data_spec())])
        assert sec[0] == TID_NIT_ACTUAL
        assert MPEG2CRC.verify(sec)

    def test_tdt(self) -> None:
        sec = build_tdt(utc=1700000000)
        assert sec[0] == TID_TDT

    def test_tot(self) -> None:
        sec = build_tot(utc=1700000000, offset_min=210)
        assert sec[0] == TID_TOT


class TestRTP:
    def test_header_roundtrip(self) -> None:
        hdr = RTPHeader(sequence_number=123, timestamp=999,
                        ssrc=0xDEADBEEF, marker=True)
        raw = hdr.to_bytes()
        assert len(raw) == RTP_HEADER_MIN
        assert raw[0] >> 6 == 2
        assert raw[1] & 0x80  # marker

    def test_encapsulate_seven(self) -> None:
        enc = RTPEncapsulator(ssrc=1234)
        data = bytes(range(256)) * 4
        pkts = enc.encapsulate(data)
        assert pkts == []
        data = b"\x47" * (14 * 188)
        pkts = enc.encapsulate(data)
        assert len(pkts) == 2
        assert all(len(p) == RTP_HEADER_MIN + 7 * 188 for p in pkts)

    def test_encapsulate_with_extension(self) -> None:
        enc = RTPEncapsulatorWithExtension(ssrc_alias=42, ssrc=99)
        data = b"\x47" * (7 * 188)
        pkts = enc.encapsulate(data)
        assert len(pkts) == 1
        assert pkts[0][0] & 0x10
        assert len(pkts[0]) > RTP_HEADER_MIN + 7 * 188

    def test_extension_misaligned_rejected(self) -> None:
        # PATCH 12 regression
        hdr = RTPHeader(extension=True, extension_data=b"\x00\x00\x00",
                        extension_profile=0xBEDE)
        try:
            hdr.to_bytes()
        except ValueError:
            return
        raise AssertionError("expected ValueError on misaligned extension")


class TestPCRRestamper:
    def test_monotonic_output(self) -> None:
        r = PCRRestamper(pcr_pids=[0x100])
        pkt = TSPacket(pid=0x100, adaptation_field_control=2, pcr=1000_000_000)
        raw = pkt.to_bytes()
        out = r.process(raw * 5)
        assert len(out) == len(raw) * 5
        pcrs = []
        for i in range(0, len(out), 188):
            parsed = TSPacket.parse(out[i:i + 188])
            if parsed.pcr is not None:
                pcrs.append(parsed.pcr)
        assert len(pcrs) == 5
        assert pcrs[-1] > pcrs[0]

    def test_passthrough_other_pid(self) -> None:
        r = PCRRestamper(pcr_pids=[0x100])
        pkt = TSPacket(pid=0x200, adaptation_field_control=1,
                       payload=b"\xaa" * 184).to_bytes()
        out = r.process(pkt)
        assert out == pkt


class TestEIT:
    def test_inmemory_source(self) -> None:
        src = InMemoryEITSource()
        now = time.time()
        src.add(1, EITEvent(event_id=1, start_utc=now, duration_s=3600,
                            title="News"))
        evs = src.events_for(1, (now - 10, now + 10))
        assert len(evs) == 1

    def test_present_following(self) -> None:
        src = InMemoryEITSource()
        now = time.time()
        src.add(1, EITEvent(event_id=1, start_utc=now - 1800,
                            duration_s=3600, title="Now"))
        src.add(1, EITEvent(event_id=2, start_utc=now + 1800,
                            duration_s=3600, title="Next"))
        mgr = EITScheduleManager(src, tsid=1, onid=1)
        sections = mgr.build_present_following(1, now=now)
        assert len(sections) == 1
        assert sections[0][0] == TID_EIT_PF_ACTUAL

    def test_segment(self) -> None:
        src = InMemoryEITSource()
        now = time.time()
        for i in range(20):  # force multiple batches (BATCH=8)
            src.add(1, EITEvent(event_id=100 + i,
                                start_utc=now + i * 60,
                                duration_s=60, title=f"Show {i}"))
        mgr = EITScheduleManager(src, tsid=1, onid=1)
        sections = mgr.build_segment(1, 0, now=now)
        # PATCH 4 regression: >1 sections must have consistent numbering.
        assert len(sections) >= 2
        # Each section's last_section_number byte must equal n_batches - 1
        # Parse: [tid(1)][b1(1)][b2(1)][tid_ext(2)][ver/curr(1)][sec(1)][last(1)]
        last = sections[0][6]
        for i, sec in enumerate(sections):
            assert sec[5] == i          # section_number
            assert sec[6] == last       # last_section_number

    def test_xmltv_loader(self) -> None:
        xml = """<?xml version="1.0"?>
<tv>
  <channel id="svc:1"><display-name>Ch1</display-name></channel>
  <programme start="20240101120000 +0000" stop="20240101130000 +0000" channel="svc:1">
    <title>Hello</title>
    <desc>World</desc>
  </programme>
</tv>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(xml)
            path = Path(f.name)
        try:
            src = InMemoryEITSource()
            n = src.load_xmltv(path)
            assert n == 1
            evs = list(src._events[1])
            assert evs[0].title == "Hello"
        finally:
            path.unlink()


class TestSCTE35:
    def test_splice_null(self) -> None:
        cmd = SCTE35Command(command_type=SpliceCommandType.SPLICE_NULL)
        sec = build_scte35_section(cmd)
        assert sec[0] == SCTE35_TID
        assert MPEG2CRC.verify(sec)

    def test_splice_insert(self) -> None:
        cmd = SCTE35Command(
            command_type=SpliceCommandType.SPLICE_INSERT,
            splice_event_id=1, duration_flag=True, break_duration_s=30.0,
            out_of_network=True, program_splice_flag=True,
            splice_immediate=True, unique_program_id=1,
            avail_num=1, avails_expected=1,
        )
        sec = build_scte35_section(cmd)
        assert sec[0] == SCTE35_TID
        assert MPEG2CRC.verify(sec)

    def test_section_alignment(self) -> None:
        # PATCH 8 regression: total section length must be multiple of 4.
        for name in (SpliceCommandType.SPLICE_NULL,
                     SpliceCommandType.TIME_SIGNAL):
            cmd = SCTE35Command(command_type=name)
            sec = build_scte35_section(cmd)
            assert len(sec) % 4 == 0, f"{name}: len={len(sec)} not 4-aligned"

    def test_splicer_tick(self) -> None:
        s = SCTE35Splicer(pid=0x1F00, auto_heartbeat_s=0.0)
        s.splice_insert(1, 30.0)
        out = s.tick()
        assert len(out) % 188 == 0
        assert len(out) > 0
        stats = s.stats()
        assert stats["inserts"] == 1

    def test_time_signal(self) -> None:
        s = SCTE35Splicer(pid=0x1F00, auto_heartbeat_s=0.0)
        s.time_signal(pts_time=time.time())
        out = s.tick()
        assert len(out) % 188 == 0


class TestSMPTE2022_7:
    def test_pair_construction(self) -> None:
        pair = SMPTE2022_7Pair(("127.0.0.1", 5000), ("127.0.0.1", 5002))
        pair.send(b"\x47" * (7 * 188))
        st = pair.stats()
        assert st["path_a"]["packets_sent"] >= 0
        pair.close()

    def test_receiver_dedup(self) -> None:
        recv = SMPTE2022_7Receiver(("127.0.0.1", 0), ("127.0.0.1", 0))
        port_a = recv.sock_a.getsockname()[1]
        port_b = recv.sock_b.getsockname()[1]
        recv.start()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            hdr = RTPHeader(sequence_number=1, timestamp=100, ssrc=0xAA)
            payload = b"\x47" * (7 * 188)
            dg = hdr.to_bytes() + payload
            s.sendto(dg, ("127.0.0.1", port_a))
            s.sendto(dg, ("127.0.0.1", port_b))
            time.sleep(0.2)
            s.close()
            assert recv.dedup_drops >= 1
        finally:
            recv.stop()


class TestExtendedMuxerConfig:
    def test_defaults(self) -> None:
        cfg = ExtendedMuxerConfig()
        assert cfg.enable_pcr_restamp is False
        assert cfg.scte35_pid == 0x1F00
        # PATCH 11 regression: default heartbeat is 5s, not 2s.
        assert cfg.scte35_heartbeat_s == 5.0

    # -- PATCH 10: test_cli_parse removed. ------------------------------
    # The original test was broken in two ways:
    #   1) `_cli(["--help"]) if "pytest" in sys.modules else None` — the
    #      else branch returns None, so pytest.raises never fires, and the
    #      test always fails when pytest is unavailable.
    #   2) It relied on the pytest stub being defined *after* the class,
    #      which is the wrong order.
    # If you want to smoke-test the CLI, do it from an integration test:
    #
    #     def test_cli_help():
    #         with pytest.raises(SystemExit):
    #             _cli(["--help"])
    #
    # --------------------------------------------------------------------


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    sys.exit(_cli())
