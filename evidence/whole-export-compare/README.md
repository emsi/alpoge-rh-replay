# Whole-export comparison with con-leche

A separate [recorded run](../emsi-whole-export-compare-20261121.json)
checked the complete published solution export with con-leche at
`5dc36dc15b7bee891a02406b78a8ac816a47baad`, built using Lean 4.36.0.
It accepted 1,468,418 solution declarations and justified all five theorem
statements against a 51,692-declaration Mathlib-only challenge. Exit status was
0, elapsed time 478.377 seconds, peak RSS 45.3 GiB, and average CPU use 46.5 cores
on a 64-CPU host. The checker reported its 64-worker solution-checking pool
99.5% busy; initial parsing took 143.7 seconds. Pool occupancy is distinct from
measured CPU use.

This avoids rebuilding overlapping packet prerequisites and loading a separate
import environment for each target. The complete solution is checked before
the challenge is checked against it; statement names, types and their context
must agree. Positive controls passed, and invalid proofs, an extra axiom, a
wrong statement and changed context were rejected. The
[release](https://github.com/emsi/alpoge-rh-replay/releases/tag/whole-export-compare-20261121)
contains the original log, controls, commands, checker and input hashes,
resource records, and a SHA-256 manifest.

The challenge's exact bytes were also recovered from the successful independent
[Mathlib-only CI run](https://github.com/leanprover-community/riemann-hypothesis/actions/runs/44871806349).
The release preserves the CI artifact, source, pins and workflow. Its decompressed
SHA-256 matches the challenge used in the recorded check. This is provenance
evidence from CI, not a local clean challenge build.

With the pinned checker built and the two exports decompressed, the checking
command is:

```bash
timeout --signal=TERM --kill-after=10s 1200s \
  prlimit --as=206158430208:206158430208 --cpu=60000:60000 -- \
  /path/to/con-leche/.lake/build/bin/con-leche \
    --verified --jobs=64 --progress=5% \
    --compare /path/to/challenge.ndjson /path/to/solution.ndjson
```

The example bounds one checker to 192 GiB of address space, 60,000 CPU seconds
and 20 minutes wall time. It requires enough available memory for that bound;
it is not a sandbox. The recorded run additionally used an offline filesystem
sandbox and a process-tree resource supervisor, preserved in the evidence.
The checker binary in that run had SHA-256
`588c0b86a3cb5e9fbee489ec1d609435599d82b155df1bb4f5e6886d43e3a12e`.

Obtain the [published exports](https://math.mit.edu/~drew/grh/) and verify these
decompressed identities before checking:

| File | Bytes | SHA-256 |
|---|---:|---|
| `solution.ndjson` | 61,570,595,788 | `5ea74b912147c5c93cc2b6d70cc727cf908df2f62ac6a20480a3fa02f6be244e` |
| `challenge.ndjson` | 350,862,311 | `bd484ac9886a96f26b1d9a7a59ebd45b7a70d102c015d4f31f9302bbdd6ab2b9` |

The recorded checker build took 205.9 seconds, solution decompression 106.3
seconds, and the guarded comparison including controls and input rehashing
595.3 seconds: about 15.1 minutes summed. This is not a measured cold end-to-end
time; obtaining the toolchain and exports, provenance checks and publication
are additional work. Check an existing validated export to reproduce the fast
verification result. A clean source build followed by export is a separate,
much longer operation.

This evidence establishes exported-proof checking and challenge comparison.
It does not claim local source-to-export reproduction, execution of the repository's pinned
comparator workflow, native `leanchecker --fresh Main`, packet completion,
or the coordinator's final acceptance. Existing acceptance markers and the
module ledger are unchanged. The checker's `--verified` mode also retains the
I/O and runtime assumptions described by its pinned source; it is not a claim
that the entire executable and toolchain are formally verified.
