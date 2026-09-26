# -*- coding: utf-8 -*-
"""Offline tests for the frozen-software loader and the blinded review export.

No real keys or paid calls. The provider transport is mocked; the geometry
backend runs on a synthetic box model only. `check_in_process` spawns one
Python subprocess that executes the frozen copy of the engine offline.
"""
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "package" if (HERE / "package").is_dir() else HERE.parent
sys.path.insert(0, str(PACKAGE))
import experiment as engine
import frozen_runtime
import providers
import review_export

IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
# Minimal model accepted by resources/shared/minimal_v1: one 2x4x6 mm box in the world frame.
BOX_MODEL = {"schema_version": "0.2.0", "model_id": "OFFLINE_SYNTHETIC_BOX",
             "coordinate_frames": [{"id": "world", "length_unit": "mm", "parent_frame_id": None,
                                    "transform_to_parent": IDENTITY}],
             "entities": [{"id": "box", "label": "SYNTHETIC_LABEL_SENTINEL",
                           "geometry": {"primitives": [{"kind": "box", "parameters": {"length_unit": "mm", "extents": [2, 4, 6]}}]},
                           "pose": {"frame_id": "world", "transform": IDENTITY}}]}


def response(text):
    raw = {"id": "resp_fixture", "model": "gpt-6-astra", "status": "completed",
           "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
           "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    return json.dumps(raw).encode("utf-8"), {"http_status": 200, "request_id": "offline-request-id"}


def prepare_experiment(root, name="exp"):
    """Freeze the package configuration into root/name with an offline placeholder image."""
    image = root / "image.png"
    if not image.exists():
        image.write_bytes(b"OFFLINE_IMAGE_NOT_A_REAL_DRAWING")
    cfg = engine.read_json(PACKAGE / "config.json")
    cfg.update({"provider": "openai", "model": "gpt-6-astra", "repetitions": 1,
                "max_output_tokens": 4096, "auto_continue_limit": 1})
    for key in ("knowledge_source", "common_instruction", "output_schema", "geometry_contract", "backend_dir"):
        cfg[key] = str(PACKAGE / cfg[key])
    for case in cfg["cases"]:
        for key in ("scope", "text", "provenance"):
            case[key] = str(PACKAGE / case[key])
        case["images"] = [str(image)]
    config = root / (name + "_config.json")
    engine.write_json(config, cfg)
    exp = root / name
    engine.prepare(config, exp)
    return exp


class FrozenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline_frozen_", dir=HERE)
        self.root = Path(self.temp.name).resolve()
        self.no_paid = mock.patch.object(providers, "send_request", side_effect=AssertionError("Paid transport forbidden"))
        self.paid = self.no_paid.start()
        self.exp = prepare_experiment(self.root)
        self.software = self.exp / "software"

    def tearDown(self):
        self.paid.assert_not_called()
        self.no_paid.stop()
        self.temp.cleanup()

    def test_verified_software_accepts_the_frozen_tree_and_rejects_every_tampering(self):
        self.assertEqual(frozen_runtime.verified_software(self.exp), self.software.resolve())
        frozen = self.software / "providers.py"
        original = frozen.read_bytes()
        frozen.write_bytes(original + b"\n# tampered\n")
        with self.assertRaisesRegex(ValueError, "Frozen files changed: software/providers.py"):
            frozen_runtime.verified_software(self.exp)
        frozen.write_bytes(original)
        extra = self.software / "helper.py"
        extra.write_text("SENTINEL = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrecorded software"):
            frozen_runtime.verified_software(self.exp)
        extra.unlink()
        manifest = self.exp / "manifest.json"
        saved = manifest.read_bytes()
        manifest.write_bytes(saved.replace(b"\n", b"\n", 1) + b"\n")
        with self.assertRaisesRegex(ValueError, "Manifest changed"):
            frozen_runtime.verified_software(self.exp)
        manifest.write_bytes(saved)
        self.assertEqual(frozen_runtime.verified_software(self.exp), self.software.resolve())

    def test_load_in_worker_executes_the_frozen_source_not_the_live_modules(self):
        live = {name: sys.modules[name] for name in frozen_runtime.MODULES}
        try:
            frozen = frozen_runtime.load_in_worker(self.exp)
            self.assertIsNot(frozen, engine)
            self.assertEqual(Path(frozen.__file__).resolve(), (self.software / "experiment.py").resolve())
            self.assertIs(sys.modules["experiment"], frozen)
            self.assertIs(frozen.providers, sys.modules["providers"])
            self.assertEqual(Path(frozen.providers.__file__).resolve(), (self.software / "providers.py").resolve())
            self.assertEqual(frozen.VERSION, engine.VERSION)
            self.assertTrue(frozen.verify_frozen(self.exp)["passed"])
        finally:
            for name, module in live.items():
                sys.modules[name] = module
        self.assertIs(sys.modules["experiment"], engine)

    def test_check_in_process_runs_the_frozen_engine_in_a_child_process(self):
        result = frozen_runtime.check_in_process(self.exp)
        self.assertTrue(result["passed"])
        self.assertEqual(result["case_count"], len(engine.read_json(self.exp / "config.json")["cases"]))
        frozen = self.software / "experiment.py"
        frozen.write_bytes(frozen.read_bytes() + b"\n# tampered\n")
        with self.assertRaisesRegex(ValueError, "Frozen files changed: software/experiment.py"):
            frozen_runtime.check_in_process(self.exp)
        self.assertIs(sys.modules["experiment"], engine)


class ReviewExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline_review_", dir=HERE)
        self.root = Path(self.temp.name).resolve()
        self.no_paid = mock.patch.object(providers, "send_request", side_effect=AssertionError("Paid transport forbidden"))
        self.paid = self.no_paid.start()
        self.exp = prepare_experiment(self.root)
        self.backend = self.exp / "software/backend"
        self.schema = self.exp / "frozen/shared/common_output.schema.json"
        self.runs = [row["run_id"] for row in engine.read_json(self.exp / "schedule.json")["runs"]]
        self.assertEqual(len(self.runs), 3)
        transport = mock.Mock(return_value=response(json.dumps(BOX_MODEL)))
        with contextlib.redirect_stdout(io.StringIO()):
            engine.run_batch(self.exp, transport=transport, max_new_calls=1, test_mode=True)
        self.completed = self.exp / "runs" / self.runs[0]
        self.assertEqual(engine.read_json(self.completed / "result.json")["status"], "completed")
        self.model_sha = engine.sha(self.completed / "model_input.json")
        refused = self.exp / "runs" / self.runs[1]
        engine.write_json(refused / "attempt.json", {"offline_fixture_only": True})
        engine.write_json(refused / "result.json", {"status": "refused"})

    def tearDown(self):
        self.paid.assert_not_called()
        self.no_paid.stop()
        self.temp.cleanup()

    def export(self):
        return review_export.export_review(self.exp, self.backend, self.schema)

    def test_export_blinds_every_slot_and_keeps_the_key_administrator_only(self):
        report = self.export()
        self.assertEqual((report["status"], report["review_slot_count"], report["available_geometry_count"]), ("completed", 3, 1))
        review = self.exp / "review"
        self.assertEqual(sorted(p.name for p in review.iterdir()), ["S001", "S002", "S003", "index.html"])
        key = engine.read_json(self.exp / "admin/review_key.json")
        self.assertEqual({slot["review_id"] for slot in key["slots"]}, {"S001", "S002", "S003"})
        self.assertEqual({slot["run_id"] for slot in key["slots"]}, set(self.runs))
        schedule = {row["run_id"]: row for row in engine.read_json(self.exp / "schedule.json")["runs"]}
        by_run = {slot["run_id"]: slot for slot in key["slots"]}
        for run_id, row in schedule.items():
            self.assertEqual((by_run[run_id]["condition"], by_run[run_id]["case_id"], by_run[run_id]["repetition"]),
                             (row["condition"], row["case_id"], row["repetition"]))
        statuses = {by_run[run_id]["review_id"]: engine.read_json(review / by_run[run_id]["review_id"] / "run_summary.json")
                    for run_id in self.runs}
        completed, refused, missing = (statuses[by_run[run_id]["review_id"]] for run_id in self.runs)
        self.assertEqual((completed["execution_status"], completed["conversion_status"], completed["review_export_status"]),
                         ("completed", "ok", "available"))
        self.assertEqual(sorted(completed["files"]), ["model.dxf", "model.glb", "model.obj"])
        self.assertEqual((refused["execution_status"], refused["conversion_status"], refused["files"]), ("refused", "not_attempted", []))
        self.assertEqual((missing["execution_status"], missing["conversion_status"], missing["files"]), ("missing_result", "not_attempted", []))
        for summary in statuses.values():
            self.assertEqual(set(summary), {"review_id", "execution_status", "parse_status", "conversion_status", "review_export_status", "files"})
        # Run identifiers, the case id, the model's own labels, and the condition letters keyed by run must not reach the review tree.
        identifying = [run_id.encode() for run_id in self.runs] + [b"SYNTHETIC_DEMO", b"SYNTHETIC_LABEL_SENTINEL", b"OFFLINE_SYNTHETIC_BOX", b"run_id", b'"condition"']
        for file in review.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                for token in identifying:
                    self.assertNotIn(token, data, str(file))
        slot_dir = review / by_run[self.runs[0]]["review_id"]
        glb = (slot_dir / "model.glb").read_bytes()
        magic, version, total = struct.unpack_from("<III", glb)
        self.assertEqual((magic, version, total), (0x46546C67, 2, len(glb)))
        length, chunk_type = struct.unpack_from("<II", glb, 12)
        self.assertEqual(chunk_type, 0x4E4F534A)
        gltf = json.loads(glb[20:20 + length])
        self.assertEqual(gltf["asset"]["generator"], "Anonymous geometry review export")
        self.assertNotIn("name", json.dumps(gltf))
        obj_lines = [line.split(None, 1)[0] for line in (slot_dir / "model.obj").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
        self.assertTrue(set(obj_lines) <= {"o", "v", "vt", "vn", "f", "s"})
        self.assertIn("o Object0001", (slot_dir / "model.obj").read_text(encoding="utf-8"))
        self.assertEqual(engine.sha(self.completed / "model_input.json"), self.model_sha)
        index = (review / "index.html").read_text(encoding="utf-8")
        for review_id in ("S001", "S002", "S003"):
            self.assertIn(review_id, index)
        self.assertNotIn("A_prompt", index)

    def test_second_export_reuses_conversions_keeps_the_key_and_archives_earlier_copies(self):
        first = self.export()
        key = (self.exp / "admin/review_key.json").read_bytes()
        review_id = next(slot["review_id"] for slot in json.loads(key)["slots"] if slot["run_id"] == self.runs[0])
        before = (self.exp / "review" / review_id / "model.glb").read_bytes()
        with mock.patch.object(review_export.subprocess, "run", side_effect=AssertionError("Backend must not run again")):
            second = self.export()
        self.assertEqual([s["review_id"] for s in first["summaries"]], [s["review_id"] for s in second["summaries"]])
        self.assertEqual((self.exp / "admin/review_key.json").read_bytes(), key)
        audit = engine.read_json(self.exp / "admin/review_export_audit.json")
        receipts = [row["receipt"] for row in audit["runs"] if "receipt" in row]
        self.assertEqual([r["reused"] for r in receipts], [True])
        for slot in json.loads(key)["slots"]:
            self.assertTrue((self.exp / "admin/review_history" / slot["review_id"] / "version_001/run_summary.json").is_file())
        self.assertEqual((self.exp / "review" / review_id / "model.glb").read_bytes(), before)
        self.assertEqual(engine.sha(self.completed / "model_input.json"), self.model_sha)

    def test_export_refuses_a_review_key_that_no_longer_matches_the_schedule(self):
        self.export()
        key_path = self.exp / "admin/review_key.json"
        key = engine.read_json(key_path)
        engine.write_json(key_path, {**key, "slots": key["slots"][:-1]})
        with self.assertRaisesRegex(ValueError, "does not match this schedule"):
            self.export()
        changed = json.loads(json.dumps(key))
        changed["slots"][0]["condition"] = "Z"
        engine.write_json(key_path, changed)
        with self.assertRaisesRegex(ValueError, "metadata does not match"):
            self.export()
        engine.write_json(key_path, key)
        self.assertEqual(self.export()["status"], "completed")

    def test_invalid_model_json_is_reported_without_touching_the_original(self):
        broken = self.exp / "runs" / self.runs[2]
        engine.write_json(broken / "attempt.json", {"offline_fixture_only": True})
        engine.write_json(broken / "result.json", {"status": "completed"})
        (broken / "model_input.json").write_text('{"schema_version": "0.2.0", "duplicate": 1, "duplicate": 2}', encoding="utf-8")
        original = (broken / "model_input.json").read_bytes()
        report = self.export()
        summary = next(s for s in report["summaries"] if s["execution_status"] == "completed" and s["parse_status"] == "invalid_json")
        self.assertEqual((summary["conversion_status"], summary["review_export_status"], summary["files"]), ("failed", "no_geometry", []))
        self.assertEqual((broken / "model_input.json").read_bytes(), original)

    def test_missing_backend_or_schema_is_refused_before_any_write(self):
        with self.assertRaises(FileNotFoundError):
            review_export.export_review(self.exp, self.root / "nowhere", self.schema)
        with self.assertRaises(FileNotFoundError):
            review_export.export_review(self.exp, self.backend, self.root / "missing.schema.json")
        self.assertFalse((self.exp / "review").exists())
        self.assertFalse((self.exp / "admin/review_key.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
