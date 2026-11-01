"""Container entry point for the unchanged M3b.2 replay gate."""

from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import traceback
from zoneinfo import ZoneInfo

PACKAGE = Path("/payload")
ROOT = Path("/repo")
RESULTS = Path("/results")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def verify_snapshot(source):
    files = source["files_sha256"]
    actual = set()
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            raise ValueError("Staged repository contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(ROOT).as_posix())
    if actual != set(files):
        raise ValueError("Staged repository file set differs from the frozen allowlist")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("Unsafe staged file path")
        if digest(ROOT / name) != expected:
            raise ValueError(f"Staged source or data changed: {name}")


def runtime_report(spec):
    report = {"python_version": platform.python_version(), "python_full": sys.version,
              "python_compiler": platform.python_compiler(), "platform": platform.platform(),
              "system": platform.system(), "machine": platform.machine(),
              "libc": list(platform.libc_ver()), "package_versions": {},
              "thread_environment": {key: os.environ.get(key) for key in spec["thread_environment"]},
              "isolated_interpreter": bool(sys.flags.isolated),
              "source_import_strategy": "Explicit validated /repo/src insertion; Python isolated mode ignores PYTHONPATH.",
              "euid": os.geteuid(), "egid": os.getegid()}
    for name in sorted(spec["packages"]):
        report["package_versions"][name] = importlib.metadata.version(name)
    report["pip_version"] = importlib.metadata.version("pip")
    cpu = Path("/proc/cpuinfo")
    if cpu.is_file():
        first = cpu.read_text().split("\n\n", 1)[0]
        report["cpu_observation"] = {key.strip(): value.strip() for line in first.splitlines()
                                     if ":" in line for key, value in [line.split(":", 1)]
                                     if key.strip() in {"vendor_id", "model name", "flags", "Features", "CPU architecture"}}
    return report


def validate_runtime(report, spec):
    if report["python_version"] != spec["python_version"]:
        raise ValueError("Container Python patch version differs from the fixed candidate")
    if report["system"] != "Linux" or report["machine"] not in ("x86_64", "AMD64"):
        raise ValueError("Container process is not Linux/x86_64")
    if report["package_versions"] != spec["packages"]:
        raise ValueError("Container dependency versions differ from the complete ten-package lock")
    if report["thread_environment"] != spec["thread_environment"]:
        raise ValueError("Container thread policy differs from the frozen candidate")
    if not report["isolated_interpreter"]:
        raise ValueError("Container Python must use isolated mode")
    if report["euid"] == 0:
        raise ValueError("Container process must run as a non-root user")
    names = {"scikit-learn": "sklearn", "psycopg-binary": "psycopg_binary", "typing-extensions": "typing_extensions"}
    for name in spec["packages"]:
        importlib.import_module(names.get(name, name))
    report["binary_imports_verified"] = True
    report["configured_timezone_available"] = str(ZoneInfo("America/Los_Angeles"))
    check = subprocess.run([sys.executable, "-B", "-I", "-m", "pip", "--disable-pip-version-check", "check"], text=True, capture_output=True, check=False)
    report["pip_check"] = {"exit_code": check.returncode, "stdout": check.stdout, "stderr": check.stderr}
    if check.returncode:
        raise ValueError("Container dependency closure check failed")


def compare_pair(previous, current):
    from ds_m01.candidate_replay import compare_numerical, compare_predictions, verify_receipt
    from ds_m01.candidate_run import load_retained_inputs
    from ds_m01.common import read_jsonl
    before, settings = verify_receipt(ROOT, previous)
    after, _ = verify_receipt(ROOT, current)
    for field in ("input_sha256", "settings_sha256", "code_sha256", "package_versions"):
        if before[field] != after[field]:
            raise ValueError(f"Fresh runs have different execution inputs: {field}")
    exact = {name: digest(previous / name) == digest(current / name) for name in ("training_rows.jsonl", "summary.json")}
    if not all(exact.values()):
        raise ValueError("Fresh runs differ in exact training rows or summary")
    first_models, next_models = load(previous / "models.json"), load(current / "models.json")
    predictions = compare_predictions(read_jsonl(previous / "predictions.jsonl"),
                                      read_jsonl(current / "predictions.jsonl"), first_models, next_models,
                                      load_retained_inputs(ROOT), settings, before["package_versions"])
    models = compare_numerical(first_models, next_models)
    return {"status": "passed", "exact_core_artifacts": exact,
            "numerical_differences": {"models": models, "predictions": predictions},
            "model_artifact_byte_identical": digest(previous / "models.json") == digest(current / "models.json"),
            "prediction_artifact_byte_identical": digest(previous / "predictions.jsonl") == digest(current / "predictions.jsonl"),
            "comparison_uses_original_binding_and_tolerances": True}


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else None
    if mode not in {"runtime", "replay"}:
        raise ValueError("Expected runtime or replay mode")
    if (RESULTS / "inner_receipt.json").exists():
        raise FileExistsError("Container receipt already exists")
    receipt = {"protocol": "m3b2-canonical-v1", "mode": mode, "status": "started",
               "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "training_attempted": False, "publication_approved": False}
    code = 1
    try:
        spec = load(PACKAGE / "runtime_spec.json")
        receipt["runtime_spec_sha256"] = digest(PACKAGE / "runtime_spec.json")
        receipt["runtime"] = runtime_report(spec)
        validate_runtime(receipt["runtime"], spec)
        if mode == "replay":
            source = load(PACKAGE / "source_manifest.json")
            verify_snapshot(source)
            receipt["source_manifest_sha256"] = digest(PACKAGE / "source_manifest.json")
            receipt["retained_receipt_sha256"] = digest(ROOT / "reports/m3b2/run_receipt.json")
            sys.path.insert(0, str(ROOT / "src"))
            from ds_m01.candidate_replay import check
            receipt["training_attempted"] = True
            receipt["replay_result"] = check(ROOT, RESULTS / "replay")
            if receipt["replay_result"].get("status") != "passed":
                raise ValueError("Original replay gate did not return passed")
            receipt["model_count"] = len(load(RESULTS / "replay/models.json"))
            if receipt["model_count"] != 24:
                raise ValueError("Replay did not retain all 24 candidate models")
            if Path("/previous").is_dir():
                receipt["pair_comparison"] = compare_pair(Path("/previous"), RESULTS / "replay")
            verify_snapshot(source)
        receipt["status"] = "passed"
        code = 0
    except BaseException as exc:
        receipt.update(status="failed", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        traceback.print_exc()
    finally:
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        with (RESULTS / "inner_receipt.json").open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    print(json.dumps({"status": receipt["status"], "mode": mode, "publication_approved": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
