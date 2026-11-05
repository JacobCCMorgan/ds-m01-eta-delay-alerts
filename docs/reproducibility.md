# Candidate replay and supported environments

The M3b.2 experiment is an **Engineering and Method Demonstration — Synthetic
Data**. Reproducibility here means preserving the frozen inputs, training
protocol, model bindings and prediction results. It does not establish accuracy,
calibrated coverage or operational benefit.

## Why training uses a fixed container

Native macOS fitting did not reproduce the retained gradient-boosting results
under the original replay contract, despite passing the unit suite and using the
same numerical package versions and random seed. Training rows and summary
counts remained identical. Retained-model inference passed separately.

The observed difference concerns fitted tree choices. The evidence supports
environment-sensitive fitting; it does not identify a particular CPU instruction,
compiler, Python patch version or library as the sole cause. Matching package
versions and a seed is insufficient to claim identical fitting across platforms.
The original native macOS failure remains a failure.

The fixed runtime keeps the original model parameters, folds, source bytes and
comparison tolerances. It uses the pinned Linux/amd64 Python base and exact wheel
hashes in [the runtime recipe](../reproducibility/m3b2/runtime/Dockerfile) and
[dependency lock](../reproducibility/m3b2/runtime/requirements-linux-amd64.lock).
The [runtime specification](../reproducibility/m3b2/runtime_spec.json) fixes Python
3.12.14, all ten dependency versions and five thread settings at `1`.

## Evidence and execution scope

The runtime experiment completed on **2026-11-01 UTC** using Docker Desktop on
an Apple Silicon Mac, with the container process reporting Linux x86_64. Both
fresh containers used the same built image and the original full replay gate.

| Check in the recorded runtime experiment | Result |
|---|---|
| Full replay against the original reference | Passed in both fresh containers |
| Models per run | 24: 12 linear and 12 gradient-boosting model states |
| Predictions per run | 10,326 across the same 5,163 validation opportunities |
| Core output bytes versus the original reference | Models, predictions, training rows and summary all identical |
| Direct comparison between the two runs | Same four core files identical |
| Maximum numeric and ETA difference | 0 |
| Actual Python / compiler / libc | Python 3.12.14 / GCC 12.2.0 / glibc 2.36 |

Execution receipts contain run-specific timestamps; their bytes differ even when
the four core outputs match. The initial experiment established the fixed
runtime. A separate execution then verified the integrated entry point at
`0814fa1`: two fresh containers each matched the reference and each other on all
four files. The [dated verification summary](../reports/m3b2_integrated_replay_20261031.json)
records the image ID, output hashes, 285 local tests and retained-inference
result. This was a local run on the setup below; no GitHub Actions run was
recorded for the tested commit.

| Execution surface | Supported conclusion |
|---|---|
| Recorded native Linux reference environment | Original retained experiment and native replay passed; this is not an OCI result |
| Native macOS retained-JSON inference | Observed pass without fitting or opening the outcome source |
| Native macOS exact retraining | Observed failure; unsupported for exact training replay |
| Recorded Mac Docker Desktop Linux/amd64 runtime | Two full replays and their direct comparison passed |
| Another native Linux Docker host or different CPU | Untested; no cross-host or cross-CPU guarantee |

A fixed image alone does not prove equivalence on another execution surface.
Validate each newly claimed surface with the complete gate. Supporting another
host is a separate extension, not a requirement to use the observed Mac setup.
CI, predictive evaluation, calibration and final holdout scoring remain separate
milestones.

## Run the integrated replay

Start in the repository root with a working local Docker daemon. The host command
uses Python's standard library; training dependencies remain inside the image.
The source tree must be clean. The frozen raw outcome file
`data/raw/eta_trip_outcome.jsonl` must already be present with its recorded SHA256;
see [candidate input requirements](m3b2_candidates.md#reproducibility-and-artifact-use).
The remaining model inputs and reference outputs are retained in the repository.
If generating the raw file from scratch, use the [M2 workflow](../README.md#run-locally)
in a separate checkout and copy just the generated outcome file into the clean
checkout used here. Preflight checks its digest before training.

If the tested image is already present in your local Docker engine, select it by
its full immutable ID:

```bash
replay_image_id=sha256:f7e8e96ea325cadf646467291df7109d7621812109f8b2723413b280497fecbc
```

Otherwise, build the pinned recipe from the repository root. Docker needs
access to the base image and locked wheels during the build. The tag below
names the local build; the commands that run it use its resolved image ID.

```bash
docker build --platform linux/amd64 -t ds-m01-m3b2-replay:local reproducibility/m3b2/runtime
replay_image_id="$(docker image inspect --format '{{.Id}}' ds-m01-m3b2-replay:local)"
```

The recorded ID is local to an engine that has that image; it is not a
registry address. A new build can have a different ID. Record that ID and run
the full check below before claiming a result for your build.

Check inputs, payload integrity, the local Docker endpoint and image platform:

```bash
python -I scripts/check_canonical_candidate_replay.py preflight --root . --image "$replay_image_id"
```

Expected status: `passed_preflight_only`. This does not run training. If a source,
configuration, payload or image check fails, preserve the error and correct its
cause before proceeding; do not change the reference hashes to silence it.

Run the actual runtime check and two complete training replays. The output path
below is an example of a **new absolute directory outside the repository**; it
must not already exist.

```bash
python -I scripts/check_canonical_candidate_replay.py run --root . --image "$replay_image_id" --output "$HOME/Documents/DS-M01_M3b2_Canonical_Integration_Run01"
```

Expected status: `passed_two_container_replays_and_pair_comparison`. Each run
must match the original reference, and the second must also match the first
directly. A runtime or replay failure stops subsequent execution and preserves
logs, receipts and any partial results. Use a different new output directory for
a later intentional run; do not delete evidence merely to reuse the example.

Archive a completed or failed run after the output directory has been created:

```bash
python -I scripts/check_canonical_candidate_replay.py archive --output "$HOME/Documents/DS-M01_M3b2_Canonical_Integration_Run01"
```

Expected status: `replay_archive_created`. The ZIP is created beside the output
directory and must not already exist. Archiving preserves evidence; it does not
turn a failed check into a pass. Keep the original output directory as well.

## Isolation, integrity and failure behavior

The runner verifies the frozen source allowlist and its own execution payload,
then copies only those files into checked snapshots. It mounts source and
payload read-only and gives each container a new output directory. It does not
mount the full repository, Git history, host virtual environment, home directory,
credentials or Docker socket. Training runs without network access, as the
host's non-root numeric user, with a read-only root filesystem and temporary
`/tmp` storage.

The original reference tree is provenance, not a requirement that later additive
documentation or runtime changes have the same Git tree. The current HEAD and
tree are recorded, while the exact original source/data hashes still control
the replay. The wrapper does not rewrite existing settings, requirements,
retained models or prediction artifacts.

Both full replays enforce exact structures, types, identifiers, order, counts
and decisions. Float comparisons retain absolute tolerance `1e-6` **or** relative
tolerance `1e-9`; ETA timestamps retain the 50-microsecond limit. Exact training
rows and summary are required. All five replay output files, their receipt
bindings and the direct pair comparison are mandatory. No parameter or tolerance
adjustment occurs automatically after a mismatch.

Unit tests exercise wrong identities, changed sources, malformed results,
reserved output paths and failed child processes. Stubbed container responses
verify control flow only. Actual runtime/import checks and the original full
model gate must still execute inside the selected container.

For retained JSON inference alone, use the pinned native model dependencies and:

```bash
python scripts/check_candidate_replay.py --root . --inference-only
```

That check reports `training_executed=false` and `outcome_source_opened=false`.
It is a separate consumption check and cannot replace full training replay.
