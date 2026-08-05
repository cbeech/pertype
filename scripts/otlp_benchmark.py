"""Measure-first: OpenTelemetry OTLP traces — the Mode-B / columnar telemetry opportunity.

WHY THIS TARGET
---------------
OTLP is the last untested Tier-2 entry in the "near-drop-in" family where pertype is 3/3:
IoT/MQTT (+41% vs zstd --train), financial tick (+49% vs zstd -19), CAN-bus (+18% vs gzip).
All three are schema-repetitive record streams, which is exactly what OTLP is: every span carries
the same nested envelope (resource → scope → span → attributes), and a collector ships millions
of them. OTLP/HTTP's real default transport compression is **gzip**, so that is the honest bar,
with zstd (generic and dictionary-trained) as the stronger modern alternative.

THE STRUCTURAL CATCH, STATED UP FRONT
------------------------------------
Every span carries a 16-byte trace ID and an 8-byte span ID, and both are cryptographically random
by spec. That is 24 bytes/span of irreducible entropy that NO compressor can touch. At realistic
span sizes this is a large fraction of the payload and puts a hard ceiling on every method here,
ours included. Any headline ratio that ignores it is misleading, so this script reports the
incompressible-ID floor explicitly alongside the ratios.

DATA PROVENANCE — read this before quoting any number
-----------------------------------------------------
Spans are generated through the **real OpenTelemetry Python SDK** and serialised by the **reference
OTLP encoder** (`opentelemetry.exporter.otlp.proto.common`), so the schema, field types and wire
encoding are authentic, not a hand-rolled imitation. The *workload* they describe (service names,
call graph, attribute distributions, error rates) is modelled on a typical microservice deployment
rather than captured from one — a real production capture would be better and is the obvious
follow-up. This mirrors the methodology already used for the IoT target, where real Intel Lab
sensor readings were reshaped into per-message JSON.

Field distributions are chosen to match reality where it affects compressibility:
  - trace/span IDs      : uniformly random (per spec — the entropy floor above)
  - timestamps          : monotonic nanoseconds, small jittered deltas
  - service/span names  : low cardinality (a real deployment has tens, not thousands)
  - attributes          : repetitive keys, low-cardinality values, realistic HTTP status skew

Usage:  PYTHONPATH=. python3 scripts/otlp_benchmark.py [n_spans] [batch_size]
"""
import gzip
import lzma
import os
import random
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

N_SPANS = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BATCH = int(sys.argv[2]) if len(sys.argv) > 2 else 512   # OTLP BatchSpanProcessor default

SERVICES = ["frontend", "cartservice", "productcatalog", "checkout", "payment",
            "shipping", "currency", "recommendation", "adservice", "email"]
ROUTES = ["/api/cart", "/api/checkout", "/api/products/{id}", "/api/payment",
          "/api/ship", "/api/currency/convert", "/api/recommend", "/api/ads", "/healthz"]
METHODS = ["GET", "GET", "GET", "POST", "POST", "PUT"]          # GET-skewed, as in reality
STATUS = [200] * 90 + [201] * 3 + [404] * 3 + [500] * 2 + [503] * 2   # realistic error skew


def build_spans(n, rng):
    """Real OTLP ReadableSpans via the OpenTelemetry SDK."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, TraceFlags, SpanKind
    from opentelemetry.trace.status import Status, StatusCode

    scope = InstrumentationScope("otlp-bench", "1.0.0")
    spans = []
    t0 = 1_700_000_000_000_000_000
    per_service = {s: Resource.create({"service.name": s, "service.version": "1.4.2",
                                       "deployment.environment": "prod"})
                   for s in SERVICES}
    trace_id = rng.getrandbits(128)
    depth = 0
    for i in range(n):
        # A trace is a burst of related spans; start a new trace every few spans.
        if depth == 0:
            trace_id = rng.getrandbits(128)
            depth = rng.randint(3, 12)
        depth -= 1

        svc = SERVICES[rng.randrange(len(SERVICES))]
        route = ROUTES[rng.randrange(len(ROUTES))]
        code = STATUS[rng.randrange(len(STATUS))]
        start = t0 + i * rng.randint(200_000, 900_000)
        dur = rng.randint(120_000, 40_000_000)

        ctx = SpanContext(trace_id=trace_id, span_id=rng.getrandbits(64),
                          is_remote=False, trace_flags=TraceFlags(0x01))
        spans.append(ReadableSpan(
            name=f"{METHODS[rng.randrange(len(METHODS))]} {route}",
            context=ctx,
            parent=None,
            resource=per_service[svc],
            attributes={
                "http.request.method": METHODS[rng.randrange(len(METHODS))],
                "http.route": route,
                "http.response.status_code": code,
                "url.scheme": "https",
                "server.address": f"{svc}.svc.cluster.local",
                "server.port": 8080,
                "net.peer.ip": f"10.42.{rng.randrange(256)}.{rng.randrange(256)}",
                "rpc.system": "grpc",
            },
            kind=SpanKind.SERVER,
            status=Status(StatusCode.ERROR if code >= 500 else StatusCode.OK),
            start_time=start,
            end_time=start + dur,
            instrumentation_scope=scope,
        ))
    return spans


def otlp_batches(spans, batch):
    """Serialise to authentic OTLP/protobuf export payloads, one per batch."""
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    out = []
    for i in range(0, len(spans), batch):
        out.append(encode_spans(spans[i:i + batch]).SerializeToString())
    return out


def zstd_total(files, dict_path=None, level="-19"):
    with tempfile.TemporaryDirectory() as od:
        cmd = ["zstd", level, "-q", "-f", "--output-dir-flat", od]
        if dict_path:
            cmd += ["-D", dict_path]
        subprocess.run(cmd + files, check=True)
        return sum(os.path.getsize(os.path.join(od, os.path.basename(f) + ".zst")) for f in files)


def main():
    rng = random.Random(20260805)
    print(f"Generating {N_SPANS} spans via the real OpenTelemetry SDK...")
    spans = build_spans(N_SPANS, rng)
    payloads = otlp_batches(spans, BATCH)
    raw = sum(len(p) for p in payloads)

    # The entropy floor: 16 B trace id + 8 B span id per span, random by spec.
    id_bytes = N_SPANS * 24
    print(f"OTLP/protobuf: {len(payloads)} export batches of <={BATCH} spans, "
          f"{raw} raw bytes ({raw/N_SPANS:.0f} B/span)")
    print(f"Incompressible ID floor: {id_bytes} B "
          f"({100.0*id_bytes/raw:.1f}% of raw) -- no method below can compress this away.\n")

    n_train = max(1, len(payloads) // 3)
    train_p, test_p = payloads[:n_train], payloads[n_train:]
    test_raw = sum(len(p) for p in test_p)

    with tempfile.TemporaryDirectory() as td:
        traindir = os.path.join(td, "train"); os.makedirs(traindir)
        for i, p in enumerate(train_p):
            open(os.path.join(traindir, f"{i:05d}.otlp"), "wb").write(p)
        testfiles = []
        for i, p in enumerate(test_p):
            fp = os.path.join(td, f"t{i:05d}.otlp"); open(fp, "wb").write(p); testfiles.append(fp)

        results = [("raw OTLP/protobuf", test_raw, None)]
        results.append(("gzip -9  (OTLP/HTTP default)",
                        sum(len(gzip.compress(p, 9)) for p in test_p), "BAR"))
        results.append(("xz -9", sum(len(lzma.compress(p, preset=9)) for p in test_p), None))
        results.append(("zstd -19", zstd_total(testfiles), None))

        dict_path = os.path.join(td, "otlp.dict")
        try:
            subprocess.run(["zstd", "--train", "-q", "-f", "--maxdict=65536", "-o", dict_path]
                           + [os.path.join(traindir, f) for f in os.listdir(traindir)], check=True)
            results.append(("zstd --train (dict)", zstd_total(testfiles, dict_path), "BAR2"))
        except subprocess.CalledProcessError:
            print("  (zstd --train failed -- too few/large samples; skipping)")

        from pertype.model import train
        from pertype.codec import compress as pt_compress
        t = time.perf_counter()
        model = train(train_p, type_id="otlp")
        tt = time.perf_counter() - t
        pt = sum(len(pt_compress(p, model)) for p in test_p)
        results.append((f"pertype trained (use_lz={model.use_lz})", pt, "OURS"))

    print(f"{'method':<34}{'bytes':>10}{'ratio':>8}{'B/span':>9}")
    print("-" * 63)
    bar = next(b for n, b, tag in results if tag == "BAR")
    spans_in_test = sum(min(BATCH, N_SPANS - i * BATCH) for i in range(n_train, len(payloads)))
    for name, b, tag in results:
        mark = ""
        if tag == "OURS":
            mark = f"  <- {'BEATS' if b < bar else 'loses to'} gzip by {100*(bar-b)/bar:+.1f}%"
        print(f"{name:<34}{b:>10}{test_raw/b:>7.2f}x{b/max(1,spans_in_test):>9.1f}{mark}")

    ours = next(b for n, b, tag in results if tag == "OURS")
    floor = spans_in_test * 24
    print(f"\nEntropy-floor check: random IDs alone are {floor} B; pertype output is {ours} B "
          f"= {ours/floor:.2f}x the floor.")
    print(f"(pertype train took {tt:.0f}s over {n_train} batches)")


if __name__ == "__main__":
    main()
