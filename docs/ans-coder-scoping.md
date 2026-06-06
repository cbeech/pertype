# Scoping: an ANS entropy coder

**Question:** would replacing the entropy backend with ANS (Asymmetric Numeral Systems)
— as used by JPEG-XL and zstd — close the remaining ~4% gap to JPEG-XL on images and ~6%
to `zstd --train` on text?

**Short answer: no. Don't build it for compression ratio.** ANS is a *speed* optimization,
not a *ratio* one, and the evidence below shows our entropy coding is already at the
model's entropy. The remaining gaps are in the *model* (predictor + context selection),
not the coder backend — and closing them is a large, domain-specific, uncertain-ROI effort.

## 1. The key correction: ANS ≈ arithmetic in ratio

ANS and arithmetic coding both encode a symbol stream to within a fraction of a bit of its
**model entropy** `-Σ log2 p(sym)`. They are mathematically equivalent in compression; ANS
wins on *throughput* (table-driven, no per-symbol division) and loses a little flexibility
(adaptive probabilities are awkward; tANS uses static/blockwise tables). We already use a
range/arithmetic coder (`compressor/arithmetic.py`) behind `ctxcoder` and CALIC. So swapping
in ANS would change speed, not size — **expected ratio gain ≈ 0%.**

JPEG-XL and zstd use ANS *because they need speed at scale*, and they get their compression
edge from their **models**, which is a separate thing that happens to sit in front of ANS.

## 2. Evidence that our entropy coder is already tapped

Measured this session (all on real data, real coded bytes or well-conditioned entropy):

- **Arithmetic coding is ~optimal given the model** (textbook; overhead < 0.1%). Nothing to
  recover in the backend.
- **Top-mantissa-bit modelling** (shipped in both `ctxcoder` and CALIC) captured the one real
  bit of headroom: +0.4–4% on numeric/columnar, +0.8–1.9% on images. A **2nd** mantissa bit
  added only ~0.3% — diminishing returns confirm the per-symbol model is near its floor.
- The apparent "order-2 → 13% headroom" was **overfitting** (sparse high-order contexts on
  a small sample); the real achievable was ~+2%, which we took.
- On a Kodak luma residual, **our CALIC coding (~5.27 b/px) already beats a naive
  full-distribution adaptive model (~5.55 b/px)** — its bias correction + energy-conditioned
  contexts + mantissa modelling are *more* efficient than a flat richer-context coder. The
  coder is not the bottleneck.

## 3. Where the remaining gaps actually are

- **Images (−4% vs JPEG-XL):** JXL's edge is its **predictor** (a self-correcting weighted
  blend of sub-predictors, stronger than our GAP/CALIC) and its **adaptive context tree**
  (MA-tree selecting from many neighbour-derived contexts). The residual it feeds its ANS
  coder is *smaller and better-conditioned* than ours — the win is upstream of the coder.
- **Text (−6% vs `zstd --train`):** zstd's edge is its **repeat-offset-aware optimal LZ
  parser + FSE/ANS sequence coder**, a mature integrated parser. Proven (earlier this
  project) to be a diffuse ~2 KB sum with no single closable lever, short of reimplementing
  zstd's sequence coder.

In both cases the lever is the **model/parser**, not an ANS backend.

## 4. What it would actually take (and the honest ROI)

| Option | Effort | Risk | Ratio gain | Verdict |
|---|---|---|---|---|
| ANS backend swap | medium | medium (bitstream + native rewrite) | **~0%** | ✗ pointless for ratio |
| Stronger image predictor (JXL-style weighted self-correcting) + richer context tree | **large** | high (new codec path, native port, re-verify all image tests, byte-exact) | maybe **+2–4%**, images only | ✗ low ROI, uncertain |
| Reimplement zstd's optimal LZ + FSE for text | **very large** | high | maybe +4–6%, text only | ✗ duplicating zstd |

Every path is large, single-domain, high-regression-risk (it touches the verified core +
the byte-identical C native), and at most recovers a few percent on *one* domain.

## 5. Recommendation

**Do not build the ANS coder, and do not start the predictor/parser rewrite now.** The
compression frontier for this architecture is a defended plateau:

- the per-symbol entropy lever is fully captured (mantissa modelling in both coders);
- every self-describing format routes through the verified `auto` front door;
- the remaining gaps are mature-competitor models (JXL predictor, zstd parser) that cost a
  large rewrite for a few percent on a single domain.

If a future goal specifically wants the image crown, the right target is the **predictor +
context tree** (not ANS), scoped as its own measure-first project: prototype a weighted
self-correcting predictor in Python, measure the residual-entropy drop on Kodak vs CALIC,
and only commit to the native port if the prototype clears, say, +3%. Until then this is
the wrong place to spend effort.
