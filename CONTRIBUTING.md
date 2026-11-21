# Contribute a proof check

For a whole exported-proof check, see the
[recorded parallel con-leche comparison](evidence/whole-export-compare/README.md).
It checked the complete published export and all five statements in under
eight minutes with prepared inputs. This is a separate contribution from the
source-build packets below; it does not reserve or complete packet targets,
and it does not replace the coordinator's final acceptance requirements.

The index gives each of the 175,187 native Lean modules a stable ID for this archive version. There are 2,858 packets of at most 64 modules at the same project-import depth. Within a packet the modules can build concurrently after their prerequisites are ready. Reading the index and building a module require Python's standard library. Creating a claim with the CLI also requires an authenticated GitHub CLI.

When GitHub CLI is signed in to github.com, the tracker uses that account’s API quota to read open claims. Anonymous lookups are also supported.

## Choose and claim work

```bash
python3 tracker/tasks.py next
python3 tracker/tasks.py show M000001
```

For a worker with the pinned Mathlib cache and no project build outputs, run
`python3 tracker/tasks.py next --cold`. This lists individual modules with no
project imports, including available members of partially occupied packets.
It skips observed, assigned, and claimed modules. Suggestions first favor
modules that are the sole project import of more available tasks, then those
with more available direct consumers. These counts describe the source graph.
Use the displayed module ID to claim a single module.

Choose a packet from the live suggestions or a single open module. See [Choosing a task](#choosing-a-task) below for how depth affects build cost and how to work a partially completed packet. Replace the sample identifier below with your selection.

```bash
python3 tracker/tasks.py claim P0123
```

The claim command opens a public issue. You can also open an issue manually with a title of the form `Claim P0123` or `Claim M000001`. Check for overlapping claims after submitting. Wait for the claim-granted label and matching bot comment before starting. A serialized workflow preserves existing valid grants and processes simultaneous new requests in issue-number order. Close a claim if you stop working on it. The snapshot states in the index describe coordinator progress at the recorded time. Open claim issues supply the live volunteer assignments. The coordinator's full run also continues in the background.

## Choosing a task

Depth is project-import depth: it counts the chain of project modules a module imports. Mathlib arrives prebuilt from the pinned cache, but project modules do not. On a cold machine Lake builds the transitive project imports of your target locally before the target itself, so cold build cost grows quickly with depth. A shallow packet or a single shallow module is the right first task; a depth-100 packet can mean building thousands of project modules first. A first build on a new machine also pays the one-time prepare step: the archive download and the Mathlib cache fill a volume of roughly 11 GB.

`next` suggests packets whose modules are all still open. Once any module in a packet has evidence, the packet leaves those suggestions even though its remaining modules are still open work. Run

```bash
python3 tracker/tasks.py next --include-partial
```

to also list these partial packets, with open/total counts, shallowest and most-open first. Packets containing coordinator-assigned modules are not listed. A partial packet cannot be claimed as a packet: the claim bot reserves every module that already has evidence, so a whole-packet claim would conflict. Instead, list the packet's modules with `python3 tracker/tasks.py show P0123`, pick an open one, and claim it individually with `python3 tracker/tasks.py claim M000001`. The `--cold` listing already works this way: it lists individual open modules, including members of partial packets.

## Build a task

Use Linux with elan, Git, GNU tar, sha256sum, a C toolchain and adequate storage. Prepare a clean checkout using the original release and the pinned checksums. Keep the generated receipt directory outside the source tree.

```bash
./verify.sh /path/to/new/replay-volume --prepare-only
python3 tracker/tasks.py build P0123 /path/to/new/replay-volume/lean /path/to/new/receipt --bundle
```

The task command checks the selected sources and pinned mathlib revision, builds the targets with Lake, and saves the command, exit status, toolchain identity, output hashes and build transcript. Lake will build missing imports locally. A module late in the dependency graph can require substantial work on a cold machine. The optional bundle contains the selected modules' generated build files. Use `--fast-bundle` to package the same files with gzip level 1 when worker time matters more than transfer size. Both variants record the final archive's SHA-256 in the receipt. The public index lists direct project dependencies as task IDs.

## Build on Windows or macOS with Docker

The replay scripts expect Linux. On Windows or macOS, run them in a Docker container. Building the project runs Lean code from the archive, so do not mount host folders into the container. Use a named volume for the build.

```bash
docker volume create rhwork
docker run -d --name rhreplay -v rhwork:/work ubuntu:24.04 sleep infinity
docker exec -it rhreplay bash
```

Inside the container:

```bash
apt-get update && apt-get install -y curl git python3 build-essential ca-certificates
curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh
sh /tmp/elan-init.sh -y --default-toolchain none
ln -s /root/.elan/bin/* /usr/local/bin/
git clone https://github.com/rohanarun/alpoge-rh-replay /work/repo
cd /work/repo
./verify.sh /work/vol --prepare-only
python3 tracker/tasks.py next --cold
```

The container has no GitHub login. Claim from the host instead, either on the website or with the GitHub CLI:

```bash
gh issue create --repo rohanarun/alpoge-rh-replay --title "Claim M000001" --body "I am working on M000001."
```

After the grant, build inside the container, then copy the receipt to the host for your submission:

```bash
python3 tracker/tasks.py build M000001 /work/vol/lean /work/receipt-M000001 --bundle
```

```bash
docker cp rhreplay:/work/receipt-M000001 .
```

Notes:

- Docker Desktop limits the container's memory. With the WSL 2 backend on Windows, the limit comes from WSL, which defaults to half of the host RAM. Raise it with `memory=` under `[wsl2]` in `%UserProfile%\.wslconfig`, then run `wsl --shutdown`. On macOS, use Docker Desktop's Resources settings. Run `free -g` in the container to see the limit.
- The prepare step downloads about 8,700 Mathlib cache files and can be slow. After that, a cold module builds in minutes. One took about 5 minutes on 16 vCPUs.
- `ubuntu:24.04` has no `/usr/bin/time`. Use the shell's `time` keyword to log elapsed time, or install the `time` package.
- In Git Bash on Windows, prefix `docker exec` and `docker cp` commands that contain container paths with `MSYS_NO_PATHCONV=1`. Otherwise Git Bash rewrites `/work/...` into a Windows path.
- Remove everything afterwards with `docker rm -f rhreplay` and `docker volume rm rhwork`.

## Troubleshoot a Lean version mismatch

If `Cache/Requests.lean` reports a missing `Lake.PackageEntrySrc.path` case and the trace shows Lean 4.35.0, update your checkout. The replay uses Lean 4.33.1, and the verifier now calls that version explicitly through elan. Keep elan on PATH, then retry in a new directory on the build volume.

```sh
git pull --ff-only
elan --version
./verify.sh /path/to/fresh-replay --prepare-only
```

The run prints its Lean version near the start of the cache step. If it still fails, share that version and the first failing excerpt from the new directory's `replay.log` on your claim.

## Submit evidence

Begin a comment on your claim with `Replay submission` on its own first line. Include the task or packet ID, exit status, elapsed time, and links to the receipt and log. For a large bundle, attach it to a release on your fork and include its SHA-256 digest. Check logs for personal paths before publishing. The README credits you while your evidence is pending. Keep the claim open for review. Maintainers check the grant, pinned toolchain and inputs, exact target list, successful transcript, and artifact digests before marking a volunteer build complete. Reviewed receipts go in `evidence/volunteers/` and appear in the README and module ledger. Maintainers close accepted claims as completed. Any later reuse or kernel replay is recorded separately. If a build fails, include the first Lean error, file and line, resource limits, and reproduction command.

The final acceptance record separately checks the standard RH proposition, its axiom dependencies and the fresh kernel replay of Main. Module receipts are evidence of partial progress.

## Coordinator and cloud helpers

A targeted cloud helper uses the same public claim queue as volunteers. Open a standard module or packet claim and wait for both the `claim-granted` label and the matching GitHub Actions bot comment. Before provisioning, the coordinator runs `python3 tracker/check_grant.py BATCH.json ISSUE_NUMBER` using the GitHub account that opened the claim. The batch's `targets` must exactly match the claimed module or packet. When a worker has multiple grants, pass every issue number. GitHub lookup failures stop provisioning.

Post each helper's replay submission on its claim and keep the issue open while the evidence is reviewed. Close abandoned claims so others can pick them up. Lake may compile prerequisites along the way, and the primary still walks the full dependency graph for the end-to-end replay.
