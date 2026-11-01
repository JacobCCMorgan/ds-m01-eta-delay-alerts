"""Canonical-runtime integrity tests; Docker calls are mocked, not OCI evidence."""

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ds_m01 import canonical_replay as canonical


PROJECT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64


def zero_differences(count):
    return {"compared_float_values": count, "max_absolute_difference": 0.0,
            "max_relative_difference": 0.0, "max_eta_difference_microseconds": 0.0}


class CanonicalPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_paths_cannot_escape_or_follow_symlinks(self):
        (self.folder / "source").write_text("original")
        (self.folder / "linked").symlink_to(self.folder / "source")
        (self.folder / "directory").mkdir()
        (self.folder / "parent_link").symlink_to(self.folder / "directory", target_is_directory=True)
        for name in ("../source", "/source", "linked", "parent_link/missing", "directory/../source", "directory//source"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                canonical.relative_file(self.folder, name)

    def test_snapshot_rejects_changed_missing_and_extra_files(self):
        path = self.folder / "source"
        path.write_text("original")
        expected = {"source": canonical.digest(path)}
        path.write_text("changed")
        with self.assertRaises(ValueError):
            canonical.verify_files(self.folder, expected, exact=True)
        path.unlink()
        with self.assertRaises(ValueError):
            canonical.verify_files(self.folder, expected, exact=True)
        path.write_text("original")
        (self.folder / "extra").write_text("unexpected")
        with self.assertRaises(ValueError):
            canonical.verify_files(self.folder, expected, exact=True)

    def test_snapshot_rejects_an_unlisted_symlink(self):
        path = self.folder / "source"
        path.write_text("original")
        expected = {"source": canonical.digest(path)}
        (self.folder / "extra").symlink_to(path)
        with self.assertRaises(ValueError):
            canonical.verify_files(self.folder, expected, exact=True)

    def test_output_requires_absolute_path_outside_repository(self):
        with self.assertRaises(ValueError):
            canonical.safe_work_path(Path("relative-results"), self.folder)
        for value in (self.folder, self.folder / "results"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                canonical.safe_work_path(value, self.folder)

    def test_output_rejects_mount_delimiters_and_symlink_parents(self):
        (self.folder / "real").mkdir()
        (self.folder / "link").symlink_to(self.folder / "real", target_is_directory=True)
        for value in (self.folder / "comma,name", self.folder / "line\nbreak", self.folder / "link/output"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                canonical.safe_work_path(value)

    def test_staging_preserves_source_bytes_and_only_copies_manifest_files(self):
        root = self.folder / "repo"
        root.mkdir()
        (root / "input").write_text("frozen source")
        (root / "unlisted").write_text("must not be staged")
        source = {"files_sha256": {"input": canonical.digest(root / "input")}}
        destination = self.folder / "snapshot"
        canonical.stage(root, destination, source)
        self.assertEqual((destination / "input").read_bytes(), (root / "input").read_bytes())
        self.assertFalse((destination / "unlisted").exists())
        self.assertEqual((destination / "input").stat().st_mode & 0o222, 0)
        with self.assertRaises(FileExistsError):
            canonical.stage(root, destination, source)

    def test_changed_source_is_rejected_before_copying_its_bytes(self):
        root = self.folder / "repo"
        root.mkdir()
        path = root / "input"
        path.write_text("before")
        source = {"files_sha256": {"input": canonical.digest(path)}}
        path.write_text("after")
        destination = self.folder / "snapshot"
        with self.assertRaises(ValueError):
            canonical.stage(root, destination, source)
        self.assertFalse((destination / "input").exists())
        self.assertEqual(path.read_text(), "after")


class CanonicalDockerTests(unittest.TestCase):
    def test_image_reference_must_be_a_full_immutable_id(self):
        for reference in ("python:latest", "python:3.12", "sha256:abc", "a" * 64):
            with self.subTest(reference=reference), patch.object(canonical, "capture") as capture:
                with self.assertRaises(ValueError):
                    canonical.inspect_image(reference)
                capture.assert_not_called()

    def test_image_inspection_rejects_platform_identity_and_ambiguity(self):
        valid = {"Os": "linux", "Architecture": "amd64", "Id": IMAGE}
        cases = [[dict(valid, Architecture="arm64")], [dict(valid, Os="windows")],
                 [dict(valid, Id=OTHER_IMAGE)], [], [valid, valid]]
        for answer in cases:
            with self.subTest(answer=answer), patch.object(canonical, "capture", return_value=json.dumps(answer)):
                with self.assertRaises(ValueError):
                    canonical.inspect_image(IMAGE)

    def test_matching_immutable_image_is_accepted(self):
        expected = {"Os": "linux", "Architecture": "amd64", "Id": IMAGE}
        with patch.object(canonical, "capture", return_value=json.dumps([expected])):
            self.assertEqual(canonical.inspect_image(IMAGE), expected)

    def test_subprocess_failure_cannot_be_returned_as_success(self):
        failure = subprocess.CompletedProcess(["docker", "version"], 17, "", "daemon failed")
        with patch.object(canonical.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "17"):
                canonical.capture(["docker", "version"])

    def test_remote_context_wins_over_local_host_environment_and_is_rejected(self):
        responses = ["remote", json.dumps([{"Endpoints": {"docker": {"Host": "ssh://remote"}}}])]
        with patch.object(canonical.shutil, "which", return_value="/usr/bin/docker"), \
                patch.object(canonical, "capture", side_effect=responses), \
                patch.dict(canonical.os.environ, {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///local.sock"}, clear=True):
            with self.assertRaises(ValueError):
                canonical.docker_host()

    def test_direct_remote_host_environment_is_rejected(self):
        responses = ["local", json.dumps([{"Endpoints": {"docker": {"Host": "unix:///local.sock"}}}])]
        with patch.object(canonical.shutil, "which", return_value="/usr/bin/docker"), \
                patch.object(canonical, "capture", side_effect=responses), \
                patch.dict(canonical.os.environ, {"DOCKER_HOST": "tcp://remote:2375"}, clear=True):
            with self.assertRaises(ValueError):
                canonical.docker_host()

    def test_replay_command_uses_nonroot_isolated_readonly_inputs(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(canonical.os, "getuid", return_value=501), \
                patch.object(canonical.os, "getgid", return_value=20):
            folder = Path(temp).resolve()
            command = canonical.container_command(IMAGE, folder / "output", "replay",
                payload=folder / "payload", snapshot=folder / "snapshot", previous=folder / "prior")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--user") + 1], "501:20")
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges", command)
        self.assertIn("-I", command)
        self.assertIn(IMAGE, command)
        self.assertNotIn("--privileged", command)
        mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
        for target in ("/payload", "/repo", "/previous"):
            self.assertTrue(any(f"dst={target},readonly" in value for value in mounts), target)
        self.assertTrue(any("dst=/results" in value and "readonly" not in value for value in mounts))
        self.assertFalse(any(".git" in value or "docker.sock" in value for value in mounts))
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            self.assertIn(f"{key}=1", command)

    def test_container_command_rejects_root_host_account(self):
        with patch.object(canonical.os, "getuid", return_value=0), self.assertRaises(ValueError):
            canonical.container_command(IMAGE, Path("/tmp/output"), "runtime", payload=Path("/tmp/payload"))

    def test_frozen_endpoint_command_cannot_be_redirected_by_environment(self):
        with patch.dict(canonical.os.environ, {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "tcp://remote:2375",
                "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/other-certs", "UNRELATED": "keep"}, clear=True):
            environment = canonical.docker_environment()
        self.assertEqual(environment, {"UNRELATED": "keep"})
        host = {"endpoint": "unix:///frozen.sock"}
        self.assertEqual(canonical.docker_args(["image", "inspect", IMAGE], host),
                         ["docker", "--host", "unix:///frozen.sock", "image", "inspect", IMAGE])


class CanonicalContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.contract_path = self.root / canonical.RUNTIME_DIR / "contract.json"
        for name in (*canonical.CONTRACT_FILES, canonical.RUNTIME_DIR + "/contract.json"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT / name, destination)

    def tearDown(self):
        self.temp.cleanup()

    def replace_bound_json(self, relative, value):
        path = self.root / relative
        path.write_text(json.dumps(value))
        contract = canonical.read_json(self.contract_path)
        contract["files_sha256"][relative] = canonical.digest(path)
        self.contract_path.write_text(json.dumps(contract))

    def test_checked_in_contract_binds_complete_sources_and_fixed_runtime(self):
        _, source, spec = canonical.verify_contract(self.root)
        self.assertEqual(len(source["files_sha256"]), 32)
        self.assertEqual(len(spec["packages"]), 10)
        self.assertEqual(spec["python_version"], "3.12.14")

    def test_omitted_executable_or_source_cannot_hide_behind_a_smaller_manifest(self):
        good = canonical.read_json(self.contract_path)
        bad = copy.deepcopy(good)
        del bad["files_sha256"]["src/ds_m01/canonical_replay.py"]
        self.contract_path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            canonical.verify_contract(self.root)
        self.contract_path.write_text(json.dumps(good))
        relative = canonical.RUNTIME_DIR + "/source_manifest.json"
        source = canonical.read_json(self.root / relative)
        source["files_sha256"].pop("data/raw/eta_trip_outcome.jsonl")
        source["source_file_count"] -= 1
        self.replace_bound_json(relative, source)
        with self.assertRaises(ValueError):
            canonical.verify_contract(self.root)

    def test_rehashed_runtime_document_cannot_change_fixed_protocol(self):
        relative = canonical.RUNTIME_DIR + "/runtime_spec.json"
        good = canonical.read_json(self.root / relative)
        changes = [("packages", dict(good["packages"], numpy="0.0.0")),
                   ("thread_environment", dict(good["thread_environment"], OMP_NUM_THREADS="2")),
                   ("python_version", "3.12.2"), ("platform", "linux/arm64"),
                   ("base_image", "docker.io/library/python:latest")]
        for field, value in changes:
            bad = copy.deepcopy(good)
            bad[field] = value
            self.replace_bound_json(relative, bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                canonical.verify_contract(self.root)

    def test_modified_executable_payload_fails_hash_verification(self):
        path = self.root / canonical.RUNTIME_DIR / "inner_replay.py"
        path.write_bytes(path.read_bytes() + b"\n# altered payload\n")
        with self.assertRaises(ValueError):
            canonical.verify_contract(self.root)

    def test_additive_repository_tree_is_allowed_but_changed_target_bytes_are_not(self):
        target = self.root / "data/raw/eta_trip_outcome.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("frozen outcome\n")
        receipt = self.root / "reports/m3b2/run_receipt.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}\n")
        source = {"reference_tree": canonical.REFERENCE_TREE,
                  "files_sha256": {"data/raw/eta_trip_outcome.jsonl": canonical.digest(target),
                      "reports/m3b2/run_receipt.json": canonical.digest(receipt)}}
        answers = [str(self.root), "", "1" * 40, "2" * 40]
        with patch.object(canonical, "capture", side_effect=answers):
            verified = canonical.verify_repository(self.root, source)
        self.assertEqual(verified["tree"], "2" * 40)
        self.assertNotEqual(verified["tree"], source["reference_tree"])
        target.write_text("changed outcome\n")
        with patch.object(canonical, "capture", side_effect=answers), self.assertRaises(ValueError):
            canonical.verify_repository(self.root, source)

    def test_dirty_repository_stops_before_manifest_reads(self):
        with patch.object(canonical, "capture", side_effect=[str(self.root), "?? preserve-me.txt"]):
            with self.assertRaisesRegex(ValueError, "not clean"):
                canonical.verify_repository(self.root, {})


class CanonicalReceiptTests(unittest.TestCase):
    """Exercise received-byte validation independently of Docker subprocess mocks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.payload = PROJECT / "reproducibility/m3b2"
        self.spec = canonical.read_json(self.payload / "runtime_spec.json")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, mode="runtime"):
        receipt = {
            "protocol": "m3b2-canonical-v1", "mode": mode, "status": "passed",
            "publication_approved": False, "training_attempted": mode == "replay",
            "runtime_spec_sha256": canonical.digest(self.payload / "runtime_spec.json"),
            "runtime": {
                "python_version": self.spec["python_version"],
                "package_versions": copy.deepcopy(self.spec["packages"]),
                "thread_environment": copy.deepcopy(self.spec["thread_environment"]),
                "system": "Linux", "machine": "x86_64", "isolated_interpreter": True,
                "euid": 501, "binary_imports_verified": True, "pip_check": {"exit_code": 0},
            },
        }
        if mode == "replay":
            original = PROJECT / "reports/m3b2"
            (self.folder / "replay").mkdir()
            for name in ("models.json", "predictions.jsonl", "training_rows.jsonl", "summary.json", "run_receipt.json"):
                shutil.copy2(original / name, self.folder / "replay" / name)
            receipt.update(
                source_manifest_sha256=canonical.digest(self.payload / "source_manifest.json"),
                retained_receipt_sha256=canonical.digest(original / "run_receipt.json"),
                model_count=24,
                replay_result={
                    "status": "passed", "numerical_models_equal": True,
                    "numerical_predictions_equal": True, "retained_inference_equal": True,
                    "prediction_rows": 10326, "validation_opportunities": 5163,
                    "package_versions": copy.deepcopy(canonical.NUMERICAL_PINS),
                    "model_artifact_byte_identical": True, "prediction_artifact_byte_identical": True,
                    "numerical_differences": {"models": zero_differences(33580),
                        "predictions": zero_differences(60546), "retained_inference": zero_differences(60546)},
                    "exact_core_artifacts": {"training_rows.jsonl": True, "summary.json": True},
                    "tolerances": {"absolute_float": 1e-6, "relative_float": 1e-9, "eta_microseconds": 50},
                },
            )
        self.save(receipt)
        return receipt

    def save(self, receipt):
        (self.folder / "inner_receipt.json").write_text(json.dumps(receipt))

    def verify(self, mode="runtime"):
        return canonical.require_inner_result(self.folder, mode, payload=self.payload,
            reference=PROJECT if mode == "replay" else None)

    def test_complete_fixed_runtime_receipt_is_accepted(self):
        receipt = self.fixture()
        self.assertEqual(self.verify(), receipt)

    def test_runtime_receipt_cannot_hide_wrong_python_pins_threads_or_execution_mode(self):
        good = self.fixture()
        mutations = [
            ("runtime_spec_sha256", "0" * 64), ("status", "failed"),
            ("protocol", "other"), ("mode", "replay"), ("publication_approved", True),
        ]
        for field, value in mutations:
            bad = copy.deepcopy(good)
            bad[field] = value
            self.save(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
        runtime_mutations = [
            ("python_version", "3.12.2"), ("package_versions", dict(self.spec["packages"], numpy="0.0.0")),
            ("thread_environment", dict(self.spec["thread_environment"], OMP_NUM_THREADS="2")),
            ("system", "Darwin"), ("machine", "arm64"), ("isolated_interpreter", False),
            ("euid", 0), ("binary_imports_verified", False), ("pip_check", {"exit_code": 1}),
        ]
        for field, value in runtime_mutations:
            bad = copy.deepcopy(good)
            bad["runtime"][field] = value
            self.save(bad)
            with self.subTest(runtime_field=field), self.assertRaises(ValueError):
                self.verify()

    def test_retained_artifacts_satisfy_complete_receipt_contract(self):
        receipt = self.fixture("replay")
        self.assertEqual(self.verify("replay"), receipt)

    def test_each_of_five_outputs_is_required_even_with_passing_receipt(self):
        self.fixture("replay")
        for name in ("models.json", "predictions.jsonl", "training_rows.jsonl", "summary.json", "run_receipt.json"):
            original = self.folder / "replay" / name
            withheld = self.folder / name
            original.rename(withheld)
            try:
                with self.subTest(file=name), self.assertRaises((ValueError, FileNotFoundError)):
                    self.verify("replay")
            finally:
                withheld.rename(original)

    def test_full_replay_rejects_wrong_source_binding_counts_or_relaxed_tolerances(self):
        good = self.fixture("replay")
        changes = [
            ("training_attempted", False), ("model_count", 23),
            ("source_manifest_sha256", "0" * 64), ("retained_receipt_sha256", "0" * 64),
            ("publication_approved", True),
        ]
        for field, value in changes:
            bad = copy.deepcopy(good)
            bad[field] = value
            self.save(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify("replay")
        replay_changes = [
            ("status", "failed"), ("numerical_models_equal", False),
            ("numerical_predictions_equal", False), ("retained_inference_equal", False),
            ("prediction_rows", 10325), ("validation_opportunities", 5162),
            ("exact_core_artifacts", {"training_rows.jsonl": True, "summary.json": False}),
            ("tolerances", {"absolute_float": 1e-5, "relative_float": 1e-9, "eta_microseconds": 50}),
            ("tolerances", {"absolute_float": 1e-6, "relative_float": 1e-8, "eta_microseconds": 50}),
            ("tolerances", {"absolute_float": 1e-6, "relative_float": 1e-9, "eta_microseconds": 51}),
            ("numerical_differences", {}),
            ("numerical_differences", dict(good["replay_result"]["numerical_differences"],
                predictions=zero_differences(60545))),
        ]
        for field, value in replay_changes:
            bad = copy.deepcopy(good)
            bad["replay_result"][field] = value
            self.save(bad)
            with self.subTest(replay_field=field, value=value), self.assertRaises(ValueError):
                self.verify("replay")

    def test_modified_model_bytes_are_detected_against_output_receipt(self):
        self.fixture("replay")
        path = self.folder / "replay/models.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.verify("replay")

    def test_run_receipt_cannot_substitute_different_inputs_or_omit_output_hashes(self):
        self.fixture("replay")
        path = self.folder / "replay/run_receipt.json"
        good = canonical.read_json(path)
        for field in ("input_sha256", "code_sha256", "settings_sha256", "package_versions", "output_sha256"):
            bad = copy.deepcopy(good)
            bad[field] = {} if isinstance(bad[field], dict) else "0" * 64
            path.write_text(json.dumps(bad))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify("replay")


class CanonicalExecutionTests(unittest.TestCase):
    """Mocked child processes prove stop/preserve behavior, not model replay."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.root = self.folder / "repo"
        self.root.mkdir()
        self.output = self.folder / "results"
        payload = self.root / canonical.RUNTIME_DIR
        payload.mkdir(parents=True)
        hashes = {}
        for name in canonical.PAYLOAD_FILES:
            shutil.copy2(PROJECT / canonical.RUNTIME_DIR / name, payload / name)
            hashes[canonical.RUNTIME_DIR + "/" + name] = canonical.digest(payload / name)
        (payload / "contract.json").write_text("{}\n")
        (self.root / "input.bin").write_text("frozen input")
        self.contract = {"files_sha256": hashes}
        self.source = {"files_sha256": {"input.bin": canonical.digest(self.root / "input.bin")}}
        self.ready = {"protocol": canonical.PROTOCOL, "status": "passed_preflight_only",
            "publication_approved": False,
            "repository": {"head_commit": "1" * 40, "tree": "2" * 40, "root": str(self.root)},
            "docker_host": {"endpoint": "unix:///test.sock"}, "docker_image_id": IMAGE,
            "contract_sha256": canonical.digest(payload / "contract.json")}
        self.image = {"Id": IMAGE, "Os": "linux", "Architecture": "amd64"}
        self.child_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def verified_inner(self, folder, mode, **kwargs):
        return {"pair_comparison": {
            "status": "passed", "comparison_uses_original_binding_and_tolerances": True,
            "exact_core_artifacts": {"training_rows.jsonl": True, "summary.json": True},
            "model_artifact_byte_identical": True, "prediction_artifact_byte_identical": True,
            "numerical_differences": {"models": zero_differences(33580), "predictions": zero_differences(60546)},
        }}

    def child(self, command, log):
        name = Path(log).stem
        self.child_calls.append(name)
        Path(log).write_text("mocked child process control-flow fixture\n")
        folder = self.output / name
        (folder / "inner_receipt.json").write_text("{}\n")
        if name.startswith("run_"):
            replay = folder / "replay"
            replay.mkdir()
            for output in canonical.REPLAY_FILES:
                (replay / output).write_text("{}\n")
        return 0

    def mocked_processes(self, child=None, verifier=None):
        stack = ExitStack()
        stack.enter_context(patch.object(canonical, "preflight", return_value=self.ready))
        stack.enter_context(patch.object(canonical, "verify_contract", return_value=(self.contract, self.source, {})))
        stack.enter_context(patch.object(canonical, "verify_repository", return_value=self.ready["repository"]))
        stack.enter_context(patch.object(canonical, "inspect_image", return_value=self.image))
        stack.enter_context(patch.object(canonical, "require_inner_result", side_effect=verifier or self.verified_inner))
        stack.enter_context(patch.object(canonical, "run_logged", side_effect=child or self.child))
        stack.enter_context(patch.object(canonical.os, "getuid", return_value=501))
        stack.enter_context(patch.object(canonical.os, "getgid", return_value=20))
        return stack

    def test_existing_output_is_preserved_without_starting_a_child(self):
        self.output.mkdir()
        (self.output / "keep").write_text("existing work")
        with self.mocked_processes(), self.assertRaises(FileExistsError):
            canonical.run_replay(self.root, self.output, IMAGE)
        self.assertEqual((self.output / "keep").read_text(), "existing work")
        self.assertEqual(self.child_calls, [])

    def test_runtime_failure_records_failure_without_starting_training(self):
        def fail(command, log):
            self.child(command, log)
            return 19
        with self.mocked_processes(child=fail), self.assertRaisesRegex(RuntimeError, "19"):
            canonical.run_replay(self.root, self.output, IMAGE)
        receipt = canonical.read_json(self.output / "execution_receipt.json")
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["training_attempted"])
        self.assertEqual(self.child_calls, ["runtime_probe"])
        self.assertFalse((self.output / "run_01").exists())

    def test_first_replay_failure_preserves_partial_outputs_and_never_starts_second(self):
        def fail(command, log):
            self.child(command, log)
            if Path(log).stem == "run_01":
                return 17
            return 0
        with self.mocked_processes(child=fail), self.assertRaisesRegex(RuntimeError, "run 1 failed"):
            canonical.run_replay(self.root, self.output, IMAGE)
        receipt = canonical.read_json(self.output / "execution_receipt.json")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["runs"][0]["process_exit_code"], 17)
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01"])
        self.assertFalse((self.output / "run_02").exists())
        self.assertTrue((self.output / "run_01/replay/models.json").is_file())
        archive = canonical.archive(self.output)
        with zipfile.ZipFile(archive["zip"]) as zipped:
            self.assertIn("execution_receipt.json", zipped.namelist())
            self.assertIn("run_01/replay/models.json", zipped.namelist())
            self.assertFalse(any(name.startswith(("source_snapshot/", "payload_snapshot/")) for name in zipped.namelist()))
        with self.assertRaises(FileExistsError):
            canonical.archive(self.output)

    def test_changed_staged_input_after_first_run_stops_before_second(self):
        def mutate(command, log):
            self.child(command, log)
            if Path(log).stem == "run_01":
                path = self.output / "source_snapshot/input.bin"
                path.chmod(0o644)
                path.write_text("changed after first child")
            return 0
        with self.mocked_processes(child=mutate), self.assertRaises(ValueError):
            canonical.run_replay(self.root, self.output, IMAGE)
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01"])
        self.assertFalse((self.output / "run_02").exists())
        self.assertEqual(canonical.read_json(self.output / "execution_receipt.json")["status"], "failed")

    def test_first_run_output_change_is_detected_before_second(self):
        def mutate_after_verification(folder, mode, **kwargs):
            result = self.verified_inner(folder, mode, **kwargs)
            if Path(folder).name == "run_01":
                (Path(folder) / "replay/models.json").write_text("changed after hash capture")
            return result
        with self.mocked_processes(verifier=mutate_after_verification), self.assertRaises(ValueError):
            canonical.run_replay(self.root, self.output, IMAGE)
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01"])
        self.assertFalse((self.output / "run_02").exists())

    def test_two_passing_child_codes_do_not_replace_required_pair_comparison(self):
        def no_pair(folder, mode, **kwargs):
            return {} if Path(folder).name == "run_02" else self.verified_inner(folder, mode, **kwargs)
        with self.mocked_processes(verifier=no_pair), self.assertRaisesRegex(ValueError, "pair comparison"):
            canonical.run_replay(self.root, self.output, IMAGE)
        receipt = canonical.read_json(self.output / "execution_receipt.json")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01", "run_02"])
        self.assertTrue((self.output / "run_02/replay/models.json").exists())

    def test_completed_control_flow_reuses_one_image_and_rejects_repeat_in_place(self):
        with self.mocked_processes(), patch.object(canonical, "container_command", wraps=canonical.container_command) as command:
            result = canonical.run_replay(self.root, self.output, IMAGE)
            self.assertEqual(result["status"], "passed_two_container_replays_and_pair_comparison")
            self.assertEqual([call.args[0] for call in command.call_args_list], [IMAGE, IMAGE, IMAGE])
            self.assertEqual([Path(call.args[1]).name for call in command.call_args_list], ["runtime_probe", "run_01", "run_02"])
            self.assertEqual(command.call_args_list[-1].kwargs["previous"], self.output / "run_01/replay")
            with self.assertRaises(FileExistsError):
                canonical.run_replay(self.root, self.output, IMAGE)
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01", "run_02"])

    def test_pair_gate_requires_binding_statistics_and_truthful_byte_identity(self):
        first, second = self.folder / "first", self.folder / "second"
        for folder in (first, second):
            folder.mkdir()
            for name in canonical.REPLAY_FILES:
                (folder / name).write_text("same fixture bytes\n")
        good = self.verified_inner(second, "replay")
        canonical.require_pair(good, first, second)
        for field, value in (("comparison_uses_original_binding_and_tolerances", False),
                ("numerical_differences", {}), ("model_artifact_byte_identical", False),
                ("prediction_artifact_byte_identical", False)):
            changed = copy.deepcopy(good)
            changed["pair_comparison"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                canonical.require_pair(changed, first, second)
        (second / "summary.json").write_text("different exact artifact\n")
        with self.assertRaises(ValueError):
            canonical.require_pair(good, first, second)

    def test_evidence_validator_binds_identity_logs_outputs_and_both_run_receipts(self):
        with self.mocked_processes():
            canonical.run_replay(self.root, self.output, IMAGE)
            verified = canonical.validate_evidence(self.root, self.output, IMAGE)
            self.assertFalse(verified["docker_executed_by_verifier"])
            self.assertFalse(verified["training_executed_by_verifier"])
            with self.assertRaises(ValueError):
                canonical.validate_evidence(self.root, self.output, OTHER_IMAGE)
            for name in ("runtime_probe.log", "run_01.log", "run_02.log",
                    "runtime_probe/inner_receipt.json", "run_01/inner_receipt.json",
                    "run_02/inner_receipt.json", "run_01/replay/models.json",
                    "run_02/replay/run_receipt.json"):
                path = self.output / name
                original = path.read_bytes()
                path.write_bytes(original + b"altered\n")
                try:
                    with self.subTest(file=name), self.assertRaises(ValueError):
                        canonical.validate_evidence(self.root, self.output, IMAGE)
                finally:
                    path.write_bytes(original)
            receipt_path = self.output / "execution_receipt.json"
            good = canonical.read_json(receipt_path)
            for field, value in (("runs", good["runs"][:1]), ("runtime_probe_exit_code", True),
                    ("publication_approved", True), ("payload_sha256", {}), ("log_sha256", {})):
                changed = copy.deepcopy(good)
                changed[field] = value
                receipt_path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    canonical.validate_evidence(self.root, self.output, IMAGE)
            receipt_path.write_text(json.dumps(good))
            self.assertEqual(canonical.validate_evidence(self.root, self.output, IMAGE)["runs"], 2)
        self.assertEqual(self.child_calls, ["runtime_probe", "run_01", "run_02"])


if __name__ == "__main__":
    unittest.main()
