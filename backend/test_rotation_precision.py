"""Offline, synthetic geometry checks; never load or reconvert experiment outputs."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import trimesh

from geometry_backend import GeometryInputError, convert_model, rigid_matrix


IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
ROUND_45 = [[1, 0, 0], [0, 0.70711, -0.70711], [0, 0.70711, 0.70711]]
# Numeric-only reproductions of the five audited matrix patterns. These are
# synthetic boxes, IDs and translations, not the original bridge responses.
AUDITED_PATTERNS = {
    "pose_45": ROUND_45,
    "frame_plane": [[-0.47826, 0, 0.87822], [0, -1, 0], [0.87822, 0, 0.47826]],
    "unused_frame": [[0.9045, 0, 0.4265], [0, 1, 0], [-0.4265, 0, 0.9045]],
    "pose_four_decimals": [[1, 0, 0], [0, 0.4786, 0.878], [0, -0.878, 0.4786]],
    "frame_xz": [[0.42756, 0, 0.904], [0, 1, 0], [-0.904, 0, 0.42756]],
}


def transform(rotation=None, translation=(0, 0, 0)):
    result = copy.deepcopy(IDENTITY)
    if rotation is not None:
        for index in range(3):
            result[index][:3] = list(rotation[index])
    for index in range(3):
        result[index][3] = translation[index]
    return result


def frame(identifier="world", rotation=None, translation=(0, 0, 0), unit="mm", parent=None):
    return {"id": identifier, "description": "Synthetic test frame", "length_unit": unit,
            "parent_frame_id": parent, "transform_to_parent": transform(rotation, translation),
            "source_evidence": []}


def box(identifier="box", rotation=None, translation=(0, 0, 0), frame_id="world"):
    return {"id": identifier, "label": "Synthetic box", "kind": "generic",
            "geometry": {"representation": "explicit primitives", "primitives": [
                {"kind": "box", "parameters": {"length_unit": "mm", "extents": [2, 4, 6]}, "source_evidence": []}]},
            "pose": {"frame_id": frame_id, "transform": transform(rotation, translation), "source_evidence": []},
            "source_evidence": []}


def model(entities, frames=None):
    return {"schema_version": "0.1.0", "model_id": "synthetic_precision_only",
            "coordinate_frames": [frame()] if frames is None else frames,
            "entities": entities, "relations": [], "unknowns": []}


class RotationPrecisionTests(unittest.TestCase):
    def conversion(self, value, validate_schema=True, minimal_schema=False):
        scratch = Path(__file__).resolve().parents[2] / "rotation_test_artifacts"
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic_", dir=str(scratch))
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        source = directory / "input.json"
        original = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        source.write_bytes(original)
        schema = Path(__file__).resolve().parents[1] / "resources/shared/common_output.schema.json"
        if minimal_schema:
            schema = schema.parent / "minimal_v1/common_output.schema.json"
        output = directory / "converted"
        report = convert_model(source, output, schema if validate_schema else None)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual((output / "model_input.json").read_bytes(), original)
        return report, output

    def test_rounded_pose_exports_and_records_projection_without_partial_status(self):
        report, output = self.conversion(model([box(rotation=ROUND_45, translation=(5, 6, 7))]))
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["schema_validation"], "passed")
        self.assertEqual(report["entities"][0]["status"], "converted")
        self.assertEqual(report["frame_errors"], [])
        self.assertEqual(report["rotation_correction_count"], 1)
        record = report["rotation_corrections"][0]
        self.assertEqual((record["target"], record["entity_id"], record["frame_id"]), ("entity_pose", "box", "world"))
        self.assertEqual(record["before_transform"], transform(ROUND_45, (5, 6, 7)))
        after = np.asarray(record["after_transform"])
        s = math.sqrt(0.5)
        np.testing.assert_allclose(after[:3, :3], [[1, 0, 0], [0, s, -s], [0, s, s]], atol=1e-14, rtol=0)
        np.testing.assert_array_equal(after[:, 3], [5, 6, 7, 1])
        self.assertAlmostEqual(record["before_metrics"]["orthogonality_max_abs_error"], 0.0000091042, places=12)
        self.assertLess(record["after_metrics"]["orthogonality_max_abs_error"], 1e-14)
        self.assertEqual(record["policy_version"], report["rotation_policy"]["version"])
        np.testing.assert_allclose(report["entities"][0]["bounds_mm"], [[4, 6-5*s, 7-5*s], [6, 6+5*s, 7+5*s]], atol=1e-12)
        scene = trimesh.load(output / "model.glb", force="scene")
        self.assertEqual(len(scene.geometry), 1)
        self.assertEqual(json.loads((output / "conversion_report.json").read_text(encoding="utf-8")), report)

    def test_accepted_rotation_is_unchanged_even_with_existing_small_drift(self):
        for rotation in ([[0, -1, 0], [1, 0, 0], [0, 0, 1]],
                         [[1.0000001, 0, 0], [0, 1, 0], [0, 0, 1]]):
            with self.subTest(rotation=rotation):
                original = transform(rotation, (1, 2, 3))
                before = copy.deepcopy(original)
                result = rigid_matrix(original, 10.0, "synthetic")
                np.testing.assert_array_equal(result[:3, :3], rotation)
                np.testing.assert_array_equal(result[:3, 3], [10, 20, 30])
                self.assertEqual(original, before)
        report, _ = self.conversion(model([box(rotation=[[1.0000001, 0, 0], [0, 1, 0], [0, 0, 1]])]))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["rotation_corrections"], [])

    def test_corrected_parent_frame_is_logged_once_and_propagates_units_and_poses(self):
        frames = [frame("root", ROUND_45, (1, 2, 3), "m"),
                  frame("child", translation=(0, 1, 0), unit="cm", parent="root")]
        value = model([box("one", translation=(1, 0, 0), frame_id="child"),
                       box("two", translation=(2, 0, 0), frame_id="child")], frames)
        report, _ = self.conversion(value)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["mesh_count"], 2)
        self.assertEqual(len(report["rotation_corrections"]), 1)
        record = report["rotation_corrections"][0]
        self.assertEqual((record["target"], record["frame_id"], record["entity_id"]), ("frame", "root", None))
        self.assertEqual(record["translation_to_mm_factor"], 1000)
        np.testing.assert_array_equal(np.asarray(record["after_transform"])[:3, 3], [1, 2, 3])
        s = math.sqrt(0.5)
        for row, x in zip(report["entities"], (1010, 1020)):
            np.testing.assert_allclose(row["bounds_mm"], [[x-1, 2000+995*s, 3000+995*s],
                                                         [x+1, 2000+1005*s, 3000+1005*s]], atol=1e-10)

    def test_large_scale_shear_reflection_singular_and_overflow_are_rejected(self):
        bad = {
            "scale": [[1.01, 0, 0], [0, 1, 0], [0, 0, 1]],
            "shear": [[1, 0.01, 0], [0, 1, 0], [0, 0, 1]],
            "reflection": [[-1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "singular": [[0, 0, 0], [0, 1, 0], [0, 0, 1]],
            "determinant_gate_only": [[1.00004, 0, 0], [0, 1.00004, 0], [0, 0, 1.00004]],
            "orthogonality_gate_only": [[1, 0.0001001, 0], [0, 1, 0], [0, 0, 1]],
            "overflow": [[1e300, 0, 0], [0, 1e300, 0], [0, 0, 1e300]],
        }
        report, _ = self.conversion(model([box()] + [box(name, rotation) for name, rotation in bad.items()]))
        self.assertEqual(report["status"], "partial", report)
        self.assertEqual(report["mesh_count"], 1)
        for row in report["entities"][1:]:
            self.assertEqual(row["status"], "skipped", row)
            self.assertTrue(row["errors"], row)
        self.assertEqual(report["rotation_corrections"], [])

    def test_threshold_inside_is_corrected_but_outside_is_rejected(self):
        inside = [[1, 0.0000999, 0], [0, 1, 0], [0, 0, 1]]
        corrected = rigid_matrix(transform(inside), 1.0, "synthetic")
        np.testing.assert_allclose(corrected[:3, :3].T @ corrected[:3, :3], np.eye(3), atol=1e-14, rtol=0)
        self.assertGreater(np.linalg.det(corrected[:3, :3]), 0)
        outside = [[1, 0.0001001, 0], [0, 1, 0], [0, 0, 1]]
        with self.assertRaises(GeometryInputError):
            rigid_matrix(transform(outside), 1.0, "synthetic")

    def test_svd_failure_is_a_local_entity_error(self):
        with patch("geometry_backend.np.linalg.svd", side_effect=np.linalg.LinAlgError("synthetic SVD failure")):
            report, _ = self.conversion(model([box(), box("rounded", ROUND_45)]))
        self.assertEqual(report["status"], "partial", report)
        self.assertEqual(report["mesh_count"], 1)
        self.assertEqual(report["global_errors"], [])
        self.assertEqual(report["entities"][1]["status"], "skipped")

    def test_optional_metadata_cannot_block_geometry_conversion(self):
        value = model([box()])
        value.pop("relations")
        value.pop("unknowns")
        value["coordinate_frames"][0].pop("source_evidence")
        item = value["entities"][0]
        item.pop("source_evidence")
        item["pose"].pop("source_evidence")
        item["geometry"]["primitives"][0].pop("source_evidence")
        for optional in (None, 17, "free text", {}, [None, "text", {"field": []}]):
            with self.subTest(optional=optional):
                value["unknowns"] = optional
                report, output = self.conversion(value, validate_schema=False)
                self.assertEqual(report["status"], "ok", report)
                self.assertEqual(report["mesh_count"], 1)
                diagnostic = json.loads((output / "unknown_review.json").read_text(encoding="utf-8"))
                if not isinstance(optional, list):
                    self.assertEqual(diagnostic["input_status"], "ignored_non_array")

    def test_actual_minimal_schema_without_evidence_and_with_arbitrary_metadata(self):
        value = model([box(rotation=ROUND_45)])
        value["schema_version"] = "0.2.0"
        value.pop("relations")
        value.pop("unknowns")
        value["metadata"] = {"arbitrary": [None, 17, {"nested": True}]}
        value["coordinate_frames"][0].pop("source_evidence")
        value["coordinate_frames"][0].pop("description")
        item = value["entities"][0]
        for field in ("source_evidence", "label", "kind"):
            item.pop(field)
        item["pose"].pop("source_evidence")
        item["geometry"].pop("representation")
        item["geometry"]["primitives"][0].pop("source_evidence")
        item["arbitrary_metadata"] = {"nested": ["test", None, 123]}
        report, _ = self.conversion(value, minimal_schema=True)
        self.assertEqual(report["schema_validation"], "passed", report)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["rotation_correction_count"], 1)
        value["unknowns"] = None
        report, _ = self.conversion(value, minimal_schema=True)
        self.assertEqual(report["status"], "ok", report)

    def test_failed_world_composition_does_not_log_repeated_frame_corrections(self):
        frames = [frame(), frame("large", translation=(1e308, 0, 0)),
                  frame("overflow_child", ROUND_45, (1e308, 0, 0), parent="large")]
        items = [box(), box("one", frame_id="overflow_child"), box("two", frame_id="overflow_child")]
        report, _ = self.conversion(model(items, frames))
        self.assertEqual(report["status"], "partial", report)
        self.assertEqual(report["mesh_count"], 1)
        self.assertEqual(len(report["frame_errors"]), 1)
        self.assertEqual(report["rotation_corrections"], [])

    def test_numeric_audit_patterns_convert_31_synthetic_boxes_and_14_distinct_matrices(self):
        frames = [frame(), frame("frame_plane", AUDITED_PATTERNS["frame_plane"]),
                  frame("frame_xz", AUDITED_PATTERNS["frame_xz"]),
                  frame("unused_frame", AUDITED_PATTERNS["unused_frame"])]
        items = [box("a"+str(i), AUDITED_PATTERNS["pose_45"]) for i in range(4)]
        items += [box("b"+str(i), frame_id="frame_plane") for i in range(12)]
        items += [box("c"+str(i), AUDITED_PATTERNS["pose_four_decimals"]) for i in range(7)]
        items += [box("d"+str(i), frame_id="frame_xz") for i in range(8)]
        report, _ = self.conversion(model(items, frames))
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["mesh_count"], 31)
        self.assertEqual(report["converted_entity_count"], 31)
        self.assertEqual(report["rotation_correction_count"], 14)
        self.assertEqual(report["frame_errors"], [])
        self.assertEqual(sum(r["target"] == "frame" for r in report["rotation_corrections"]), 3)


if __name__ == "__main__":
    unittest.main()
