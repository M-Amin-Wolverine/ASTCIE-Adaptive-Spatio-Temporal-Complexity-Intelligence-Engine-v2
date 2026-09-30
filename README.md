# ASTCIE — Adaptive Spatio-Temporal Complexity Intelligence Engine v2

> **Intelligent video complexity analysis, hybrid content intelligence, adaptive bitrate allocation, and broadcast-oriented MPEG-TS orchestration.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-supported-green?logo=ffmpeg)](https://ffmpeg.org/)
[![License](https://img.shields.io/badge/License-TBD-lightgrey)](#license)
[![Status](https://img.shields.io/badge/Status-Research%20%2B%20Engineering-orange)](#project-status)

**ASTCIE (Adaptive Spatio-Temporal Complexity Intelligence Engine)** is a research and engineering framework for extracting actionable intelligence from video content before encoding and transmission.

The system analyzes video complexity from multiple complementary perspectives—including **spatial structure, temporal dynamics, motion, texture, events, distributional behavior, burstiness, tails, and extreme observations**—and transforms these measurements into an operational intelligence layer for downstream bitrate allocation and broadcast-oriented encoding.

ASTCIE is designed around a simple principle:

> **Video streams should not be treated as equally expensive content. Their encoding and transmission requirements should be inferred from the content itself.**

---

## Table of Contents

* [Overview](#overview)
* [Motivation](#motivation)
* [What ASTCIE Does](#what-astcie-does)
* [Architecture](#architecture)
* [ASTCIE Intelligence Layers](#astcie-intelligence-layers)
* [V8.1 — Spatial-Temporal Complexity Intelligence](#v81--spatial-temporal-complexity-intelligence)
* [V9 — Distributional Intelligence](#v9--distributional-intelligence)
* [Why V8.1 + V9](#why-v81--v9)
* [Hybrid Intelligence Layer](#hybrid-intelligence-layer)
* [Weight Sensitivity Analysis](#weight-sensitivity-analysis)
* [Benchmark Dataset](#benchmark-dataset)
* [Operational Wrapper](#operational-wrapper)
* [Adaptive Bitrate Allocation](#adaptive-bitrate-allocation)
* [Resolution-Aware Allocation](#resolution-aware-allocation)
* [Parallel Encoding](#parallel-encoding)
* [Multi-Program MPEG-TS](#multi-program-mpeg-ts)
* [FFprobe Validation](#ffprobe-validation)
* [Post-Encode Validation](#post-encode-validation)
* [Scientific Positioning](#scientific-positioning)
* [Limitations](#limitations)
* [Roadmap](#roadmap)
* [Repository Structure](#repository-structure)
* [Installation](#installation)
* [Usage](#usage)
* [Example Workflow](#example-workflow)
* [Research Reproducibility](#research-reproducibility)
* [References](#references)
* [Author](#author)
* [License](#license)

---

# Overview

ASTCIE is built as a **content intelligence layer between raw video and encoding/transmission decisions**.

The project currently combines two complementary analysis engines:

```text
                 INPUT VIDEO
                      │
                      ▼
          ┌────────────────────────┐
          │     ASTCIE Wrapper     │
          └────────────┬───────────┘
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
      ┌──────────────┐    ┌──────────────┐
      │   ASTCIE V8.1│    │   ASTCIE V9  │
      │ complexity7  │    │ complexity8  │
      └──────┬───────┘    └──────┬───────┘
             │                   │
             └─────────┬─────────┘
                       ▼
          ┌─────────────────────────┐
          │ Hybrid Intelligence     │
          │ Layer (HIL)             │
          └────────────┬────────────┘
                       ▼
          ┌─────────────────────────┐
          │ Adaptive Allocation     │
          │ Engine                  │
          └────────────┬────────────┘
                       ▼
             ┌──────────────────┐
             │ Parallel FFmpeg  │
             │ Encoding         │
             └────────┬─────────┘
                      ▼
             ┌──────────────────┐
             │ MPEG-TS Multiplex │
             │ 30 Mb/s Transport │
             └────────┬─────────┘
                      ▼
                ┌───────────┐
                │ FFprobe   │
                │ Validation│
                └─────┬─────┘
                      ▼
              POST-ENCODE ASTCIE
              V8.1 + V9 ANALYSIS
```

The architecture intentionally separates:

1. **Measurement**
2. **Intelligence extraction**
3. **Decision making**
4. **Encoding**
5. **Transport multiplexing**
6. **Validation**

This separation makes the system easier to benchmark, reproduce, extend, and eventually deploy.

---

# Motivation

Modern video systems operate under constrained computational, storage, and transmission budgets.

A fixed bitrate policy assumes that every video segment has approximately the same encoding difficulty.

That assumption is often wrong.

A static allocation can:

* over-provision simple content,
* under-provision highly complex content,
* waste available bandwidth,
* produce visible compression artifacts,
* reduce quality on high-complexity scenes,
* fail to distinguish short extreme events from ordinary content,
* and make poor decisions when multiple channels compete for a fixed transport budget.

ASTCIE investigates whether **content-derived intelligence can be converted into better resource allocation decisions**.

The motivation is consistent with the broader role of spatial and temporal video characteristics in understanding compression difficulty. ITU-T P.910 explicitly identifies spatial information (SI) and temporal information (TI) as important scene characteristics related to the amount of compression possible and resulting impairment.

ASTCIE extends this idea beyond a simple SI/TI pair.

---

# What ASTCIE Does

ASTCIE extracts multiple families of video characteristics.

## Spatial Intelligence

Examples include:

* Spatial Information (SI)
* SI percentile statistics
* Texture characteristics
* Spatial complexity
* Spatial distribution behavior

## Temporal Intelligence

Examples include:

* Temporal Information
* frame-to-frame variation
* temporal percentiles
* temporal burst behavior
* scene transitions
* temporal distribution characteristics

## Motion Intelligence

Examples include:

* Motion Occupancy
* Motion Magnitude
* Motion-related complexity
* temporal activity

## Event Intelligence

Examples include:

* Scene Change Count
* Scene Changes per Minute
* Event density
* burst behavior
* extreme-event detection

## Distributional Intelligence

V9 introduces additional distribution-oriented analysis:

* P50
* P75
* P90
* P99
* P99.5
* P99.9
* Maximum
* Tail behavior
* Extreme severity
* Hill-based tail analysis
* Distributional risk

The goal is to distinguish between:

```text
Typical behavior
       vs.
Rare but operationally important behavior
```

This distinction is one of the central ideas behind the V9 architecture.

---

# ASTCIE Intelligence Layers

ASTCIE can be viewed as a layered intelligence system.

```text
Layer 1
Raw Video
   │
   ▼
Layer 2
Primitive Measurements
   │
   ├── Spatial
   ├── Temporal
   ├── Motion
   ├── Texture
   └── Events
   │
   ▼
Layer 3
Statistical Intelligence
   │
   ├── Median
   ├── Percentiles
   ├── IQR
   └── Distribution
   │
   ▼
Layer 4
Risk / Tail Intelligence
   │
   ├── Burst
   ├── Tail
   ├── Extreme
   ├── Tail Index
   └── Extreme Severity
   │
   ▼
Layer 5
Hybrid Intelligence
   │
   ├── Complexity
   └── BDI
   │
   ▼
Layer 6
Operational Decision
   │
   ├── Bitrate
   ├── Resolution
   ├── Priority
   └── Allocation
```

---

# V8.1 — Spatial-Temporal Complexity Intelligence

ASTCIE V8.1 is the primary **multi-dimensional offline complexity analyzer**.

Repository mapping:

```text
complexity7.py → ASTCIE V8.1
```

V8.1 combines several dimensions of video complexity.

Representative feature families include:

| Feature          | Description                            |
| ---------------- | -------------------------------------- |
| SI               | Spatial information                    |
| SI P50/P90/P99   | Spatial distribution                   |
| MedAD            | Robust temporal activity statistics    |
| Motion Occupancy | Fraction of samples affected by motion |
| Motion Magnitude | Motion intensity                       |
| Texture Mean     | Spatial texture complexity             |
| Scene Changes    | Detected scene transitions             |
| Scene/min        | Scene-change rate                      |
| Spatial          | Normalized spatial intelligence        |
| Temporal         | Normalized temporal intelligence       |
| Motion           | Motion intelligence                    |
| Event            | Event intelligence                     |
| Burst            | Short-duration concentration           |
| Tail             | Tail behavior                          |
| Extreme          | Extreme-event behavior                 |
| Fusion           | Combined V8.1 complexity               |
| BDI              | Behavioral Demand Index                |

V8.1 is therefore not simply an SI/TI calculator.

It is a multi-factor complexity intelligence system.

---

# V9 — Distributional Intelligence

ASTCIE V9 is designed to capture information that can be underrepresented by central statistics.

Repository mapping:

```text
complexity8.py → ASTCIE V9
```

V9 places additional emphasis on the **shape and tails of the complexity distribution**.

Representative outputs include:

* P50
* P75
* P90
* P99
* P99.5
* P99.9
* Maximum
* Risk
* Tail characteristics
* Hill Log Excess
* Hill Alpha
* Extreme Severity
* ADCI
* Regime classification
* V9 Complexity
* V9 BDI

The underlying idea is:

> Two videos can have similar median behavior while having very different extreme-event behavior.

For encoding and transmission systems, that distinction can matter.

For example:

```text
Video A:
Typical complexity = high
Extreme complexity = moderate

Video B:
Typical complexity = moderate
Extreme complexity = very high
```

A system relying only on central statistics may rank these two streams differently from a system that explicitly models the tail.

V9 is intended to expose this additional information.

---

# Why V8.1 + V9?

V9 is **not intended to replace V8.1**.

The two engines overlap substantially, but they are not identical.

A significant portion of their information is shared:

```text
                 ┌────────────────────┐
                 │    Shared Content   │
                 │    Information      │
                 └─────────┬──────────┘
                           │
             ┌─────────────┴─────────────┐
             │                           │
        V8.1-specific               V9-specific
        information                 information
             │                           │
      spatial/temporal             distributional
      motion/texture               tail/extreme
      event fusion                 risk analysis
```

V8.1 provides a broad representation of:

* spatial complexity,
* temporal complexity,
* motion,
* texture,
* events,
* and their fusion.

V9 adds a stronger representation of:

* distributional structure,
* tail behavior,
* rare events,
* extreme values,
* and risk-oriented characteristics.

Therefore the research question is not:

> "Is V9 better than V8.1?"

Instead, the question is:

> **Does combining V8.1 and V9 produce a more robust representation of video demand than either engine alone?**

That is the motivation for the Hybrid Intelligence Layer.

---

# Hybrid Intelligence Layer

The current baseline uses a simple and intentionally transparent fusion strategy.

## Complexity

Let:

* $$\(C_{8.1}\)$$ = V8.1 complexity
* $$\(C_9\)$$ = V9 complexity

Then:

$$C_H = 0.5C_{8.1} + 0.5C_9$$

## Behavioral Demand Index

Similarly:

$$BDI_H = 0.5BDI_{8.1} + 0.5BDI_9$$

The 50/50 configuration is deliberately used as a **baseline**, not as a claim that 50/50 is mathematically optimal.

This makes the first hybrid experiment:

* transparent,
* reproducible,
* easy to interpret,
* and independent of a learned weighting model.

---

# Weight Sensitivity Analysis

The hybrid layer is designed to support multiple weighting configurations.

Current test set:

```text
30 / 70
40 / 60
50 / 50
60 / 40
70 / 30
```

where the first value represents the V8.1 contribution.

For complexity:

$$C_H(w)=wC_{8.1}+(1-w)C_9$$

For BDI:

$$BDI_H(w)=wBDI_{8.1}+(1-w)BDI_9$$

The purpose is to measure **ranking stability**.

If the ranking remains largely unchanged across the weight range:

```text
30/70 ──┐
40/60 ──┤
50/50 ──┼──► Stable ranking
60/40 ──┤
70/30 ──┘
```

then the hybrid representation is less sensitive to the exact weighting.

If rankings change substantially:

```text
30/70 ──► Ranking A
40/60 ──► Ranking A
50/50 ──► Ranking B
60/40 ──► Ranking C
70/30 ──► Ranking C
```

then a future intelligent weighting mechanism may be justified.

This is preferable to selecting a sophisticated weighting model before understanding the behavior of the underlying features.

---

# Benchmark Dataset

The current benchmark consists of eight video channels:

```text
TV1
TV2
TV3
TV4
TV5
TV7
TV8
TV17
```

The benchmark is intended to evaluate:

* agreement between V8.1 and V9,
* complementary information,
* ranking behavior,
* complexity differences,
* BDI differences,
* distributional regimes,
* and hybrid stability.

## Current Results

| Channel | V8.1 Complexity | V8.1 BDI | V9 Complexity | V9 BDI | V9 Regime       |
| ------- | --------------: | -------: | ------------: | -----: | --------------- |
| TV1     |          0.3811 |   0.3799 |        0.4362 | 0.3873 | HEAVY-TAIL-RISK |
| TV2     |          0.4677 |   0.4861 |        0.4223 | 0.4244 | HEAVY-TAIL-RISK |
| TV3     |          0.4163 |   0.4468 |        0.4562 | 0.3685 | HEAVY-TAIL-RISK |
| TV4     |          0.3496 |   0.3487 |        0.2773 | 0.3062 | SPARSE-EXTREME  |
| TV5     |          0.3796 |   0.4103 |        0.3734 | 0.3619 | SPARSE-EXTREME  |
| TV7     |          0.4073 |   0.4219 |        0.4207 | 0.3538 | HEAVY-TAIL-RISK |
| TV8     |               — |        — |        0.4191 | 0.3516 | SPARSE-EXTREME  |
| TV17    |          0.3081 |   0.3428 |        0.4516 | 0.3461 | HEAVY-TAIL-RISK |

> **Note:** TV8's V8.1 values are intentionally left unreported here until the benchmark record is finalized. The repository should not silently substitute incomplete values.

---

# Hybrid Ranking Behavior

For the currently complete six-channel subset, the 50/50 hybrid complexity ranking is:

```text
1. TV2
2. TV3
3. TV1
4. TV17
5. TV5
6. TV4
```

The corresponding 50/50 hybrid complexity values are approximately:

| Channel | Hybrid Complexity |
| ------- | ----------------: |
| TV2     |            0.4450 |
| TV3     |            0.4362 |
| TV1     |            0.4086 |
| TV17    |            0.3798 |
| TV5     |            0.3765 |
| TV4     |            0.3134 |

The important result is not merely the numerical value.

The important research question is whether **relative ordering remains stable when the V8.1/V9 contribution changes**.

---

# A Key Distributional Case: TV17

TV17 provides an especially useful example of the difference between central statistics and tail behavior.

V8.1:

```text
Complexity = 0.3081
BDI        = 0.3428
```

V9:

```text
Complexity = 0.4516
BDI        = 0.3461
Risk       = 1.0
Regime     = HEAVY-TAIL-RISK
```

V9 additionally reports:

```text
P50    = 0
P75    = 0
P90    = 0
P99    = 1
P99.5  = 1
P99.9  = 7
Max    = 196
```

with substantial tail indicators.

This demonstrates an important design motivation for V9:

> A video may appear simple under central statistics while still containing rare, severe complexity events.

Such behavior can be operationally important for adaptive resource allocation.

---

# Operational Wrapper

ASTCIE is intended to operate as more than an offline analyzer.

The operational wrapper connects the analysis engines to the encoding pipeline.

Conceptually:

```text
astcie_wrapper.py
        │
        ├── complexity7.py
        │       └── V8.1
        │
        ├── complexity8.py
        │       └── V9
        │
        ├── Hybrid Intelligence
        │
        ├── Allocation Engine
        │
        ├── FFmpeg
        │
        ├── FFprobe
        │
        └── Post-Encode Validation
```

The wrapper should accept a video as an input argument:

```bash
python astcie_wrapper.py input.mp4
```

and produce a structured operational contract.

Example:

```json
{
  "input": {
    "file": "input.mp4"
  },
  "analysis": {
    "v8_1": {
      "complexity": 0.3811,
      "bdi": 0.3799
    },
    "v9": {
      "complexity": 0.4362,
      "bdi": 0.3873
    }
  },
  "hybrid": {
    "complexity": 0.40865,
    "bdi": 0.38360
  }
}
```

The exact production schema may evolve as the allocation and multiplexing layers mature.

---

# Adaptive Bitrate Allocation

The next operational layer is the **Allocation Engine**.

Its job is not simply:

```text
higher complexity → higher bitrate
```

Instead, allocation considers multiple constraints.

Conceptually:

$$Allocation =f(Complexity,BDI,Priority,Risk,Resolution,Codec,Budget)$$

The allocation engine should account for:

* total transport budget,
* per-program priority,
* minimum bitrate,
* target bitrate,
* maximum bitrate,
* content complexity,
* behavioral demand,
* distributional risk,
* resolution,
* codec,
* and degradation cost.

---

# 30 Mb/s Transport Budget

The current broadcast experiment targets:

$$R_{transport}=30\text{ Mb/s}$$

This is the **total MPEG-TS transport budget**.

It must not be confused with:

```text
30 Mb/s per channel
```

or:

```text
30 Mb/s per video stream
```

The total budget includes:

* video elementary streams,
* audio streams,
* MPEG-TS overhead,
* PSI/SI tables,
* PCR,
* and muxing overhead.

Therefore the allocator must reserve a configurable amount of budget for non-video payload.

Conceptually:

$$R_{payload}=R_{transport}-R_{overhead}-R_{audio}$$

Then:

$$\sum_i R_{video,i}\leqR_{payload}$$

The final transport rate should be verified independently with FFprobe.

---

# Priority-Aware Allocation

A simple proportional allocation is not sufficient for a realistic multi-channel system.

For example:

```text
Channel A
High priority
High complexity
High demand

Channel B
Medium priority
Medium complexity

Channel C
Low priority
Low complexity
```

If the budget becomes constrained, the system should not necessarily reduce all three channels equally.

Instead:

```text
                30 Mb/s
                    │
        ┌───────────┴───────────┐
        │                       │
    High Priority          Lower Priority
        │                       │
    Preserve quality       Accept degradation
```

A conceptual policy is:

### Step 1 — Minimum Allocation

Assign every program a minimum feasible configuration.

### Step 2 — Priority Preservation

Protect high-priority channels.

### Step 3 — Complexity Demand

Distribute remaining capacity according to content demand.

### Step 4 — Risk Awareness

Increase protection for streams with significant extreme-event behavior.

### Step 5 — Controlled Degradation

Reduce lower-priority streams when necessary.

### Step 6 — Resolution Adaptation

If a resolution cannot be supported at an acceptable bitrate, consider reducing resolution rather than excessively starving the encoder.

---

# Resolution-Aware Allocation

Bitrate allocation and resolution selection should not be treated as completely independent decisions.

Consider:

```text
1080p @ 4 Mb/s
```

versus:

```text
720p @ 4 Mb/s
```

For some content, allocating too little bitrate to 1080p may produce poor visual quality.

Therefore the allocator may eventually make decisions such as:

```text
1080p @ 11 Mb/s
        │
        │ budget insufficient
        ▼
720p  @ 6 Mb/s
```

instead of:

```text
1080p @ 4 Mb/s
```

The exact thresholds must be calibrated experimentally.

ASTCIE therefore treats resolution selection as part of the future quality-aware allocation problem.

This direction is consistent with research showing that content-adaptive bitrate ladder construction benefits from considering video complexity and spatial/temporal resolution rather than applying a universal ladder to every sequence.

---

# Important Scientific Limitation

The current ASTCIE complexity and BDI values **do not by themselves prove that a video requires a specific bitrate**.

For example:

```text
Complexity = 0.72
```

does not automatically mean:

```text
Required bitrate = 11 Mb/s
```

A scientifically defensible bitrate predictor requires empirical quality-rate data.

The future calibration dataset should therefore contain:

```text
Video
    │
    ├── ASTCIE V8.1 features
    ├── ASTCIE V9 features
    ├── Hybrid complexity
    ├── Hybrid BDI
    │
    ├── Resolution
    ├── Codec
    ├── Encoder settings
    ├── Target bitrate
    ├── Actual bitrate
    │
    ├── PSNR
    ├── SSIM
    ├── VMAF
    └── Encoding time
```

This enables the project to learn:

$$Bitrate^* =f(Content\ Intelligence,\ Resolution,\ Codec,\ Quality\ Target)$$

rather than assuming such a mapping.

Until that dataset exists, BDI should be treated as an **experimental content-demand proxy**, not a validated bitrate predictor.

---

# Parallel Encoding

For multi-channel operation, individual programs can be encoded concurrently.

Conceptually:

```text
                 Allocation Engine
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
    FFmpeg-1        FFmpeg-2         FFmpeg-3
      TV1             TV2              TV3
       │               │                │
       └───────────────┼────────────────┘
                       ▼
                 Encoded Programs
```

A Python orchestration layer can manage multiple FFmpeg subprocesses using process-level concurrency.

This allows the computational workload to be distributed across available CPU resources.

The degree of parallelism should remain configurable because running many encoders simultaneously can increase:

* CPU utilization,
* memory usage,
* thermal load,
* disk I/O,
* and total encoding time.

---

# Multi-Program MPEG-TS

The final output is intended to resemble a broadcast-style transport stream containing multiple programs.

The architecture is:

```text
Program 1
 ├── Video PID
 └── Audio PID

Program 2
 ├── Video PID
 └── Audio PID

Program 3
 ├── Video PID
 └── Audio PID

...

Program N
 ├── Video PID
 └── Audio PID

             ↓

       MPEG-TS Multiplexer

             ↓

      30 Mb/s Transport Stream
```

The system should expose distinct:

* program numbers,
* service/program mappings,
* video PIDs,
* audio PIDs,
* and associated metadata.

This is intentionally different from concatenating files sequentially.

A concatenated file represents one continuous program.

A multi-program MPEG-TS represents multiple independently addressable programs within one transport stream.

---

# Broadcast-Oriented Multiplexing

The target architecture is:

```text
TV1 ──┐
TV2 ──┤
TV3 ──┤
TV4 ──┤
TV5 ──┤
TV7 ──┤──► MPEG-TS Multiplexer ──► 30 Mb/s TS
TV8 ──┤
TV17 ─┤
       │
       └── Additional programs
```

The final stream should contain appropriate MPEG-TS signaling, including:

* PAT
* PMT
* program numbers
* stream mappings
* PCR handling
* continuity counters
* video PIDs
* audio PIDs

where supported by the selected FFmpeg/muxer configuration.

The exact signaling configuration remains implementation-dependent and should be validated using FFprobe and, where required, dedicated transport-stream analysis tools.

---

# FFprobe Validation

Every generated output should be validated.

Example:

```bash
ffprobe -hide_banner output.ts
```

Machine-readable inspection should preferably use:

```bash
ffprobe \
  -v error \
  -show_programs \
  -show_streams \
  -of json \
  output.ts
```

The validation layer should collect information such as:

```text
Program Number
Service Name
Video PID
Audio PID
Codec
Resolution
Frame Rate
Bitrate
Duration
Stream Type
```

The output can be exported to CSV for further analysis.

Example:

```text
channel,program,pid,codec,width,height,fps,bitrate
TV1,101,201,h264,1920,1080,25,8.1M
TV2,102,202,h264,1920,1080,25,5.7M
...
```

---

# Post-Encode Validation

ASTCIE does not stop at encoding.

The encoded output should be analyzed again.

```text
Original Video
      │
      ▼
 V8.1 + V9
      │
      ▼
Allocation
      │
      ▼
 FFmpeg
      │
      ▼
Encoded Video
      │
      ▼
 V8.1 + V9
      │
      ▼
Comparison
```

This allows the system to investigate whether encoding changed the measured characteristics.

The comparison layer can evaluate:

* complexity before/after encoding,
* BDI before/after encoding,
* SI/TI behavior,
* distributional changes,
* bitrate,
* resolution,
* encoding parameters,
* and potentially objective quality metrics.

The goal is to establish a closed experimental loop:

$$Analyze\rightarrowAllocate\rightarrowEncode\rightarrowMeasure\rightarrowCompare$$

---

# Scientific Positioning

ASTCIE is positioned as a **video content intelligence and resource-allocation research framework**.

It is not intended to replace established video coding standards.

Instead, it operates above the codec layer.

```text
                ASTCIE
                  │
       Content Intelligence
                  │
       Resource Allocation
                  │
       Encoding Configuration
                  │
        ┌─────────┴─────────┐
        │                   │
      H.264               HEVC
        │                   │
        └─────────┬─────────┘
                  ▼
             MPEG-TS
```

The scientific motivation is supported by the broader literature around:

* spatial information,
* temporal information,
* scene complexity,
* content-adaptive bitrate ladders,
* per-title encoding,
* rate control,
* and content-aware resource allocation.

ITU-T P.910 explicitly includes SI/TI as scene-characterization metrics and discusses their use in relation to compression characteristics.

Research on content-adaptive bitrate ladders similarly demonstrates the value of exploiting content characteristics instead of relying exclusively on universal bitrate ladders.

ASTCIE extends this direction by investigating an additional **distributional and tail-aware intelligence layer**.

---

# Offline vs Operational Intelligence

The current project intentionally separates offline research from future real-time deployment.

## Offline Mode

High-detail analysis:

```text
V8.1
+
V9
+
distributional analysis
+
tail analysis
+
benchmarking
```

This mode prioritizes analytical richness over latency.

## Operational Mode

The future operational path can use:

```text
Historical intelligence
        +
Fast analysis
        +
Channel profile
        +
Current content
        ↓
Allocation decision
```

This separation is important because a research-grade analyzer does not necessarily need to operate at real-time speed.

A future fast-path analyzer may be introduced for:

* live news,
* sports,
* breaking events,
* live television,
* and other latency-sensitive sources.

The fast path should not invalidate the offline V8.1/V9 reference architecture.

---

# Channel Intelligence Profiles

A future operational system can maintain historical profiles for individual channels.

Example:

```json
{
  "channel": "TV7",
  "profile": {
    "historical_complexity": 0.41,
    "historical_bdi": 0.39,
    "typical_regime": "HEAVY-TAIL-RISK",
    "priority": 1
  }
}
```

Potential profile dimensions include:

* historical complexity,
* BDI,
* tail risk,
* typical resolution,
* frame rate,
* codec,
* average bitrate,
* quality targets,
* channel priority,
* and historical allocation behavior.

This creates the foundation for a future:

```text
Content Intelligence Database
```

rather than relying exclusively on one-time analysis.

---

# Quality-Aware Resource Stealing

A central operational objective is to avoid uniform degradation.

Suppose:

```text
Total budget = 30 Mb/s
```

and:

```text
TV1 = high priority
TV2 = high complexity
TV3 = medium priority
TV4 = low complexity
TV5 = low priority
```

The system may prefer:

```text
TV1   preserve
TV2   preserve
TV3   moderate adjustment
TV4   moderate adjustment
TV5   larger adjustment
```

rather than:

```text
every channel → same percentage reduction
```

This introduces the concept of **degradation cost**.

A future formulation can be expressed as:

$$\min_{\mathbf{R},\mathbf{Q}}\sum_i D_i(R_i,Q_i)$$

subject to:

$$\sum_i R_i \leq R_{transport}$$

where:

* $$\(R_i\)$$ is allocated bitrate,
* $$\(Q_i\)$$ represents resolution/quality configuration,
* $$\(D_i\)$$ represents the estimated degradation cost.

The intelligence layer can provide features to estimate the relative cost of degrading each stream.

---

# Limitations

ASTCIE is currently a research and engineering prototype.

Important limitations include:

## 1. BDI is not yet a validated bitrate predictor

BDI currently acts as an experimental content-demand proxy.

A bitrate-quality calibration dataset is required before using it as a quantitative bitrate predictor.

## 2. Benchmark size

The current benchmark contains eight channels.

This is useful for architecture validation but insufficient for broad statistical generalization.

## 3. No universal bitrate equation

There is currently no claim that:

$$Complexity \rightarrow Exact\ Bitrate$$

The project must empirically learn such a mapping.

## 4. Encoding dependency

Required bitrate depends on more than content complexity.

It also depends on:

* codec,
* encoder implementation,
* preset,
* GOP structure,
* quantization,
* resolution,
* frame rate,
* content characteristics,
* and quality target.

## 5. Offline computational cost

High-detail V8.1/V9 analysis can be computationally expensive on long videos.

This is acceptable for the current offline research stage but motivates a future fast operational path.

## 6. Hybrid weights

The current 50/50 weighting is a baseline.

It has not been established as globally optimal.

## 7. Transport validation

A nominal MPEG-TS muxrate must always be independently verified.

---

# Roadmap

## Phase 1 — Offline Intelligence

* [x] V8.1 complexity engine
* [x] V9 distributional engine
* [x] Spatial intelligence
* [x] Temporal intelligence
* [x] Motion intelligence
* [x] Texture analysis
* [x] Event analysis
* [x] Tail analysis
* [x] Extreme analysis
* [x] Benchmark framework

## Phase 2 — Hybrid Intelligence

* [x] V8.1/V9 comparison
* [x] 50/50 hybrid baseline
* [x] 30/70 sensitivity test
* [x] 40/60 sensitivity test
* [x] 60/40 sensitivity test
* [x] 70/30 sensitivity test
* [ ] Complete TV8 benchmark record
* [ ] Full 8-channel ranking stability report
* [ ] Statistical agreement analysis

## Phase 3 — Operational Wrapper

* [x] V8.1 invocation
* [x] V9 invocation
* [x] JSON collection
* [x] Hybrid contract
* [x] Production allocation engine
* [x] Priority-aware allocation
* [x] Resolution-aware allocation
* [x] Historical channel profiles

## Phase 4 — Broadcast Pipeline

* [x] Parallel FFmpeg encoding
* [x] Per-program configuration
* [x] MPEG-TS multiplexing
* [x] 30 Mb/s transport budget
* [x] PAT/PMT validation
* [x] PID validation
* [x] PCR validation
* [x] Multi-program VLC verification
* [x] FFprobe CSV reporting

## Phase 5 — Closed-Loop Validation

* [ ] Re-analyze encoded content
* [ ] Compare V8.1 before/after
* [ ] Compare V9 before/after
* [ ] Compare hybrid complexity
* [ ] Measure objective quality
* [ ] Build bitrate-quality dataset

## Phase 6 — Intelligent Allocation

* [ ] Complexity-to-bitrate calibration
* [ ] Quality-aware allocation
* [ ] Learned allocation model
* [ ] Resolution switching policy
* [ ] Channel-specific priors
* [ ] Fast live-content path

---

# Repository Structure

A representative project structure is:

```text
ASTCIE-Adaptive-Spatio-Temporal-Complexity-Intelligence-Engine/
│
├── complexity7.py
│   └── ASTCIE V8.1
│
├── complexity8.py
│   └── ASTCIE V9
│
├── astcie_wrapper.py
│   └── Operational orchestration
│
├── allocation/
│   ├── allocator.py
│   ├── bitrate.py
│   ├── priority.py
│   └── resolution.py
│
├── encoding/
│   ├── ffmpeg_encoder.py
│   ├── parallel.py
│   └── profiles.py
│
├── transport/
│   ├── mpegts.py
│   ├── programs.py
│   └── validation.py
│
├── analysis/
│   ├── benchmark.py
│   ├── comparison.py
│   └── reports.py
│
├── results/
│   ├── original/
│   ├── encoded/
│   └── benchmark/
│
├── references/
│
├── examples/
│
├── tests/
│
├── requirements.txt
│
├── LICENSE
│
└── README.md
```

The exact directory layout may evolve as the operational pipeline is finalized.

---

# Installation

Clone the repository:

```bash
git clone https://github.com/M-Amin-Wolverine/ASTCIE-Adaptive-Spatio-Temporal-Complexity-Intelligence-Engine.git
cd ASTCIE-Adaptive-Spatio-Temporal-Complexity-Intelligence-Engine
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\activate
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Verify FFmpeg:

```bash
ffmpeg -version
```

Verify FFprobe:

```bash
ffprobe -version
```

---

# Usage

## Run V8.1

```bash
python complexity7.py input.mp4
```

## Run V9

```bash
python complexity8.py input.mp4
```

## Run the operational wrapper

```bash
python astcie_wrapper.py input.mp4
```

A production wrapper should generate structured JSON describing:

```text
Input
  ↓
V8.1
  ↓
V9
  ↓
Hybrid Intelligence
  ↓
Allocation
  ↓
Encoding
  ↓
Validation
```

---

# Example Workflow

A complete experiment can follow this sequence:

```bash
# 1. Analyze source
python complexity7.py TV1.mp4
python complexity8.py TV1.mp4

# 2. Run wrapper
python astcie_wrapper.py TV1.mp4

# 3. Encode according to allocation
ffmpeg ...

# 4. Validate encoded output
ffprobe ...

# 5. Re-analyze encoded output
python complexity7.py TV1_encoded.mp4
python complexity8.py TV1_encoded.mp4

# 6. Compare
python analysis/comparison.py ...
```

For a multi-program experiment:

```text
TV1 ──► Analyze ──► Allocate ──► Encode ──┐
TV2 ──► Analyze ──► Allocate ──► Encode ──┤
TV3 ──► Analyze ──► Allocate ──► Encode ──┤
TV4 ──► Analyze ──► Allocate ──► Encode ──┤
TV5 ──► Analyze ──► Allocate ──► Encode ──┤
TV7 ──► Analyze ──► Allocate ──► Encode ──┤
TV8 ──► Analyze ──► Allocate ──► Encode ──┤
TV17──► Analyze ──► Allocate ──► Encode ──┘
                                           │
                                           ▼
                                  MPEG-TS Multiplexer
                                           │
                                           ▼
                                      30 Mb/s TS
                                           │
                             ┌─────────────┴─────────────┐
                             ▼                           ▼
                          FFprobe                       VLC
```

---

# Research Reproducibility

ASTCIE is designed around reproducible experiments.

Each experiment should record:

```text
Input video
Source resolution
Source frame rate
Analysis resolution
Analysis frame rate
Analyzer version
Configuration
Random seed
Complexity results
BDI results
Hybrid weights
Allocation decision
Encoder
Encoder preset
Resolution
Target bitrate
Actual bitrate
Transport bitrate
Quality metrics
Runtime
```

This enables the same experiment to be reconstructed later.

---

# Design Principles

ASTCIE follows several principles.

### 1. Measure before allocating

```text
Video
  ↓
Intelligence
  ↓
Decision
```

not:

```text
Fixed bitrate
  ↓
Everything
```

### 2. Separate measurement from policy

V8.1 and V9 measure.

The allocation engine decides.

### 3. Preserve information

V9 should complement V8.1 rather than erase it.

### 4. Treat extreme behavior explicitly

Rare events can matter even when median behavior appears simple.

### 5. Validate operational decisions

The system should measure what happened after encoding.

### 6. Avoid unsupported scientific claims

A complexity score is not automatically a bitrate requirement.

### 7. Keep the baseline transparent

The 50/50 hybrid is intentionally simple before introducing learned weighting.

---

# Future Research Questions

ASTCIE creates several research directions.

## Q1 — Does hybrid intelligence outperform individual analyzers?

$$HIL > V8.1?$$

$$HIL > V9?$$

The answer should be established empirically.

## Q2 — Is the hybrid ranking stable?

How much does the ranking change under:

```text
30/70
40/60
50/50
60/40
70/30
```

?

## Q3 — Does tail intelligence improve bitrate allocation?

Does explicitly modeling extreme complexity produce better allocation under constrained transport budgets?

## Q4 — Can ASTCIE predict bitrate demand?

This requires a calibrated dataset containing:

```text
ASTCIE features
+
encoding parameters
+
bitrate
+
objective quality
```

## Q5 — Can resolution and bitrate be jointly optimized?

Instead of:

```text
bitrate only
```

the decision becomes:

```text
resolution + bitrate + quality
```

## Q6 — Can the offline intelligence be compressed into a real-time model?

A future operational engine could use:

```text
Offline ASTCIE
       ↓
Historical Dataset
       ↓
Lightweight Predictor
       ↓
Live Allocation
```

---

# References

## ITU-T P.910

ITU-T Recommendation P.910, *Subjective video quality assessment methods for multimedia applications*.

The recommendation includes spatial information (SI) and temporal information (TI) as scene-characterization metrics and discusses their role in video sequence characterization and compression-related assessment.

[ITU-T Recommendation P.910](https://www.itu.int/dms_pubrec/itu-t/rec/p/T-REC-P.910-202310-I%21%21TOC-HTM-E.htm)

## Content-Adaptive Bitrate Ladder Estimation

J. Šuljug and S. Rimac-Drlje, *Content-Adaptive Bitrate Ladder Estimation in High-Efficiency Video Coding Utilizing Spatiotemporal Resolutions*, Electronics, 2024.

The work investigates content-adaptive bitrate ladder construction using spatial and temporal characteristics of video sequences.

## Efficient Bitrate Ladder Construction

A. V. Katsenou, J. Sole, and D. R. Bull, *Efficient Bitrate Ladder Construction for Content-Optimized Adaptive Video Streaming*, 2021.

The work investigates content-tailored bitrate ladder construction using spatio-temporal video features and demonstrates the potential for reducing exhaustive encoding requirements.

---

# Project Status

**Current status: Research + Engineering Prototype**

The offline intelligence layer is substantially developed around:

```text
ASTCIE V8.1
        +
ASTCIE V9
        ↓
Hybrid Intelligence
```

The project is now moving toward:

```text
Hybrid Intelligence
        ↓
Adaptive Allocation
        ↓
Parallel Encoding
        ↓
Multi-Program MPEG-TS
        ↓
30 Mb/s Transport
        ↓
Validation
```

The allocation and broadcast layers should be considered **active development**, not a finished production broadcast system.

---

# Author

**M. Amin Wolverine**

Research & Development Engineer focused on:

* Video Intelligence
* Video Complexity Analysis
* Adaptive Bitrate Allocation
* Multimedia Systems
* Broadcast Systems
* FFmpeg Engineering
* MPEG-TS
* Content-Aware Resource Allocation
* Spatio-Temporal Video Analysis

---

# License

License information will be finalized with the repository release.

---
