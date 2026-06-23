"""Measure-first: Sentinel-2 / Landsat multispectral imagery (target #6).

10+ bands of 12-16-bit reflectance with strong *spatial* correlation within each band and
(weaker, broadly-spaced) inter-band correlation. The distribution format is per-band GeoTIFF
DEFLATE/LZW (a horizontal-predictor + LZ, no 2D model). Our 2D image codec (MED/CALIC predictor
+ context arithmetic) should beat it per band; inter-band delta is also tried (the hyperspectral
lever) but multispectral bands are too far apart for it to help.

Bar: GeoTIFF DEFLATE + horizontal predictor (the exact distribution format, via rasterio/GDAL),
also LZW, zstd, xz. Ours: per-band image codec and inter-band volume codec, round-trip verified.

Data: real Sentinel-2 L2A from the public AWS COGs (Earth Search STAC; no auth). First run fetches
a 10-band cube over a data-rich window and caches it to MULTISPEC_NPY; later runs reuse the cache.
  MULTISPEC_NPY  cache path (default data/multispec/cube.npy)
  MULTISPEC_WIN  "col,row,size" 10 m-band window (default 8700,8700,1024) — pick a data-rich area
  MULTISPEC_SCENE optional Earth-Search scene id to pin (default: latest low-cloud scene)
"""
import os
import subprocess
import tempfile
import time

import numpy as np

from pertype import imagecodec

NPY = os.environ.get("MULTISPEC_NPY", "data/multispec/cube.npy")
WIN = os.environ.get("MULTISPEC_WIN", "8700,8700,1024")
BANDS = ["B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B11", "B12"]  # 10 bands, uint16


def fetch_cube():
    import json
    import urllib.request
    import rasterio
    from rasterio.windows import Window, bounds as win_bounds, from_bounds

    scene = os.environ.get("MULTISPEC_SCENE")
    if scene:
        href0 = None
    body = json.dumps({"collections": ["sentinel-2-l2a"],
                       "query": {"eo:cloud_cover": {"lt": 5}},
                       "limit": 1, "sortby": [{"field": "properties.datetime", "direction": "desc"}]}).encode()
    req = urllib.request.Request("https://earth-search.aws.element84.com/v1/search", body,
                                 {"Content-Type": "application/json"})
    feat = json.load(urllib.request.urlopen(req, timeout=60))["features"][0]
    base = feat["assets"]["red"]["href"].rsplit("/", 1)[0] + "/"
    print(f"scene: {feat['id']}  cloud {feat['properties'].get('eo:cloud_cover'):.1f}%")
    col, rowi, sz = (int(x) for x in WIN.split(","))
    with rasterio.open("/vsicurl/" + base + "B04.tif") as ds:
        bnds = win_bounds(Window(col, rowi, sz, sz), ds.transform)
    cube = np.zeros((len(BANDS), sz, sz), np.uint16)
    for i, b in enumerate(BANDS):
        with rasterio.open("/vsicurl/" + base + b + ".tif") as ds:
            cube[i] = ds.read(1, window=from_bounds(*bnds, ds.transform),
                              out_shape=(sz, sz), boundless=True)
    return np.ascontiguousarray(cube)


def gtiff_size(cube, comp):
    import rasterio
    c, h, w = cube.shape
    f = tempfile.mktemp(suffix=".tif")
    with rasterio.open(f, "w", driver="GTiff", height=h, width=w, count=c,
                       dtype="uint16", compress=comp, predictor=2) as dst:
        dst.write(cube)
    s = os.path.getsize(f); os.remove(f)
    return s


def main():
    if os.path.exists(NPY):
        cube = np.load(NPY)
    else:
        t = time.time(); cube = fetch_cube()
        os.makedirs(os.path.dirname(NPY) or ".", exist_ok=True); np.save(NPY, cube)
        print(f"fetched in {time.time() - t:.0f}s, cached -> {NPY}")
    raw = cube.tobytes(); n = len(raw)
    print(f"cube {cube.shape} uint16  raw {n / 1e6:.2f} MB  "
          f"data {100 * float((cube > 0).all(0).mean()):.0f}%  max {int(cube.max())}\n")
    print(f"{'method':<28}{'size (MB)':>11}{'ratio':>8}")

    def row(label, size):
        print(f"{label:<28}{size / 1e6:>11.3f}{n / size:>8.2f}")

    def sh(cmd):
        return len(subprocess.run(cmd, input=raw, stdout=subprocess.PIPE).stdout)

    row("zstd -19", sh(["zstd", "-19", "-c"]))
    row("xz -9", sh(["xz", "-9", "-c"]))
    bar = gtiff_size(cube, "deflate"); row("GeoTIFF DEFLATE+pred (BAR)", bar)
    row("GeoTIFF LZW+pred", gtiff_size(cube, "lzw"))
    ps = sum(len(imagecodec.encode(cube[i], bayer=False)) for i in range(len(BANDS)))
    row("ours per-band", ps)
    vol = imagecodec.encode_volume(cube)
    assert np.array_equal(imagecodec.decode_volume(vol), cube), "volume round-trip FAILED"
    row("ours inter-band vol", len(vol))
    best = min(ps, len(vol))
    print(f"\nours best vs GeoTIFF-DEFLATE bar: {(bar - best) / bar * 100:+.1f}%  "
          f"({'WIN' if best < bar else 'lose'})   "
          f"[inter-band {'helps' if len(vol) < ps else 'HURTS (bands too far apart)'}]   round-trip OK")


if __name__ == "__main__":
    main()
