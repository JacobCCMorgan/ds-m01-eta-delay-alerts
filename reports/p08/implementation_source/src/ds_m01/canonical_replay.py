"""Run and verify the fixed M3b.2 container protocol without changing the repo.

Host operations use only the standard library. Training and numerical binding
checks remain in the original candidate replay implementation inside the image.
Receipts distinguish an observed replay from predictive or publication approval.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import zipfile

PROTOCOL = "m3b2-canonical-v1"
SCHEMA = 1
REFERENCE_TREE = "c6476c62393c1bdc42ae654dc8d2f31d100a9868"
BASE_IMAGE = "docker.io/library/python@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a"
REPLAY_FILES = ("models.json", "predictions.jsonl", "training_rows.jsonl", "summary.json", "run_receipt.json")
THREAD_POLICY = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
PINS = {"duckdb": "1.4.1", "joblib": "1.5.3", "numpy": "2.3.5", "psycopg": "3.2.10", "psycopg-binary": "3.2.10", "pyarrow": "21.0.0", "scikit-learn": "1.8.0", "scipy": "1.17.0", "threadpoolctl": "3.6.0", "typing-extensions": "4.16.0"}
NUMERICAL_PINS = {key: PINS[key] for key in ("joblib", "numpy", "scikit-learn", "scipy", "threadpoolctl")}
PAYLOAD_FILES = ("inner_replay.py", "runtime_spec.json", "source_manifest.json")
RUNTIME_DIR = "reproducibility/m3b2"
CONTRACT_FILES = frozenset(("src/ds_m01/canonical_replay.py", "scripts/check_canonical_candidate_replay.py", *(RUNTIME_DIR + "/" + name for name in PAYLOAD_FILES), *(RUNTIME_DIR + "/runtime/" + name for name in ("Dockerfile", "requirements-linux-amd64.lock", ".dockerignore"))))
SOURCE_FILES = frozenset((
    "config/m2.json", "config/m3a.json", "config/m3b1.json", "config/m3b2.json",
    "contracts/m3b1_features.json", "data/raw/eta_trip_outcome.jsonl",
    "reports/m3a/frozen_history.jsonl", "reports/m3a/run_receipt.json",
    "reports/m3b1/features.jsonl", "reports/m3b1/matrices.jsonl", "reports/m3b1/memberships.jsonl",
    "reports/m3b1/run_receipt.json", "reports/m3b1/summary.json", "reports/m3b1/transformers.json",
    "reports/m3b2/models.json", "reports/m3b2/predictions.jsonl", "reports/m3b2/run_receipt.json",
    "reports/m3b2/summary.json", "reports/m3b2/training_rows.jsonl", "reports/rebuild_report.json",
    "requirements-models.lock", "requirements.lock", "src/ds_m01/__init__.py",
    "src/ds_m01/baseline.py", "src/ds_m01/candidate_model.py", "src/ds_m01/candidate_replay.py",
    "src/ds_m01/candidate_run.py", "src/ds_m01/candidate_targets.py", "src/ds_m01/common.py",
    "src/ds_m01/feature_transform.py", "src/ds_m01/features.py", "src/ds_m01/model_splits.py"))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    def invalid(value):
        raise ValueError(f"Nonfinite JSON value: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid)


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def relative_file(root, relative):
    if not isinstance(relative, str):
        raise ValueError("File names must be strings")
    name = PurePosixPath(relative)
    if not relative or name.is_absolute() or ".." in name.parts or str(name) != relative or "\\" in relative:
        raise ValueError(f"Unsafe relative path: {relative!r}")
    current = Path(root)
    for part in name.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink is not allowed: {relative}")
    if not current.is_file():
        raise ValueError(f"Required regular file is missing: {relative}")
    return current


def verify_files(root, files, *, exact=False):
    if not isinstance(files, dict) or not files:
        raise ValueError("Empty or invalid file manifest")
    for relative, expected in files.items():
        if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError(f"Invalid SHA256: {relative}")
        if digest(relative_file(root, relative)) != expected:
            raise ValueError(f"File hash changed: {relative}")
    if exact:
        actual = set()
        for path in Path(root).rglob("*"):
            if path.is_symlink():
                raise ValueError("Symlink in snapshot")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != set(files):
            raise ValueError("Snapshot has missing or unexpected files")


def verify_contract(root):
    root = Path(root).expanduser().resolve(strict=True)
    contract = read_json(relative_file(root, RUNTIME_DIR + "/contract.json"))
    if contract.get("schema_version") != SCHEMA or contract.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported canonical runtime contract")
    if set(contract.get("files_sha256", {})) != CONTRACT_FILES:
        raise ValueError("Runtime contract must bind its complete executable file set")
    verify_files(root, contract["files_sha256"])
    if digest(Path(__file__).resolve()) != contract["files_sha256"]["src/ds_m01/canonical_replay.py"]:
        raise ValueError("The running host module differs from the declared contract")
    payload = root / RUNTIME_DIR
    source = read_json(payload / "source_manifest.json")
    spec = read_json(payload / "runtime_spec.json")
    if (source.get("schema_version") != SCHEMA or source.get("reference_tree") != REFERENCE_TREE
            or source.get("source_file_count") != len(SOURCE_FILES)
            or set(source.get("files_sha256", {})) != SOURCE_FILES):
        raise ValueError("Source manifest must cover the complete frozen reference")
    if (spec.get("protocol") != PROTOCOL or spec.get("schema_version") != SCHEMA
            or spec.get("platform") != "linux/amd64" or spec.get("python_version") != "3.12.14"
            or spec.get("base_image") != BASE_IMAGE or spec.get("packages") != PINS
            or spec.get("thread_environment") != THREAD_POLICY):
        raise ValueError("Runtime contract differs from the fixed protocol")
    if f"FROM {BASE_IMAGE}\n" not in (payload / "runtime/Dockerfile").read_text():
        raise ValueError("Dockerfile differs from the fixed base image")
    return contract, source, spec


def docker_environment():
    # Context selection is resolved once; later commands use the frozen endpoint.
    environment = dict(os.environ)
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    return environment


def capture(argv, *, cwd=None):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False,
                            env=docker_environment() if "--host" in argv else None)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]}\n{result.stderr.strip()}")
    return result.stdout.strip()


def verify_repository(root, source):
    root = Path(root).expanduser().resolve(strict=True)
    actual_root = Path(capture(["git", "rev-parse", "--show-toplevel"], cwd=root)).resolve()
    if actual_root != root:
        raise ValueError("--root must identify the repository root")
    if capture(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root):
        raise ValueError("Repository is not clean; preserve existing changes and stop")
    verify_files(root, source["files_sha256"])
    return {"root": str(root), "head_commit": capture(["git", "rev-parse", "HEAD"], cwd=root),
            "tree": capture(["git", "rev-parse", "HEAD^{tree}"], cwd=root),
            "reference_tree": source["reference_tree"],
            "source_manifest_sha256": digest(root / RUNTIME_DIR / "source_manifest.json"),
            "retained_receipt_sha256": source["files_sha256"]["reports/m3b2/run_receipt.json"]}


def docker_args(arguments, host=None):
    return ["docker", *( ["--host", host["endpoint"]] if host else []), *arguments]


def docker_host():
    if shutil.which("docker") is None:
        raise RuntimeError("Docker CLI is not available")
    context = capture(["docker", "context", "show"])
    contexts = json.loads(capture(["docker", "context", "inspect", context]))
    if not isinstance(contexts, list) or len(contexts) != 1:
        raise ValueError("Docker context is ambiguous")
    context_endpoint = contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
    endpoint = context_endpoint if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST") or context_endpoint
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///") or any(c in endpoint for c in "\n\r"):
        raise ValueError("A local Unix-socket Docker endpoint is required")
    host = {"recorded_at_utc": now(), "context": context, "endpoint": endpoint}
    version = json.loads(capture(docker_args(["version", "--format", "{{json .}}"], host)))
    platform = json.loads(capture(docker_args(["info", "--format", '{"os":"{{.OSType}}","architecture":"{{.Architecture}}"}'], host)))
    if not version.get("Server") or platform.get("os") != "linux":
        raise RuntimeError("A running Linux Docker engine is required")
    host.update(version=version, server_platform=platform)
    return host


def inspect_image(reference, host=None):
    if not isinstance(reference, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", reference) is None:
        raise ValueError("--image must be a complete immutable local sha256 image ID")
    data = json.loads(capture(docker_args(["image", "inspect", reference], host)))
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("Docker returned an ambiguous image inspection")
    image = data[0]
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64" or image.get("Id") != reference:
        raise ValueError("Image identity or Linux/amd64 platform differs from the request")
    return image


def preflight(root, image_id):
    root = Path(root).expanduser().resolve(strict=True)
    contract, source, spec = verify_contract(root)
    repository = verify_repository(root, source)
    host = docker_host()
    image = inspect_image(image_id, host)
    return {"status": "passed_preflight_only", "protocol": PROTOCOL, "recorded_at_utc": now(),
            "repository": repository, "docker_host": host, "docker_image_id": image["Id"],
            "contract_sha256": digest(root / RUNTIME_DIR / "contract.json"),
            "configured_base_image": spec["base_image"], "training_executed": False, "publication_approved": False}


def safe_work_path(path, root=None):
    raw = Path(path).expanduser()
    if not raw.is_absolute() or any(c in str(raw) for c in (",", "\n", "\r")):
        raise ValueError("--output must be absolute and cannot contain a comma or newline")
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("--output cannot traverse a symlink")
    work = raw.resolve()
    if root is not None and work.is_relative_to(Path(root).resolve()):
        raise ValueError("Replay output must be outside the repository")
    return work


def mount(source, target, readonly=True):
    source = Path(source).resolve()
    if any(c in str(source) for c in (",", "\n", "\r")):
        raise ValueError("Unsupported character in Docker mount source")
    return f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")


def container_command(image_id, output, mode, *, payload, snapshot=None, previous=None, host=None):
    if os.getuid() == 0:
        raise ValueError("Use a non-root host account for container replay")
    if mode not in ("runtime", "replay") or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None:
        raise ValueError("Invalid container mode or image identity")
    arguments = ["run", "--rm", "--platform", "linux/amd64", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777", "--mount", mount(payload, "/payload"), "--mount", mount(output, "/results", False)]
    for key, value in sorted({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HOME": "/tmp", **THREAD_POLICY}.items()):
        arguments += ["--env", f"{key}={value}"]
    if snapshot is not None:
        arguments += ["--mount", mount(snapshot, "/repo")]
    if previous is not None:
        arguments += ["--mount", mount(previous, "/previous")]
    arguments += [image_id, "python", "-B", "-u", "-I", "/payload/inner_replay.py", mode]
    return docker_args(arguments, host)


def run_logged(argv, log):
    with Path(log).open("x", encoding="utf-8") as output:
        output.write(json.dumps({"argv": argv, "started_at_utc": now()}) + "\n")
        output.flush()
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, env=docker_environment())
        try:
            for line in process.stdout:
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
            return process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise


def stage(root, destination, source):
    destination = Path(destination)
    destination.mkdir(exist_ok=False)
    for relative, expected in source["files_sha256"].items():
        original = relative_file(root, relative)
        if digest(original) != expected:
            raise ValueError(f"Source changed while staging: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with original.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        target.chmod(0o444)
    verify_files(destination, source["files_sha256"], exact=True)


def _runtime_valid(result, payload, mode):
    spec = read_json(payload / "runtime_spec.json")
    runtime = result.get("runtime", {})
    if (result.get("status") != "passed" or result.get("protocol") != PROTOCOL or result.get("mode") != mode
            or result.get("publication_approved") is not False
            or result.get("runtime_spec_sha256") != digest(payload / "runtime_spec.json")
            or runtime.get("python_version") != "3.12.14" or spec.get("packages") != PINS
            or spec.get("protocol") != PROTOCOL or spec.get("python_version") != "3.12.14"
            or spec.get("platform") != "linux/amd64" or spec.get("base_image") != BASE_IMAGE
            or spec.get("thread_environment") != THREAD_POLICY
            or runtime.get("package_versions") != PINS or runtime.get("system") != "Linux"
            or runtime.get("machine") not in ("x86_64", "AMD64")
            or runtime.get("thread_environment") != THREAD_POLICY or runtime.get("isolated_interpreter") is not True
            or type(runtime.get("euid")) is not int or runtime["euid"] <= 0
            or runtime.get("binary_imports_verified") is not True
            or type(runtime.get("pip_check", {}).get("exit_code")) is not int
            or runtime["pip_check"]["exit_code"] != 0):
        raise ValueError("Container receipt does not verify the complete fixed runtime")


def require_statistics(value, expected_count):
    keys = {"compared_float_values", "max_absolute_difference", "max_relative_difference", "max_eta_difference_microseconds"}
    if not isinstance(value, dict) or set(value) != keys or type(value.get("compared_float_values")) is not int or value["compared_float_values"] != expected_count:
        raise ValueError("Numerical comparison statistics are incomplete")
    for key in keys - {"compared_float_values"}:
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise ValueError("Numerical comparison statistics are invalid")
    if value["max_eta_difference_microseconds"] > 50:
        raise ValueError("Numerical comparison statistics exceed the fixed ETA tolerance")


def require_inner_result(folder, mode, *, payload, reference=None):
    folder, payload = Path(folder), Path(payload)
    result = read_json(relative_file(folder, "inner_receipt.json"))
    _runtime_valid(result, payload, mode)
    if mode == "runtime":
        if result.get("training_attempted") is not False:
            raise ValueError("Runtime-only check cannot claim training")
        return result
    if reference is None or result.get("training_attempted") is not True or result.get("model_count") != 24:
        raise ValueError("Replay receipt must report all models and actual training")
    reference = Path(reference)
    if (result.get("source_manifest_sha256") != digest(payload / "source_manifest.json")
            or result.get("retained_receipt_sha256") != digest(reference / "reports/m3b2/run_receipt.json")):
        raise ValueError("Replay source or retained-receipt binding changed")
    replay = folder / "replay"
    for name in REPLAY_FILES:
        relative_file(folder, "replay/" + name)
    original = read_json(reference / "reports/m3b2/run_receipt.json")
    receipt = read_json(replay / "run_receipt.json")
    for name in ("input_sha256", "settings_sha256", "code_sha256", "package_versions", "batch_id"):
        if receipt.get(name) != original.get(name):
            raise ValueError(f"Replay receipt context changed: {name}")
    if receipt.get("package_versions") != NUMERICAL_PINS:
        raise ValueError("Replay numerical package versions changed")
    if set(receipt.get("output_sha256", {})) != set(REPLAY_FILES) - {"run_receipt.json"}:
        raise ValueError("Replay receipt must bind every core output")
    verify_files(replay, receipt["output_sha256"])
    models = read_json(replay / "models.json")
    summary = read_json(replay / "summary.json")
    if not isinstance(models, list) or len(models) != 24:
        raise ValueError("Replay output must contain all 24 models")
    with (replay / "predictions.jsonl").open(encoding="utf-8") as stream:
        if sum(1 for line in stream if line.strip()) != 10326:
            raise ValueError("Replay output must contain all 10326 predictions")
    if summary.get("prediction_rows") != 10326 or summary.get("validation_opportunities") != 5163:
        raise ValueError("Replay summary coverage changed")
    answer = result.get("replay_result", {})
    if (answer.get("status") != "passed" or answer.get("numerical_models_equal") is not True
            or answer.get("numerical_predictions_equal") is not True or answer.get("retained_inference_equal") is not True
            or answer.get("prediction_rows") != 10326 or answer.get("validation_opportunities") != 5163
            or answer.get("exact_core_artifacts") != {"training_rows.jsonl": True, "summary.json": True}
            or answer.get("package_versions") != NUMERICAL_PINS):
        raise ValueError("Replay result does not satisfy the original complete gate")
    tolerance = answer.get("tolerances", {})
    if {key: tolerance.get(key) for key in ("absolute_float", "relative_float", "eta_microseconds")} != {"absolute_float": 1e-6, "relative_float": 1e-9, "eta_microseconds": 50}:
        raise ValueError("Replay tolerance contract changed")
    differences = answer.get("numerical_differences", {})
    if set(differences) != {"models", "predictions", "retained_inference"}:
        raise ValueError("Replay comparison evidence is incomplete")
    for label, count in (("models", 33580), ("predictions", 60546), ("retained_inference", 60546)):
        require_statistics(differences[label], count)
    for name in ("training_rows.jsonl", "summary.json"):
        if digest(replay / name) != original["output_sha256"][name]:
            raise ValueError(f"Exact replay output changed: {name}")
    for field, name in (("model_artifact_byte_identical", "models.json"), ("prediction_artifact_byte_identical", "predictions.jsonl")):
        if answer.get(field) is not (digest(replay / name) == original["output_sha256"][name]):
            raise ValueError(f"Replay byte-identity statement disagrees with files: {name}")
    return result


def require_pair(result, first, second):
    pair = result.get("pair_comparison", {})
    if (pair.get("status") != "passed" or pair.get("comparison_uses_original_binding_and_tolerances") is not True
            or pair.get("exact_core_artifacts") != {"training_rows.jsonl": True, "summary.json": True}):
        raise ValueError("Fresh runs did not pass a direct pair comparison")
    differences = pair.get("numerical_differences", {})
    if set(differences) != {"models", "predictions"}:
        raise ValueError("Direct pair comparison evidence is incomplete")
    require_statistics(differences["models"], 33580)
    require_statistics(differences["predictions"], 60546)
    for name in ("training_rows.jsonl", "summary.json"):
        if digest(first / name) != digest(second / name):
            raise ValueError("Fresh runs differ in an exact artifact")
    for field, name in (("model_artifact_byte_identical", "models.json"), ("prediction_artifact_byte_identical", "predictions.jsonl")):
        if pair.get(field) is not (digest(first / name) == digest(second / name)):
            raise ValueError("Pair byte-identity statement disagrees with files")


def run_replay(root, output, image_id):
    root = Path(root).expanduser().resolve(strict=True)
    ready = preflight(root, image_id)
    contract, source, _ = verify_contract(root)
    work = safe_work_path(output, root)
    work.mkdir(parents=False, exist_ok=False)
    write_json(work / "preflight.json", ready)
    receipt = {"protocol": PROTOCOL, "status": "started", "started_at_utc": now(),
               "repository": ready["repository"], "docker_host": ready["docker_host"],
               "docker_image_id": image_id, "contract_sha256": ready["contract_sha256"],
               "runs": [], "publication_approved": False, "training_attempted": False}
    try:
        image = inspect_image(image_id, ready["docker_host"])
        write_json(work / "image_inspect.json", image)
        payload_files = {name: contract["files_sha256"][RUNTIME_DIR + "/" + name] for name in PAYLOAD_FILES}
        receipt["payload_sha256"] = payload_files
        payload, snapshot = work / "payload_snapshot", work / "source_snapshot"
        stage(root / RUNTIME_DIR, payload, {"files_sha256": payload_files})
        stage(root, snapshot, source)
        runtime = work / "runtime_probe"
        runtime.mkdir()
        code = run_logged(container_command(image_id, runtime, "runtime", payload=payload, host=ready["docker_host"]), work / "runtime_probe.log")
        receipt["runtime_probe_exit_code"] = code
        if code:
            raise RuntimeError(f"Runtime probe failed with exit code {code}")
        require_inner_result(runtime, "runtime", payload=payload)
        receipt["runtime_receipt_sha256"] = digest(runtime / "inner_receipt.json")
        previous = None
        for number in (1, 2):
            verify_files(payload, payload_files, exact=True)
            verify_files(snapshot, source["files_sha256"], exact=True)
            if previous is not None:
                verify_files(previous, receipt["runs"][0]["output_hashes"], exact=True)
            folder = work / f"run_{number:02d}"
            folder.mkdir()
            receipt["training_attempted"] = True
            code = run_logged(container_command(image_id, folder, "replay", payload=payload, snapshot=snapshot, previous=previous, host=ready["docker_host"]), work / f"run_{number:02d}.log")
            record = {"run": number, "process_exit_code": code,
                      "output_hashes": {name: digest(relative_file(folder, "replay/" + name)) for name in REPLAY_FILES if (folder / "replay" / name).exists()}}
            receipt["runs"].append(record)
            if (folder / "inner_receipt.json").exists():
                record["inner_receipt_sha256"] = digest(relative_file(folder, "inner_receipt.json"))
            if code:
                raise RuntimeError(f"Replay run {number} failed with exit code {code}; subsequent runs are not started")
            result = require_inner_result(folder, "replay", payload=payload, reference=snapshot)
            if previous is not None:
                require_pair(result, previous, folder / "replay")
            record["status"] = "passed"
            previous = folder / "replay"
        verify_files(payload, payload_files, exact=True)
        verify_files(snapshot, source["files_sha256"], exact=True)
        for record in receipt["runs"]:
            verify_files(work / f"run_{record['run']:02d}/replay", record["output_hashes"], exact=True)
        if verify_repository(root, source) != ready["repository"]:
            raise ValueError("Repository identity changed during execution")
        verify_contract(root)
        receipt["status"] = "passed_two_container_replays_and_pair_comparison"
        receipt["limitation"] = "Observed execution surface only; no predictive-quality, untested-host or publication approval."
    except BaseException as exc:
        receipt.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        receipt["finished_at_utc"] = now()
        receipt["log_sha256"] = {name: digest(relative_file(work, name)) for name in ("runtime_probe.log", "run_01.log", "run_02.log") if (work / name).exists()}
        write_json(work / "execution_receipt.json", receipt)
    return receipt


def validate_evidence(root, work, image_id, *, archived=False):
    """Verify completed local evidence without Docker or model fitting."""
    root = Path(root).expanduser().resolve(strict=True)
    work = safe_work_path(work, root)
    contract, source, _ = verify_contract(root)
    repository = verify_repository(root, source)
    ready = read_json(relative_file(work, "preflight.json"))
    receipt = read_json(relative_file(work, "execution_receipt.json"))
    image = read_json(relative_file(work, "image_inspect.json"))
    if (receipt.get("status") != "passed_two_container_replays_and_pair_comparison"
            or receipt.get("protocol") != PROTOCOL or ready.get("protocol") != PROTOCOL
            or receipt.get("publication_approved") is not False or ready.get("publication_approved") is not False
            or ready.get("status") != "passed_preflight_only"
            or receipt.get("repository") != repository or ready.get("repository") != repository
            or receipt.get("contract_sha256") != digest(root / RUNTIME_DIR / "contract.json")
            or ready.get("contract_sha256") != receipt["contract_sha256"]
            or not isinstance(image_id, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None
            or receipt.get("docker_image_id") != image_id or ready.get("docker_image_id") != image_id
            or image.get("Id") != image_id or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or type(receipt.get("runtime_probe_exit_code")) is not int or receipt["runtime_probe_exit_code"] != 0
            or receipt.get("training_attempted") is not True):
        raise ValueError("Evidence identity, runtime status or repository binding changed")
    if set(receipt.get("log_sha256", {})) != {"runtime_probe.log", "run_01.log", "run_02.log"}:
        raise ValueError("Completed replay must bind all three process logs")
    verify_files(work, receipt["log_sha256"])
    payload_files = {name: contract["files_sha256"][RUNTIME_DIR + "/" + name] for name in PAYLOAD_FILES}
    if receipt.get("payload_sha256") != payload_files:
        raise ValueError("Evidence payload binding changed")
    if not archived:
        verify_files(work / "source_snapshot", source["files_sha256"], exact=True)
        verify_files(work / "payload_snapshot", payload_files, exact=True)
    payload = root / RUNTIME_DIR
    require_inner_result(work / "runtime_probe", "runtime", payload=payload)
    if receipt.get("runtime_receipt_sha256") != digest(relative_file(work, "runtime_probe/inner_receipt.json")):
        raise ValueError("Runtime evidence hash changed")
    if not isinstance(receipt.get("runs"), list) or len(receipt["runs"]) != 2:
        raise ValueError("Exactly two completed fresh runs are required")
    for number, record in enumerate(receipt["runs"], 1):
        folder = work / f"run_{number:02d}"
        if (record.get("run") != number or record.get("status") != "passed"
                or type(record.get("process_exit_code")) is not int or record["process_exit_code"] != 0
                or set(record.get("output_hashes", {})) != set(REPLAY_FILES)
                or record.get("inner_receipt_sha256") != digest(relative_file(folder, "inner_receipt.json"))):
            raise ValueError("Replay process evidence is incomplete or changed")
        verify_files(folder / "replay", record["output_hashes"], exact=True)
        result = require_inner_result(folder, "replay", payload=payload, reference=root)
        if number == 2:
            require_pair(result, work / "run_01/replay", folder / "replay")
    return {"status": "passed_evidence_verification", "protocol": PROTOCOL,
            "docker_image_id": image_id, "runs": 2, "training_executed_by_verifier": False,
            "docker_executed_by_verifier": False, "publication_approved": False}


def archive(work):
    work = safe_work_path(work)
    ready = read_json(relative_file(work, "preflight.json"))
    if ready.get("protocol") != PROTOCOL:
        raise ValueError("Not a canonical replay output folder")
    files = []
    for name in ("preflight.json", "image_inspect.json", "runtime_probe.log", "runtime_probe/inner_receipt.json", "execution_receipt.json", "run_01.log", "run_02.log"):
        if (work / name).exists() or (work / name).is_symlink():
            files.append(relative_file(work, name))
    for run in ("run_01", "run_02"):
        for name in ("inner_receipt.json", *("replay/" + item for item in REPLAY_FILES)):
            relative = run + "/" + name
            if (work / relative).exists() or (work / relative).is_symlink():
                files.append(relative_file(work, relative))
    destination = work.with_suffix(work.suffix + ".zip")
    manifest = {"protocol": PROTOCOL, "created_at_utc": now(), "publication_approved": False,
                "snapshots_included": False, "files_sha256": {path.relative_to(work).as_posix(): digest(path) for path in files}}
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in files:
            output.write(path, path.relative_to(work).as_posix())
        output.writestr("archive_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"status": "replay_archive_created", "zip": str(destination), "sha256": digest(destination),
            "bytes": destination.stat().st_size, "publication_approved": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("preflight", "run"):
        command = commands.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--image", required=True)
        if action == "run":
            command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("archive")
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "preflight":
            answer = preflight(args.root, args.image)
        elif args.action == "run":
            answer = run_replay(args.root, args.output, args.image)
        else:
            answer = archive(args.output)
        print(json.dumps(answer, indent=2, sort_keys=True, allow_nan=False))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
