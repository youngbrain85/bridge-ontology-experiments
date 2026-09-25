"""Strict geometry conversion and anonymous copies for human review.

export_review(experiment_dir, backend_dir, schema_path) -> operational report.
Original runs/<run_id>/model_input.json files are never rewritten. No scores.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


VERSION = "0.2.0"
GEOMETRY_FILES = ("model.glb", "model.dxf", "model.obj")
EXECUTION_STATES = {
    "completed", "failed", "pending", "running", "skipped", "cancelled", "error", "incomplete",
    "interrupted_outcome_unknown", "http_error", "transport_error_outcome_unknown",
    "invalid_api_response", "provider_incomplete", "provider_failed", "refused",
    "unexpected_tool_or_output", "invalid_model_json", "protocol_deviation",
    "clarification_requested", "clarification_limit_reached", "stopped_before_continuation",
    "interrupted_before_continuation",
}
EXECUTION_STATES.update({"http_error", "transport_error_outcome_unknown", "interrupted_outcome_unknown",
                         "invalid_api_response", "invalid_model_json", "refused", "unexpected_tool_or_output",
                         "provider_incomplete", "provider_failed", "provider_cancelled", "provider_unknown"})
EXECUTION_STATES.add("protocol_deviation")
CONVERSION_STATES = {"ok", "partial", "failed", "empty"}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _strict_json_object(path):
    def constant(token):
        raise ValueError("Nonfinite JSON number")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    value = json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=constant, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("Model JSON is not an object")
    return value


def _review_mapping(experiment_dir, schedule):
    runs = schedule.get("runs") if isinstance(schedule, dict) else None
    if not isinstance(runs, list):
        raise ValueError("schedule.json must contain a runs array")
    run_ids = set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("run_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run["run_id"]):
            raise ValueError("Each schedule run_id must be a safe nonempty directory name")
        if run["run_id"] in run_ids:
            raise ValueError("Duplicate schedule run_id")
        run_ids.add(run["run_id"])
    key_path = experiment_dir / "admin" / "review_key.json"
    if key_path.exists():
        saved = _read_json(key_path)
        saved_runs = saved.get("slots", [])
        if len(saved_runs) != len(runs) or {item.get("run_id") for item in saved_runs} != run_ids:
            raise ValueError("Existing review key does not match this schedule; use a new experiment directory")
        expected = {run["run_id"]: run for run in runs}
        review_ids = set()
        for item in saved_runs:
            review_id = item.get("review_id")
            if not isinstance(review_id, str) or not re.fullmatch(r"S[0-9]{3,}", review_id) or review_id in review_ids:
                raise ValueError("Existing review key has invalid or duplicate review IDs")
            review_ids.add(review_id)
            if any(item.get(field) != expected[item["run_id"]].get(field) for field in ("condition", "repetition", "case_id")):
                raise ValueError("Existing review key metadata does not match this schedule")
        return saved_runs
    randomized = list(runs)
    secrets.SystemRandom().shuffle(randomized)
    slots = [{"review_id": f"S{index:03d}", "run_id": run["run_id"], "condition": run.get("condition"), "repetition": run.get("repetition"), "case_id": run.get("case_id")} for index, run in enumerate(randomized, 1)]
    _write_json(key_path, {"version": VERSION, "purpose": "Administrator-only review key. Do not distribute with review files.", "slots": slots})
    return slots


def _anonymize_glb(source, destination):
    data = source.read_bytes()
    if len(data) < 20:
        raise ValueError("Invalid GLB header")
    magic, version, total = struct.unpack_from("<III", data)
    if magic != 0x46546C67 or version != 2 or total != len(data):
        raise ValueError("Invalid GLB header")
    chunks = []
    offset = 12
    json_count = 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated GLB chunk")
        length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        payload = data[offset:offset + length]
        if len(payload) != length or length % 4:
            raise ValueError("Invalid GLB chunk length")
        offset += length
        if chunk_type == 0x4E4F534A:
            json_count += 1
            gltf = json.loads(payload)

            def strip(value):
                if isinstance(value, dict):
                    return {key: strip(item) for key, item in value.items() if key not in ("name", "extras", "copyright")}
                if isinstance(value, list):
                    return [strip(item) for item in value]
                return value

            gltf = strip(gltf)
            gltf["asset"]["generator"] = "Anonymous geometry review export"
            payload = json.dumps(gltf, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            payload += b" " * ((-len(payload)) % 4)
        chunks.append(struct.pack("<II", len(payload), chunk_type) + payload)
    if json_count != 1:
        raise ValueError("GLB must contain one JSON chunk")
    body = b"".join(chunks)
    destination.write_bytes(struct.pack("<III", magic, version, 12 + len(body)) + body)


def _anonymize_dxf(source, destination):
    import ezdxf

    original = ezdxf.readfile(source)
    clean = ezdxf.new("R2013")
    clean.units = original.units
    space = clean.modelspace()
    layers = {}
    for item in original.modelspace():
        if item.dxftype() != "3DFACE":
            raise ValueError("Expected backend 3DFACE-only DXF")
        old_layer = item.dxf.layer
        if old_layer not in layers:
            layers[old_layer] = f"Part{len(layers) + 1:04d}"
            clean.layers.new(layers[old_layer])
        points = [item.dxf.get(f"vtx{i}") for i in range(4)]
        attributes = {"layer": layers[old_layer]}
        for attribute in ("color", "true_color", "transparency", "invisible_edges"):
            if item.dxf.hasattr(attribute):
                attributes[attribute] = item.dxf.get(attribute)
        space.add_3dface(points, dxfattribs=attributes)
    clean.saveas(destination)


def _anonymize_obj(source, destination):
    lines = ["# Anonymous geometry review copy", "# length_unit=mm; up_axis=Z"]
    object_count = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        prefix = stripped.split(None, 1)[0]
        if prefix in ("o", "g"):
            object_count += 1
            lines.append("o Object" + str(object_count).zfill(4))
        elif prefix in ("v", "vt", "vn", "f", "s"):
            lines.append(line)
        else:
            raise ValueError("Unsupported OBJ directive in backend output")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _convert(run_dir, model_path, backend_path, schema_path, receipt_path):
    conversion = run_dir / "conversion"
    signature = {"input_sha256": _sha(model_path), "backend_sha256": _sha(backend_path), "schema_sha256": _sha(schema_path)}
    if conversion.exists() and receipt_path.exists():
        receipt = _read_json(receipt_path)
        report_path = conversion / "conversion_report.json"
        if all(receipt.get(key) == value for key, value in signature.items()) and report_path.exists():
            report = _read_json(report_path)
            exports_intact = all((conversion / name).exists() and _sha(conversion / name) == item.get("sha256") for name, item in report.get("exports", {}).items())
            if report.get("input_sha256") == signature["input_sha256"] and exports_intact:
                return report, {**receipt, "reused": True}
    if conversion.exists():
        history = run_dir / "conversion_history"
        history.mkdir(exist_ok=True)
        index = 1
        while (history / f"attempt_{index:03d}").exists():
            index += 1
        conversion.replace(history / f"attempt_{index:03d}")
    command = [sys.executable, "-X", "utf8", str(backend_path), "--input", str(model_path), "--output", str(conversion), "--schema", str(schema_path)]
    receipt = {**signature, "command": command, "reused": False}
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False)
        receipt.update({"returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
        report_path = conversion / "conversion_report.json"
        if report_path.exists():
            report = _read_json(report_path)
        else:
            report = {"status": "failed", "global_errors": ["Backend did not produce a conversion report"]}
    except Exception as error:
        receipt.update({"returncode": None, "error": str(error)})
        report = {"status": "failed", "global_errors": ["Backend invocation failed: " + str(error)]}
    if _sha(model_path) != signature["input_sha256"]:
        raise RuntimeError("Original model_input.json changed during conversion")
    _write_json(receipt_path, receipt)
    return report, receipt


def _index_html(summaries):
    names = {"ok": "Converted", "partial": "Partially converted", "failed": "Conversion failed", "empty": "No convertible geometry", "not_attempted": "No input"}
    rows = []
    for summary in summaries:
        slot = html.escape(summary["review_id"])
        links = " · ".join(f'<a href="{slot}/{html.escape(name)}">{html.escape(name)}</a>' for name in summary["files"])
        links += (" · " if links else "") + f'<a href="{slot}/run_summary.json">Execution status</a>'
        state = html.escape(names.get(summary["conversion_status"], summary["conversion_status"]))
        execution = "Protocol deviation" if summary["execution_status"] == "protocol_deviation" else html.escape(summary["execution_status"])
        rows.append(f"<tr><td>{slot}</td><td>{execution}</td><td>{state}</td><td>{links}</td></tr>")
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Blinded model review</title><style>body{font-family:system-ui,sans-serif;max-width:1000px;margin:48px auto;padding:0 20px;color:#182332}table{border-collapse:collapse;width:100%}th,td{padding:14px;text-align:left;border-bottom:1px solid #ccd4dd}a{color:#155eab}p{line-height:1.7}</style><h1>Blinded model review</h1><p>Each identifier represents one generation result. Missing inputs and conversion failures remain in the same list. Status labels describe execution and file conversion; they are not accuracy judgments or scores.</p><p>These review copies omit condition names and run identifiers. Geometry and other output characteristics may still reveal the condition, so complete blinding is not guaranteed. GLB uses meters and Y-up; DXF and OBJ use millimeters and Z-up.</p><table><thead><tr><th>Review ID</th><th>Execution status</th><th>Conversion status</th><th>Files</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table></html>\n"


def export_review(experiment_dir: Path, backend_dir: Path, schema_path: Path) -> dict:
    """Export all scheduled runs, retaining failed/missing runs as anonymous slots.

    The caller can distribute experiment_dir/review by itself. admin and runs
    contain identifying material and must not be included in the blind review.
    Existing completed conversions are reused only when all signatures match.
    """
    experiment_dir = Path(experiment_dir).resolve()
    backend_path = Path(backend_dir).resolve() / "geometry_backend.py"
    schema_path = Path(schema_path).resolve()
    if not backend_path.is_file() or not schema_path.is_file():
        raise FileNotFoundError("Geometry backend or common schema is missing")
    slots = _review_mapping(experiment_dir, _read_json(experiment_dir / "schedule.json"))
    summaries, administrator = [], []
    review_dir = experiment_dir / "review"
    review_dir.mkdir(exist_ok=True)
    for slot in sorted(slots, key=lambda item: item["review_id"]):
        run_dir = experiment_dir / "runs" / slot["run_id"]
        model_path = run_dir / "model_input.json"
        result_path = run_dir / "result.json"
        summary = {"review_id": slot["review_id"], "execution_status": "missing_result", "parse_status": "missing", "conversion_status": "not_attempted", "review_export_status": "no_geometry", "files": []}
        audit = {"review_id": slot["review_id"], "run_id": slot["run_id"]}
        if result_path.exists():
            try:
                result = _read_json(result_path)
                status = result.get("status") if isinstance(result, dict) else None
                summary["execution_status"] = status if isinstance(status, str) and status in EXECUTION_STATES else "unknown"
            except (OSError, ValueError):
                summary["execution_status"] = "invalid_result"
        if model_path.exists():
            original_sha = _sha(model_path)
            try:
                _strict_json_object(model_path)
                summary["parse_status"] = "valid_json_object"
            except (OSError, ValueError) as error:
                summary["parse_status"] = "invalid_json"
                audit["parse_error"] = str(error)
            receipt_path = experiment_dir / "admin" / "conversion_receipts" / (slot["run_id"] + ".json")
            conversion_report, receipt = _convert(run_dir, model_path, backend_path, schema_path, receipt_path)
            status = conversion_report.get("status")
            summary["conversion_status"] = status if isinstance(status, str) and status in CONVERSION_STATES else "failed"
            audit.update({"input_sha256": original_sha, "conversion_report": conversion_report, "receipt": receipt})
        slot_dir = review_dir / slot["review_id"]
        # Build each anonymous slot separately, then publish only sanitized files.
        with tempfile.TemporaryDirectory(prefix="_review_stage_", dir=str(experiment_dir / "admin")) as temporary:
            stage = Path(temporary)
            if summary["conversion_status"] in ("ok", "partial"):
                try:
                    conversion = run_dir / "conversion"
                    _anonymize_glb(conversion / "model.glb", stage / "model.glb")
                    _anonymize_dxf(conversion / "model.dxf", stage / "model.dxf")
                    _anonymize_obj(conversion / "model.obj", stage / "model.obj")
                    summary["files"] = list(GEOMETRY_FILES)
                    summary["review_export_status"] = "available"
                except Exception as error:
                    summary["review_export_status"] = "failed"
                    audit["review_export_error"] = str(error)
            _write_json(stage / "run_summary.json", summary)
            if slot_dir.exists():
                # Preserve earlier review copies under the administrator-only history.
                history = experiment_dir / "admin" / "review_history" / slot["review_id"]
                history.mkdir(parents=True, exist_ok=True)
                index = 1
                while (history / f"version_{index:03d}").exists():
                    index += 1
                slot_dir.replace(history / f"version_{index:03d}")
            slot_dir.mkdir()
            for name in summary["files"] + ["run_summary.json"]:
                (stage / name).replace(slot_dir / name)
        if model_path.exists() and _sha(model_path) != audit["input_sha256"]:
            raise RuntimeError("Original model_input.json changed during review export")
        summaries.append(summary)
        administrator.append(audit)
    (review_dir / "index.html").write_text(_index_html(summaries), encoding="utf-8")
    _write_json(experiment_dir / "admin" / "review_export_audit.json", {"version": VERSION, "runs": administrator})
    report = {"version": VERSION, "status": "completed", "review_slot_count": len(summaries), "available_geometry_count": sum(item["review_export_status"] == "available" for item in summaries), "review_directory": str(review_dir), "review_key": str(experiment_dir / "admin" / "review_key.json"), "summaries": summaries}
    _write_json(experiment_dir / "admin" / "review_export_report.json", report)
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--schema", required=True)
    args = parser.parse_args()
    print(json.dumps(export_review(Path(args.experiment), Path(args.backend), Path(args.schema)), ensure_ascii=False))
