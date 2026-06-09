# Commercial licensing

This project is **dual-licensed**.

## 1. Open-source license (free)

The default license is the **GNU Affero General Public License v3.0 or later
(AGPL-3.0-or-later)** — see [`LICENSE`](LICENSE). You may use, modify, and redistribute the
software under those terms at no cost.

The key obligation of the AGPL: if you convey the software **or make it available to users
over a network** (e.g. embed it in a hosted/SaaS product), you must offer those users the
complete corresponding source of your version under the AGPL. In practice that means any
**proprietary or closed-source** use — including server-side use in a product you don't want
to open-source — is not permitted under the free license.

## 2. Commercial license (paid)

If the AGPL's copyleft and network-source obligations don't work for your use — for example
you want to:

- embed this in a **closed-source** product or service,
- offer it (or a derivative) as part of a **hosted/SaaS** offering without releasing your
  source, or
- redistribute it under terms other than the AGPL,

then you can obtain a **commercial license** that grants exactly those rights, with no
copyleft or source-disclosure requirement.

Because the project is held under a single copyright (see [`CLA.md`](CLA.md)), the author can
grant such a license directly.

**To enquire:** Craig Beech — <craigbeech@gmail.com>. Tell us roughly how you intend to use it
and we'll sort out terms.

## Scope of a commercial license

A commercial license covers **this project's own code** (the `pertype` package and the
`pertype` crate). Two boundaries to be aware of:

- **Third-party dependencies.** Bundled dependencies are permissive and carry over freely;
  certain *optional* Python extras pull in GPL/LGPL native libraries (ffmpeg/x264, LibRaw,
  libsndfile) whose terms you must satisfy separately for a closed-source product — see
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
- **Patents.** The codec is built on expired or public-domain techniques and a commercial
  license grants rights in our copyright, **not** a patent indemnity. A technical review rates
  overall patent risk low, with the **video motion-compensation** path the one area meriting a
  professional freedom-to-operate search before commercial deployment. Terms can be scoped
  accordingly (e.g. excluding the video codec).

---

*Not sure which applies?* If you're an individual, a researcher, a hobbyist, or another
open-source project that can comply with the AGPL, the free license is all you need. The
commercial license exists for organisations that need to keep their own work closed.
