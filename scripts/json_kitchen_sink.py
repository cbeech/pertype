"""Decisive json experiment: our fully-improved coder (varint header + rep-16 +
deep parse search) using zstd's own best COVER dictionary as the blob, at several
sizes. This is the strongest configuration: our best coding + zstd's best dict.
If this still loses to zstd's 49,741, the gap is definitively irreducible."""
import os
import subprocess
import tempfile

from compressor import codec
from compressor.benchmark import load_split
from compressor.model import _artifacts, Model, VERSION

tr, te = load_split("corpus_real", "json")
train = [d for _, d in tr]


def zstd_cover_dict(maxdict, workdir):
    paths = []
    for i, d in enumerate(train):
        p = os.path.join(workdir, f"s{i}.bin")
        open(p, "wb").write(d)
        paths.append(p)
    dp = os.path.join(workdir, f"d{maxdict}.dict")
    subprocess.run(["zstd", "--train", *paths, "-o", dp, f"--maxdict={maxdict}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return open(dp, "rb").read()


def measure(blob, max_chain):
    # build a model (dict patterns + freq tables) on the training data with this blob
    dic, mm, dm, mo = _artifacts(train, blob, 4096, 3, 256, max_chain)
    m = Model(type_id="json", dictionary=dic, blob=blob, main_model=mm,
              dist_model=dm, mode_model=mo, transform=(), use_lz=True, version=VERSION)
    tot = 0
    ok = True
    for _, d in te:
        c = codec.compress(d, m, max_chain=max_chain)
        if codec.decompress(c, m) != d:
            ok = False
        tot += len(c)
    return tot, ok, len(blob)


with tempfile.TemporaryDirectory() as wd:
    for md in (262144, 524288):
        blob = zstd_cover_dict(md, wd)
        for mc in (512, 2048):
            tot, ok, bl = measure(blob, mc)
            print(f"zstd-COVER@{md//1024}K blob ({bl:,} B) + our coder, chain={mc}: "
                  f"{tot:,} B  ok={ok}  (zstd 49,741)", flush=True)
