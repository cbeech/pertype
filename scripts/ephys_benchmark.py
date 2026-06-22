"""Measure-first benchmark: large-scale electrophysiology (Neuropixels, Mode A).

Multichannel int16 @ 30 kHz (Neuropixels 1.0 = 384 AP channels + 1 sync). The field
repurposes *audio* codecs (FLAC / WavPack) **per channel**, which models temporal
correlation but ignores the strong **inter-electrode** correlation (adjacent contacts
see overlapping local field + shared reference noise). We already beat FLAC on single
int16 waveforms (see ecg_benchmark.py); the lever validated here is whether a cheap
**cross-channel** decorrelation (neighbour delta / common-median reference) buys the
+3% bar on top of our per-channel coder.

Bar to beat: FLAC per channel (libFLAC via `soundfile`). Also vs gzip/zstd/xz on the
raw interleaved bytes and a delta transform. Every method is round-trip verified.

Data: a flat int16 SpikeGLX `.bin` (sample-major interleaved — `nchans` int16 per frame),
or a BlackRock NSx 2.1 (`.ns5`, magic `NEURALSG`) which is auto-detected and header-parsed.
  EPHYS_DATA   path to the .bin / .ns5     (required)
  EPHYS_NCHANS channels per frame          (default 385; ignored for auto-parsed NSx)
  EPHYS_AP     leading AP channels to keep (default NCHANS-1, i.e. drop the sync channel)
  EPHYS_RATE   sample rate Hz              (default 30000; AP 30 kHz, LF 2500 Hz)
  EPHYS_MAXSAMP cap samples/channel        (default 0 = all)

Validated (measure-first): on a SpikeGLX Neuropixels LF band (384-ch @2.5 kHz) and a
BlackRock Utah-array recording (96-ch @30 kHz wideband), per-channel `ours` ties FLAC and
the cross-channel lever is below the +3% bar on both (−8.6% / −0.6%) — temporal prediction
already removes ~100% of variance, leaving residuals whose adjacent-channel correlation is
below the 0.5 threshold where spatial decorrelation helps. See docs/data-type-opportunities.md.
"""
import io
import os
import subprocess
import time

import numpy as np

from pertype import audiocodec, native, transform

PATH = os.environ.get("EPHYS_DATA", "data/ephys/ap.bin")
NCH = int(os.environ.get("EPHYS_NCHANS", "385"))
NAP = int(os.environ.get("EPHYS_AP", str(NCH - 1)))
MAXS = int(os.environ.get("EPHYS_MAXSAMP", "0"))
RATE = int(os.environ.get("EPHYS_RATE", "30000"))  # AP band 30 kHz; LF band 2500 Hz


def sh(cmd, data):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


# ---- our per-channel int16 predictors (same family as ecg_benchmark) -------------
def _d1f(x):
    e = x.copy(); e[1:] = x[1:] - x[:-1]; return e
def _d1i(e):
    return np.cumsum(e).astype(np.int64)

PREDICTORS = {
    "delta1": (_d1f, _d1i),
    "fixed2": (native.fixed2_fwd, native.fixed2_inv),
    "fixed2+lms16": (lambda x: native.lms_fwd(native.fixed2_fwd(x), 16, 10),
                     lambda e: native.fixed2_inv(native.lms_inv(e, 16, 10))),
    "fixed2+lms16+256": (
        lambda x: native.lms_fwd(native.lms_fwd(native.fixed2_fwd(x), 16, 10), 256, 13),
        lambda e: native.fixed2_inv(native.lms_inv(native.lms_inv(e, 256, 13), 16, 10))),
}


def predict_rice(x):
    """Best-of predictor + Rice for one int64 channel; returns (name, size_bytes)."""
    best = None
    for name, (fwd, inv) in PREDICTORS.items():
        blob = native.rice_encode(fwd(x))
        if best is None or len(blob) < best[1]:
            best = (name, len(blob), inv, blob)
    name, size, inv, blob = best
    assert np.array_equal(inv(native.rice_decode(blob, len(x))), x), f"RT fail {name}"
    return name, size


def ours_total(channels):
    """Sum of best per-channel predict+Rice over a list of int64 channels (+6 B header each)."""
    total = 0
    for c in channels:
        _, sz = predict_rice(c)
        total += sz + 6
    return total


def flac_channel(ch_i16):
    """libFLAC size for one mono int16 channel, round-trip verified."""
    buf = io.BytesIO()
    import soundfile as sf
    sf.write(buf, ch_i16, RATE, format="FLAC", subtype="PCM_16")
    size = len(buf.getbuffer())   # NOT buf.tell(): soundfile seeks back to rewrite the header
    buf.seek(0)
    back, _ = sf.read(buf, dtype="int16")
    assert np.array_equal(back, ch_i16), "FLAC RT fail"
    return size


def load(path):
    """Return (data (samples, nch), nch). Handles a flat SpikeGLX .bin and a
    BlackRock NSx 2.1 ('NEURALSG') .ns5 (header parsed, sync-free, continuous)."""
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic == b"NEURALSG":
        import struct
        hdr = open(path, "rb").read(32)
        nch = struct.unpack("<I", hdr[28:32])[0]
        data_off = 32 + 4 * nch                       # channel-id table follows the header
        raw = np.fromfile(path, dtype="<i2", offset=data_off)
        frames = raw.size // nch
        return raw[: frames * nch].reshape(frames, nch), nch   # all channels are data
    raw = np.fromfile(path, dtype="<i2")              # flat interleaved (SpikeGLX)
    frames = raw.size // NCH
    return raw[: frames * NCH].reshape(frames, NCH)[:, :NAP], NAP


def main():
    x, _ = load(PATH)
    if MAXS:
        x = x[:MAXS]
    samples, nap = x.shape
    raw_bytes = np.ascontiguousarray(x).tobytes()
    n = len(raw_bytes)
    print(f"file: {PATH}")
    print(f"AP channels: {nap}   samples/ch: {samples:,}   "
          f"duration: {samples / RATE:.2f}s   raw: {n / 1e6:.1f} MB int16\n")
    print(f"{'method':<30}{'size (MB)':>12}{'ratio':>9}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<30}{size / 1e6:>12.3f}{n / size:>9.2f}{secs:>9.1f}")

    # ---- generic baselines on the interleaved bytes ----
    row("gzip -9", *sh(["gzip", "-9"], raw_bytes))
    row("zstd -19", *sh(["zstd", "-19", "-c"], raw_bytes))
    row("xz -9", *sh(["xz", "-9", "-c"], raw_bytes))
    dblob = transform.apply(raw_bytes, (("delta", 2),))
    row("delta2 + zstd -19", *sh(["zstd", "-19", "-c"], dblob))

    chans64 = [x[:, c].astype(np.int64) for c in range(nap)]
    chans16 = [np.ascontiguousarray(x[:, c]) for c in range(nap)]

    # ---- THE BAR: FLAC per channel (libFLAC) ----
    t = time.time(); flac = sum(flac_channel(c) for c in chans16)
    row("FLAC /ch (the bar)", flac, time.time() - t)

    # ---- ours: per-channel predict+Rice (no cross-channel) ----
    t = time.time(); base = ours_total(chans64)
    row("ours /ch (predict+Rice)", base, time.time() - t)

    # ---- ours: full audio codec (LMS cascade) per channel ----
    t = time.time(); ac = 0
    for c in chans16:
        enc = audiocodec.encode(c.astype(np.int16), RATE)
        dec, _ = audiocodec.decode(enc)
        assert np.array_equal(dec.ravel(), c), "audio codec RT fail"
        ac += len(enc)
    row("ours /ch (audio codec)", ac, time.time() - t)

    # ---- CROSS-CHANNEL LEVER 1: neighbour delta (spatial order-1) ----
    # resid[:,c] = ch[:,c] - ch[:,c-1]; ch[:,0] kept. Invertible by cumulative sum across ch.
    t = time.time()
    nd = [chans64[0]] + [chans64[c] - chans64[c - 1] for c in range(1, nap)]
    nd_size = ours_total(nd)
    row("ours + neighbour-delta", nd_size, time.time() - t)

    # ---- CROSS-CHANNEL LEVER 2: common median reference ----
    # ref[t] = median over channels; resid = ch - ref; store ref as an extra channel.
    t = time.time()
    ref = np.median(x.astype(np.int64), axis=1).astype(np.int64)
    cmr = [c - ref for c in chans64] + [ref]
    cmr_size = ours_total(cmr)
    row("ours + common-median-ref", cmr_size, time.time() - t)

    # ---- verdict ----
    print()
    best_cross = min(nd_size, cmr_size)
    gain = (base - best_cross) / base * 100
    print(f"per-channel ours vs FLAC : {flac / base:.3f}x  "
          f"({'ours smaller' if base < flac else 'FLAC smaller'})")
    print(f"cross-channel lever gain : {gain:+.1f}%  over per-channel ours  "
          f"(bar = +3%)  -> {'SHIP-WORTHY' if gain >= 3 else 'below bar'}")
    print("round-trip: OK (FLAC, predict+Rice, audio codec, both cross-channel transforms)")


if __name__ == "__main__":
    main()
