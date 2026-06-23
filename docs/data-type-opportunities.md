# Data-type opportunities — backlog

New data types that fit `pertype`'s compression model but aren't covered yet. Compiled from a
web-research sweep (Jun 2026). Ranked by fit × opportunity. Not yet validated — each entry needs
a measure-first benchmark against the named bar before building anything.

## Validated so far (measure-first)

| Type | Result | vs bar | Script |
|------|--------|--------|--------|
| **IoT / MQTT telemetry** (Intel Lab sensor, per-message JSON) | **3.55×** (28.9 B/msg) | **beats `zstd --train` 2.09× by +41%**; generic gzip/zstd/xz are ≤1.05× (useless on ~100 B msgs). Margin grows with training data (+34% at 400 msgs → +41% at 1200). | `scripts/iot_benchmark.py` |
| **Electrophysiology — multichannel int16** (2 real recordings: SpikeGLX Neuropixels LF 384-ch @2.5 kHz; BlackRock Utah-array 96-ch @30 kHz wideband) | per-channel **7.37×** (LFP), **3.39×** (wideband) | **ties FLAC** (7.14× / 3.40×) with *zero* ephys-specific code — and the headline **cross-channel lever is DISCONFIRMED on both bands**: best cross-channel transform is **−8.6%** (LFP) / **−0.6%** (wideband). General reason: the temporal predictor removes ~100% of variance, leaving residuals whose adjacent-channel correlation (0.10 LFP / 0.24 wideband) is **below the 0.5 threshold** where spatial differencing reduces variance. Even *optimal* cross-channel prediction of the residual has a ceiling of ~(1−corr²) ≈ **<1%** — far below the +3% bar. **Verdict: don't build cross-channel ephys.** | `scripts/ephys_benchmark.py` |
| **Financial tick / order-book** (real Binance BTCUSDT aggTrades, 1 day; same fixed-layout tick structure as ITCH/DBN — sequential IDs, monotonic ms timestamps, tick-grid prices, bool flags) | columnar **9.8×** (5.08 B/rec) | **beats the `zstd -19` bar (5.01×) by +49%**, and beats `xz -9` (7.43×) and zstd-on-raw-CSV (6.36×). The columnar de-interleave + per-column Δ/Δ² collapses the sequential IDs (Δ²→0) and monotonic timestamps that generic LZ sees only as interleaved noise. Scale-stable (+47.8% @200k → +49.1% @500k). **Note:** validated on crypto aggTrades as the accessible real proxy; equity ITCH/DBN MBO has the same structure (ns timestamps + sequential order IDs delta *even better*) so the win should hold/grow. | `scripts/financial_benchmark.py` |
| **Cryo-EM counting-mode movies** (real EMPIAR-10061 K2 beta-gal frames, gain-corrected float32; ~90% exact-zero, ~900 distinct count-values) | count-aware **23.7×** (0.169 B/px) | **beats the `zstd -19` bar (16.6×) by +30%**, and beats `xz -9` (17.1×). Method: map the few-hundred distinct gain-corrected count-values to symbols (<4 KB dict) → sparse small-int image → `ctxcoder` (context-adaptive arithmetic), near the 0.15 B/px entropy floor. Confirmed on 2 independent micrographs (+29.9% / +29.7%). **Spatial prediction HURTS** (imagecodec MED 21.8× < ctxcoder 23.7×) — sparse data wants pure entropy coding, not prediction (same lesson as ephys). | `scripts/cryoem_benchmark.py` |
| **Depth / disparity / optical-flow fields** (real Middlebury 2006 stereo disparity, 7 scenes × 2 = 14 piecewise-smooth uint8 maps) | **14.8×** | **beats the PNG bar (11.4×) by +23%** and **WebP-lossless (13.3×) by +10%**, also beats `xz -9` (14.1×) and `zstd` (13.6×). Consistent +20–27% over PNG across all 14 maps. The edge-aware 2D MED/CALIC predictor + arithmetic handles smooth interiors + sharp depth edges that PNG's Paeth+DEFLATE and WebP code less efficiently. **Caveat:** tested uint8 disparity (KITTI/TUM uint16 depth sets were all 0.4–2 GB — not fetched); the lever is bit-depth-agnostic and typically *stronger* at 16-bit. Optical flow (2-ch float) untested. | `scripts/depth_benchmark.py` |
| **Sentinel-2 / Landsat multispectral** (real Sentinel-2 L2A, 10-band uint16 cube via public AWS COGs / Earth Search STAC) | per-band **4.63×** | **beats the GeoTIFF DEFLATE+predictor bar (2.39×) by +48%**, and beats `xz -9` (4.02×), `zstd -19` (3.44×), GeoTIFF LZW (2.13×). The 2D MED/CALIC predictor + arithmetic crushes per-band DEFLATE (horizontal-predictor + LZ, no 2D model). **Inter-band delta HURTS** (3.79× < 4.63×) — multispectral bands are broadly spaced (blue→SWIR), weakly correlated, so per-band 2D wins (vs hyperspectral, where adjacent bands are near-identical and inter-band delta wins). Same "don't predict across a weakly-correlated axis" lesson. | `scripts/multispectral_benchmark.py` |
| **Microscopy / EM / micro-CT / 4D-STEM stacks** (real `skimage` cells3d confocal uint16; real EMPIAR-11058 cryo-ET tomogram float32) | ⚠️ **DOWN-RANKED — not a clean win** | Premise was "smooth volumetric → predictor beats Blosc-shuffle." On real data it doesn't hold: **confocal uint16 is photon-noisy** (ratios 1.3–2.0×), inter-slice delta doesn't help, and it's data-dependent — ours **+8.6% vs Blosc on the membrane channel but −8.3% on nuclei** (where plain zstd/xz win). **Float32 cryo-ET tomograms** compress only ~1.2× for Blosc *and* every predictor — low-mantissa bits are noise (~9.6M distinct values), near-incompressible losslessly. The codec's genuine volumetric win is on *clean integer* smooth volumes (medical CT/MR/DICOM — already covered). **Untested:** 4D-STEM diffraction (uint8/16, smooth disks on dark) — the one sub-case that could differ; sourcing was the blocker. | `scripts/microscopy_benchmark.py` |

## The two win-modes (the screen)

- **Mode A — predict-per-type, then entropy-code.** A per-type predictor (spatial 2D/3D,
  temporal, multichannel cascade) leaves small residuals the context-adaptive arithmetic coder
  crushes. Wins on **smooth / structured / multichannel-correlated** numeric data.
- **Mode B — trained dictionary + LZ + arithmetic.** A model trained per file-type, amortized
  over **many small files of a known schema** (beats `zstd --train` on some types).
- **Columnar path.** De-interleave fixed-width record streams into per-field columns →
  per-column delta/Δ². Wins on tabular / record / telemetry streams.

**Meta-insight:** nearly every strong gap is the *same shape* — a smooth or multichannel-
correlated numeric field (or a schema-repetitive record stream) where the field bolted on a
**generic LZ / Blosc / gzip / LZ4** with no predictor. That is exactly the gap this codec closes.
The two biggest untapped veins: **multichannel int16/uint16 scientific time-series & imaging**
(Mode A) and **schema-repetitive small telemetry / record streams** (Mode B / columnar).

## Already covered (for reference — do not re-list)

Text (JSON/HTML/logs/generic); photographic & gray images, Canon raw Bayer, DICOM 16-bit,
SRTM DEM, FITS, AVIRIS hyperspectral; PCM audio (beats FLAC); lossless YUV video (vs FFV1);
weather/climate float32 grids, LiDAR LAS point clouds (columnar), numeric CSV, ECG, seismic,
basic genome/protein sequence.

---

## Tier 1 — strongest misses

### Near-drop-in (reuse existing codecs — fastest to prove)

| # | Type | Mode | Why it fits | Bar to beat | Public test data |
|---|------|------|-------------|-------------|------------------|
| 1 | **Neuropixels / large-scale electrophysiology** (int16, 100s–1000s ch @30 kHz) ❌ **RULED OUT — see "Validated so far"** | A | Premise was "beat FLAC + add cross-channel prediction." Tested on 2 real recordings (LFP + 30 kHz wideband): per-channel only **ties FLAC**, and the cross-channel lever is **−8.6% / −0.6%** (disconfirmed both bands — temporal prediction already removes ~100% of variance; residual cross-correlation <0.5 so spatial decorrelation can't help; optimal-prediction ceiling <1%). **Don't build.** | FLAC / WavPack (field repurposes audio codecs, ignores inter-channel) | Allen Institute for Neural Dynamics (S3); IBL; SpikeInterface examples |
| 2 | **EEG / iEEG / MEG** (int16/24 multichannel) | A | Band-limited autocorrelated time-series + channel correlation; ECG/biosignal predictor transfers directly | MEF3 "RED" (simple diff+range coder); much data still EDF/gzip | TUH EEG Corpus; CHB-MIT; DANDI (NWB); OpenNeuro iEEG |
| 3 | **Multichannel / ambisonic / hydrophone audio** (24-bit, HOA ≥16 ch) | A | Extends the audio win along the axis FLAC barely models — inter-channel redundancy | FLAC / MPEG-4 ALS (weak cross-channel) | EigenScape (HOA); DCASE; NOAA passive-acoustic |

### New — generic incumbent (Mode A)

| # | Type | Why it fits | Bar to beat | Public test data |
|---|------|-------------|-------------|------------------|
| 4 | **Cryo-EM counting-mode movies** ✅ **VALIDATED** (+30% vs zstd-19; see "Validated so far") | Sparse near-binary integer frames → count-aware arithmetic model | TIFF+LZW / EER-RLE / MRCZ-zstd | EMPIAR (e.g. 10025, EER entries) |
| 5 | **Microscopy / EM / micro-CT / 4D-STEM stacks** (uint16, spatiotemporal) ⚠️ **DOWN-RANKED** (tested; not a clean win — see "Validated so far") | Smooth in space *and* time; incumbent has no spatial model — **but real confocal is noisy (data-dependent ±8%) and real EM tomograms are float32 (~1.2×, mantissa noise). Clean-integer CT/MR win already covered by DICOM. 4D-STEM untested.** | Blosc+ZSTD + byte-shuffle | EMPIAR; IDR; PMC9900847 benchmark corpus |
| 6 | **Sentinel-2 / Landsat multispectral** (12–16-bit, 10+ bands) ✅ **VALIDATED** (+48% vs GeoTIFF-DEFLATE; see "Validated so far") | Strong spatial correlation (2D predictor); inter-band delta HURTS (bands too far apart). Per-band 2D win. | GeoTIFF DEFLATE/LZW (distribution); CCSDS-123 (specialist) | Copernicus Data Space (Sentinel-2); USGS (Landsat 8/9) |
| 7 | **Depth / disparity / optical-flow fields** (robotics/AR) ✅ **VALIDATED** (+23% vs PNG, +10% vs WebP-LL; see "Validated so far") | Piecewise-smooth (smooth interiors, sharp edges); 2-ch flow even smoother | PNG / WebP-LL / raw LZ4 in rosbags | KITTI; Middlebury Stereo; Sintel; NYU Depth V2 |
| 8 | **MRI raw k-space (fastMRI)** (complex float, 32+ coils) | Multi-coil redundancy (same anatomy) + low-freq energy concentration | Raw / gzip-in-HDF5 (essentially ungoverned) | fastMRI (NYU); Diff5T |
| 9 | **Mass-spec proteomics** (m/z + intensity arrays) | m/z near-linear (delta→~0); intensities smooth positive floats | MassComp / MS-Numpress / mzMLb (HDF5+zlib) | PRIDE Archive; MassIVE; ProteomeXchange |
| 10 | **FASTQ quality-score stream** (Phred bytes) | Slowly-varying small ints with position + prev-value context; binned to ~8 levels on modern data | SPRING / Genozip / Illumina ORA (real bar — target quality stream, not read reordering) | SRA/ENA; fastq_compression_comparison harness |

### New — Mode B / columnar (schema-repetitive records & telemetry)

| # | Type | Mode | Why it fits | Bar to beat | Public test data |
|---|------|------|-------------|-------------|------------------|
| 11 | **Financial tick / order-book (NASDAQ ITCH, Databento DBN, LOBSTER)** ✅ **VALIDATED** (+49% vs zstd-19; see "Validated so far") | columnar + A | Fixed-layout records: monotonic ns timestamps (Δ-of-Δ→~0), sequential order IDs, tick-grid prices, low-card flags | zstd-generic at rest; FIX/FAST on wire (no entropy stage) | LOBSTER samples; NASDAQ Hist. TotalView-ITCH; Databento `dbn` repo |
| 12 | **Automotive CAN-bus / MDF4 (MF4) logs** | columnar + A | Raw frames columnar (monotonic ts, small ID set); decoded signals are slowly-varying gauges | MDF4 native per-block deflate only | CSS Electronics CANedge samples; python-can test MF4 |
| 13 | **IoT / MQTT telemetry** (small same-schema payloads) ✅ **VALIDATED** | **B** | Purest Mode-B: millions of tiny fixed-schema messages; overhead dominates <300 B | gzip / zstd-generic per message; zstd-`--train` at best | UCI/Kaggle IoT sets; Intel Lab sensor dataset |

---

## Tier 2 — strong, more work

| Type | Mode | Note / bar |
|------|------|-----------|
| **scRNA-seq sparse count matrices** (10x MTX/H5AD) | A+B | Sparse small ints; incumbent gzip/blosc; VCSC/IVCSC are layouts not coders |
| **OpenTelemetry / OTLP traces & metrics** | B + columnar | gzip default; *watch* OTel-Arrow as emerging columnar competitor |
| **NetFlow / IPFIX flow records** | columnar + A | nfdump uses LZO/LZ4/bzip2 — no field awareness; CAIDA/MAWI data |
| **Multiplexed spatial-omics imaging** (CODEX/MERFISH/Xenium) | A | Many co-registered channels (huge inter-channel); generic OME-Zarr Blosc |
| **Thermal / radiometric IR** (16-bit pre-AGC) | A | Smooth + temporal video; bar JPEG-LS (Golomb — beatable with arithmetic) |
| **Flow-cytometry FCS** | B | Many small same-schema files, still ZIP'd; columnar/CSV path applies |
| **Mocap BVH / animation curves** | B + A | Thousands of small schema files (gzip/text) + smooth temporal channels |
| **Smart-meter / AMI load profiles** | A + columnar | Slowly-varying; bar DEGA (exp-Golomb+arithmetic) / LZMA; borders covered time-series |
| **Other-vendor raw (Sony ARW / Nikon NEF / DNG)** | A | Generalizes the Bayer codec for free — coverage, not novelty |

---

## Honestly de-prioritized (don't chase)

- **MD trajectories, EXR HDR** — fields tolerate lossy / float prediction is hard (EXR bar ~2.4:1).
- **Gravitational-wave strain** — float64 detector-noise floor caps the ratio.
- **VCF genotypes, Parquet/ORC internals** — strong specialists already (Genozip/GSC; Parquet
  does dictionary+RLE+delta+byte-stream-split).
- **Blockchain ledger** — payload is hash/signature bytes (near-random ceiling).
- **NMR FID / HEP (ROOT)** — near the noise floor / mature columnar I/O already.
- **Encrypted / already-compressed media** — out of scope by the project's own rule.

---

## Recommended first to prototype (public data ready, generic incumbent, low risk)

1. ~~**Neuropixels ephys**~~ — ❌ tested, cross-channel lever ruled out (ties FLAC, no win).
2. ~~**MQTT / IoT telemetry**~~ — ✅ validated (+41% vs `zstd --train`).
3. ~~**Financial ITCH / DBN**~~ — ✅ validated (+49% vs zstd-19; columnar Δ/Δ²).
4. ~~**Cryo-EM counting movies**~~ — ✅ validated (+30% vs zstd-19; symbol-map + ctxcoder).

**Measure-first scorecard:** IoT ✅ (+41% vs `zstd --train`), Financial ✅ (+49% vs zstd-19),
Cryo-EM ✅ (+30% vs zstd-19), Multispectral ✅ (+48% vs GeoTIFF-DEFLATE),
Depth/disparity ✅ (+23% vs PNG, +10% vs WebP-LL),
Electrophysiology ❌ (cross-channel lever ruled out),
Microscopy/4D-STEM ⚠️ (down-ranked — noisy/float real data, ±8% data-dependent).
Recurring lessons: (1) **sparse / already-decorrelated data wants pure adaptive entropy coding,
not prediction** (cryo-EM imagecodec < ctxcoder; ephys cross-channel < per-channel); (2) **the
win needs clean low-entropy structure** — noisy (confocal) or float-mantissa-noisy (tomogram)
data is near-incompressible losslessly and no predictor helps.

Each is a measure-first task: grab the public data, compress with the existing codec (or a small
predictor tweak), and compare to the named bar before committing to build.
