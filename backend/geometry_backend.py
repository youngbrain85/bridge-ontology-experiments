"""Mechanical JSON-to-mesh adapter with logged, bounded rotation precision handling.

Contains no domain classification or shape inference. Source JSON stays unchanged.

CLI: python geometry_backend.py --input model.json --output new_directory --schema schema.json
Python: convert_model(input_path, output_dir, schema_path=None) -> report dictionary
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

import ezdxf
import numpy as np
import trimesh
from shapely.geometry import Polygon
from shapely.validation import explain_validity

VERSION = "0.1.2"
ROTATION_POLICY = {
    "version": "near_rotation_so3_v1",
    "strict_orthogonality_atol": 1e-6,
    "strict_determinant_atol": 1e-6,
    "strict_determinant_rtol": 1e-9,
    "near_rotation_max_error": 1e-4,
    "method": "SVD nearest SO(3)",
    "original_input_preserved": True,
    "accepted_rotations_unchanged": True,
    "scope": "Input frame and entity pose rotations only; before composition and unit conversion",
}
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}
CURVE_SECTIONS = 64
MAX_VERTICES = 500000
MAX_FACES = 1000000
GLB_FROM_MM = np.array([[0.001, 0, 0, 0], [0, 0, 0.001, 0], [0, -0.001, 0, 0], [0, 0, 0, 1]], dtype=float)
PARAMETERS = {
    "box": ({"length_unit", "extents"}, set()),
    "cylinder": ({"length_unit", "radius", "height"}, set()),
    "tube": ({"length_unit", "outer_radius", "inner_radius", "height"}, set()),
    "extruded_polygon": ({"length_unit", "polygon", "height"}, {"holes"}),
    "mesh": ({"length_unit", "vertices", "faces"}, set()),
}


class GeometryInputError(ValueError):
    pass


def _jsonschema_uses_referencing() -> bool:
    """True from jsonschema 4.18, where validators take ``registry=`` and ``resolver=`` is a broken shim."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("jsonschema")
        parts = [int(re.match(r"\d+", part).group()) for part in version.split(".")[:2]]
    except (importlib.metadata.PackageNotFoundError, AttributeError, ValueError):
        return False
    return tuple(parts) >= (4, 18)


def schema_errors(validator, instance) -> list:
    """Collect validation errors; surface a refused external reference as its own message."""
    try:
        return list(validator.iter_errors(instance))
    except Exception as exc:  # the referencing layer wraps the refusal raised by the retriever
        cause = exc
        while cause is not None:
            if isinstance(cause, GeometryInputError):
                raise cause from None
            cause = cause.__cause__ or cause.__context__
        raise


def local_schema_validator(schema: dict):
    """Keep all schema references local; model conversion never fetches schemas.

    jsonschema >= 4.18 resolves references through the ``referencing`` library.
    The bundled Draft 2020-12 vocabulary meta-schemas stay available, and any
    reference that is not local to the document or those bundled resources is
    rejected instead of retrieved. The legacy ``RefResolver`` path is kept for
    the pinned jsonschema 4.4 environment: its urljoin handling turns a URN plus
    '#/$defs/...' into a fragment-only URL, so the document is also registered
    at the empty URI. Passing ``resolver=`` to newer releases leaks ``$id``
    scopes during the meta-schema pass and must not be used there.
    """
    from jsonschema import Draft202012Validator

    if not _jsonschema_uses_referencing():
        from jsonschema import RefResolver

        class LocalOnlyResolver(RefResolver):
            def resolve_remote(self, uri):
                raise GeometryInputError(f"External schema reference is disabled: {uri!r}")

        resolver = LocalOnlyResolver.from_schema(schema, store={"": schema})
        return Draft202012Validator(schema, resolver=resolver)

    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    def refuse_retrieval(uri):
        raise GeometryInputError(f"External schema reference is disabled: {uri!r}")

    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = Registry(retrieve=refuse_retrieval).with_resource("", resource)
    schema_id = schema.get("$id")
    if isinstance(schema_id, str) and schema_id:
        registry = registry.with_resource(schema_id, resource)
    # The validator combines the bundled specification meta-schemas into this
    # registry itself, so Draft 2020-12 vocabulary references resolve offline.
    return Draft202012Validator(schema, registry=registry)


def unit_factor(unit: Any, context: str) -> float:
    if not isinstance(unit, str) or unit not in UNIT_TO_MM:
        raise GeometryInputError(f"{context}: explicit length_unit must be mm, cm, or m; got {unit!r}")
    return UNIT_TO_MM[unit]


def positive(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise GeometryInputError(f"{context}: expected an explicit finite positive number")
    return float(value)


def numeric_array(value: Any, columns: int, context: str, minimum: int = 1) -> np.ndarray:
    if not isinstance(value, list) or len(value) < minimum or len(value) > MAX_VERTICES:
        raise GeometryInputError(f"{context}: expected {minimum}..{MAX_VERTICES} rows")
    for row in value:
        if not isinstance(row, list) or len(row) != columns:
            raise GeometryInputError(f"{context}: every row must have {columns} numbers")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in row):
            raise GeometryInputError(f"{context}: every coordinate must be a finite number")
    return np.asarray(value, dtype=float)


def _rotation_metrics(rotation: np.ndarray) -> dict:
    # Finite entries can still overflow when multiplied. Such matrices are not
    # eligible for projection and must not emit NaN/Infinity into JSON reports.
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        error = float(np.max(np.abs(rotation.T @ rotation - np.eye(3))))
        determinant = float(np.linalg.det(rotation))
    return {"orthogonality_max_abs_error": error, "determinant": determinant,
            "determinant_abs_error": abs(determinant - 1.0)}


def _strict_rotation(metrics: dict) -> bool:
    return (math.isfinite(metrics["orthogonality_max_abs_error"])
            and metrics["orthogonality_max_abs_error"] <= ROTATION_POLICY["strict_orthogonality_atol"]
            and math.isclose(metrics["determinant"], 1.0,
                             rel_tol=ROTATION_POLICY["strict_determinant_rtol"],
                             abs_tol=ROTATION_POLICY["strict_determinant_atol"]))


def rigid_matrix(value: Any, translation_factor: float, context: str, *,
                 corrections=None, target=None, frame_id=None, entity_id=None) -> np.ndarray:
    matrix = numeric_array(value, 4, context, 4)
    if matrix.shape != (4, 4):
        raise GeometryInputError(f"{context}: matrix must have exactly four rows")
    if not np.allclose(matrix[3], [0, 0, 0, 1], rtol=0, atol=1e-9):
        raise GeometryInputError(f"{context}: last row must be [0,0,0,1]")
    result = matrix.copy()
    rotation = matrix[:3, :3]
    before = _rotation_metrics(rotation)
    correction = None
    if not _strict_rotation(before):
        maximum = ROTATION_POLICY["near_rotation_max_error"]
        if (not all(math.isfinite(v) for v in before.values())
                or before["determinant"] <= 0
                or before["orthogonality_max_abs_error"] > maximum
                or before["determinant_abs_error"] > maximum):
            raise GeometryInputError(f"{context}: rotation must be orthonormal with determinant +1; scale/shear/reflection are unsupported beyond the bounded rotation precision policy")
        try:
            left, _, right_t = np.linalg.svd(rotation)
        except np.linalg.LinAlgError as exc:
            raise GeometryInputError(f"{context}: bounded rotation projection failed") from exc
        projected = left @ right_t
        after = _rotation_metrics(projected)
        # An eligible matrix has positive determinant and is nonsingular. Its
        # nearest orthogonal matrix is already in SO(3); never flip a reflection.
        if not _strict_rotation(after) or after["determinant"] <= 0:
            raise GeometryInputError(f"{context}: bounded rotation projection did not produce a proper rotation")
        result[:3, :3] = projected
        correction = {
            "policy_version": ROTATION_POLICY["version"],
            "target": target, "frame_id": frame_id, "entity_id": entity_id,
            "context": context,
            "before_transform": matrix.tolist(),
            "after_transform": result.tolist(),
            "before_metrics": before, "after_metrics": after,
            "rotation_max_abs_change": float(np.max(np.abs(projected - rotation))),
            "translation_to_mm_factor": translation_factor,
            "transform_units": "Original input translation units, before conversion to mm",
        }
    result[:3, 3] *= translation_factor
    if not np.isfinite(result).all():
        raise GeometryInputError(f"{context}: unit conversion exceeds numeric range")
    if correction is not None and corrections is not None:
        corrections.append(correction)
    return result


class FrameResolver:
    """Resolve explicit rigid frame chains after normalizing translations to mm."""

    def __init__(self, frames: Any, rotation_corrections=None):
        if not isinstance(frames, list):
            raise GeometryInputError("coordinate_frames must be an array")
        self.frames = {}
        self.cache = {}
        self.errors = {}
        self.rotation_corrections = [] if rotation_corrections is None else rotation_corrections
        for frame in frames:
            if not isinstance(frame, dict) or not isinstance(frame.get("id"), str) or not frame["id"]:
                raise GeometryInputError("each coordinate frame needs a nonempty string id")
            if frame["id"] in self.frames:
                raise GeometryInputError(f"duplicate coordinate frame id: {frame['id']}")
            self.frames[frame["id"]] = frame

    def resolve(self, frame_id: Any, chain=()) -> np.ndarray:
        if not isinstance(frame_id, str) or frame_id not in self.frames:
            raise GeometryInputError(f"missing/unknown coordinate frame: {frame_id!r}")
        if frame_id in chain:
            raise GeometryInputError("coordinate frame cycle: " + " -> ".join(chain + (frame_id,)))
        if frame_id in self.cache:
            return self.cache[frame_id].copy()
        frame = self.frames[frame_id]
        own_factor = unit_factor(frame.get("length_unit"), f"frame {frame_id}")
        if "parent_frame_id" not in frame:
            raise GeometryInputError(f"frame {frame_id}: parent_frame_id must be explicit (null for root)")
        parent_id = frame["parent_frame_id"]
        if parent_id is None:
            parent_world = np.eye(4)  # Explicit null selects the defined world, not a missing transform.
            translation_factor = own_factor
        else:
            parent_world = self.resolve(parent_id, chain + (frame_id,))
            translation_factor = unit_factor(self.frames[parent_id].get("length_unit"), f"parent of {frame_id}")
        pending_corrections = []
        local = rigid_matrix(frame.get("transform_to_parent"), translation_factor, f"frame {frame_id}.transform_to_parent",
                             corrections=pending_corrections, target="frame", frame_id=frame_id)
        with np.errstate(over="ignore", invalid="ignore"):
            world = parent_world @ local
        if not np.isfinite(world).all():
            raise GeometryInputError(f"frame {frame_id}: transform composition exceeds numeric range")
        self.cache[frame_id] = world
        self.rotation_corrections.extend(pending_corrections)
        return world.copy()

    def pose(self, value: Any, entity_id=None) -> np.ndarray:
        if not isinstance(value, dict):
            raise GeometryInputError("entity.pose is absent/null; no placement assumed")
        frame_id = value.get("frame_id")
        world = self.resolve(frame_id)
        factor = unit_factor(self.frames[frame_id].get("length_unit"), f"pose frame {frame_id}")
        pending_corrections = []
        local = rigid_matrix(value.get("transform"), factor, "entity.pose.transform",
                             corrections=pending_corrections, target="entity_pose",
                             frame_id=frame_id, entity_id=entity_id)
        with np.errstate(over="ignore", invalid="ignore"):
            result = world @ local
        if not np.isfinite(result).all():
            raise GeometryInputError("entity.pose: transform composition exceeds numeric range")
        self.rotation_corrections.extend(pending_corrections)
        return result


def make_primitive(primitive: Any) -> trimesh.Trimesh:
    if not isinstance(primitive, dict):
        raise GeometryInputError("primitive must be an object")
    kind = primitive.get("kind")
    if not isinstance(kind, str) or kind not in PARAMETERS:
        raise GeometryInputError(f"unsupported primitive kind: {kind!r}")
    params = primitive.get("parameters")
    if not isinstance(params, dict):
        raise GeometryInputError("primitive.parameters must be an object")
    required, optional = PARAMETERS[kind]
    missing = required - set(params)
    extra = set(params) - required - optional
    if missing or extra:
        raise GeometryInputError(f"{kind} parameters: missing={sorted(missing)}, unsupported={sorted(extra)}")
    factor = unit_factor(params["length_unit"], f"primitive {kind}")
    if kind == "box":
        extents = params["extents"]
        if not isinstance(extents, list) or len(extents) != 3:
            raise GeometryInputError("box.extents must contain three explicit lengths")
        mesh = trimesh.creation.box(extents=np.array([positive(v, "box.extents") for v in extents]) * factor)
    elif kind == "cylinder":
        mesh = trimesh.creation.cylinder(radius=positive(params["radius"], "cylinder.radius") * factor, height=positive(params["height"], "cylinder.height") * factor, sections=CURVE_SECTIONS)
    elif kind == "tube":
        outer = positive(params["outer_radius"], "tube.outer_radius")
        inner = positive(params["inner_radius"], "tube.inner_radius")
        if inner >= outer:
            raise GeometryInputError("tube.inner_radius must be smaller than outer_radius")
        mesh = trimesh.creation.annulus(r_min=inner * factor, r_max=outer * factor, height=positive(params["height"], "tube.height") * factor, sections=CURVE_SECTIONS)
    elif kind == "extruded_polygon":
        boundary = numeric_array(params["polygon"], 2, "extruded_polygon.polygon", 3) * factor
        hole_data = params.get("holes", [])
        if not isinstance(hole_data, list):
            raise GeometryInputError("extruded_polygon.holes must be an array of rings")
        holes = [numeric_array(h, 2, "extruded_polygon.hole", 3) * factor for h in hole_data]
        polygon = Polygon(boundary, holes=holes)
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
            raise GeometryInputError("extruded_polygon: invalid polygon; " + explain_validity(polygon))
        mesh = trimesh.creation.extrude_polygon(polygon, positive(params["height"], "extruded_polygon.height") * factor, engine="earcut")
    else:
        vertices = numeric_array(params["vertices"], 3, "mesh.vertices", 3) * factor
        faces = params["faces"]
        if not isinstance(faces, list) or not 1 <= len(faces) <= MAX_FACES:
            raise GeometryInputError(f"mesh.faces must contain 1..{MAX_FACES} triangles")
        for face in faces:
            if not isinstance(face, list) or len(face) != 3 or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(vertices) for i in face):
                raise GeometryInputError("mesh.faces must be triangles of zero-based in-range integer vertex indices")
            if len(set(face)) != 3:
                raise GeometryInputError("mesh.faces contains a triangle with repeated vertex indices")
        face_array = np.asarray(faces, dtype=np.int64)
        edge1 = vertices[face_array[:, 1]] - vertices[face_array[:, 0]]
        edge2 = vertices[face_array[:, 2]] - vertices[face_array[:, 0]]
        if np.any(np.linalg.norm(np.cross(edge1, edge2), axis=1) == 0):
            raise GeometryInputError("mesh.faces contains a zero-area triangle")
        mesh = trimesh.Trimesh(vertices=vertices, faces=face_array, process=False, validate=False)
    if len(mesh.vertices) > MAX_VERTICES or len(mesh.faces) > MAX_FACES:
        raise GeometryInputError("primitive exceeds backend mesh capacity")
    if not len(mesh.vertices) or not len(mesh.faces) or not np.isfinite(mesh.vertices).all():
        raise GeometryInputError("primitive produced empty or nonfinite mesh")
    return mesh


def safe_name(value: str, prefix="item") -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", value)[:64].strip("_")
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{clean or 'unnamed'}_{suffix}"


def _metadata(entity: dict, primitive_index: int) -> dict:
    return {"id": entity["id"], "entity_id": entity["id"], "label": entity.get("label"), "kind": entity.get("kind"), "primitive_index": primitive_index}


def write_json(path: Path, value: Any):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _pointer_get(value: Any, pointer: str):
    if pointer == "":
        return True, value
    if not pointer.startswith("/"):
        return False, None
    current = value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def inspect_unknowns(model: dict) -> dict:
    """Flag syntactic populated-field collisions; never evaluate an answer."""
    entities = {item.get("id"): item for item in model.get("entities", []) if isinstance(item, dict)}
    result = {"purpose": "Syntactic review only; declared values are never changed. No correctness scores.", "items": []}
    unknowns = model.get("unknowns", [])
    if not isinstance(unknowns, list):
        result["input_status"] = "ignored_non_array"
        result["input_type"] = type(unknowns).__name__
        return result
    for index, unknown in enumerate(unknowns):
        entry = {"index": index, "original": unknown, "populated_candidates": []}
        if not isinstance(unknown, dict):
            entry["status"] = "unresolved_field"
            result["items"].append(entry)
            continue
        target, field = unknown.get("target"), unknown.get("field")
        scope = entities.get(target) if isinstance(target, str) else {k: v for k, v in model.items() if k != "unknowns"}
        if not isinstance(field, str) or scope is None:
            entry["status"] = "unresolved_field"
            result["items"].append(entry)
            continue
        if field.startswith("/"):
            ok, value = _pointer_get(scope, field)
            if not ok:
                ok, value = _pointer_get(model, field)
            if ok and value is not None:
                entry["populated_candidates"].append({"path": field, "value": value})
        elif "." in field:
            pointer = "/" + "/".join(field.split("."))
            ok, value = _pointer_get(scope, pointer)
            if ok and value is not None:
                entry["populated_candidates"].append({"path": pointer, "value": value})
        else:
            def walk(value, path=""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        escaped = key.replace("~", "~0").replace("/", "~1")
                        item_path = path + "/" + escaped
                        if key == field and item is not None:
                            entry["populated_candidates"].append({"path": item_path, "value": item})
                        if key not in ("source_evidence", "unknowns"):
                            walk(item, item_path)
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        walk(item, path + "/" + str(i))
            walk(scope)
        entry["status"] = "possible_value_conflict" if entry["populated_candidates"] else "no_populated_match_found"
        result["items"].append(entry)
    return result


def export_meshes(meshes, output_dir: Path) -> dict:
    scene = trimesh.Scene(base_frame="world_y_up_m")
    for entity, index, mesh in meshes:
        glb_mesh = mesh.copy()
        glb_mesh.apply_transform(GLB_FROM_MM)
        glb_mesh.metadata.update(_metadata(entity, index))
        object_name = safe_name(f"{entity['id']}__primitive_{index}", "obj")
        scene.add_geometry(glb_mesh, node_name=object_name, geom_name=object_name, metadata=_metadata(entity, index))
    scene.metadata.update({"generator": "generic_geometry_backend " + VERSION, "length_unit": "m", "up_axis": "Y", "source_axes": "Z up mm", "axis_mapping": "[x,y,z] mm -> [x,z,-y]/1000 m"})
    glb_bytes = scene.export(file_type="glb")

    doc = ezdxf.new("R2013")
    doc.units = ezdxf.units.MM
    doc.appids.add("GENERIC_MODEL")
    space = doc.modelspace()
    layer_map = {}
    obj_lines = ["# generic_geometry_backend " + VERSION, "# length_unit=mm; up_axis=Z; input model preserved in model_input.json"]
    vertex_offset = 1
    for entity, index, mesh in meshes:
        layer = safe_name(entity["id"], "part")
        if layer not in doc.layers:
            doc.layers.new(layer)
            layer_map[layer] = {"id": entity["id"], "label": entity.get("label"), "kind": entity.get("kind")}
        metadata = _metadata(entity, index)
        # ASCII JSON keeps DXF XDATA safe even when original strings contain non-ASCII/control characters.
        encoded_metadata = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
        # XDATA uses a short stable sidecar key to avoid DXF's per-entity 16 KB cap.
        xdata = [(1000, "entity_key=" + layer), (1000, "primitive_index=" + str(index))]
        for face in mesh.faces:
            points = [tuple(map(float, mesh.vertices[int(i)])) for i in face]
            item = space.add_3dface(points + [points[2]], dxfattribs={"layer": layer})
            item.set_xdata("GENERIC_MODEL", xdata)
        obj_lines.append("o " + safe_name(f"{entity['id']}__primitive_{index}", "obj"))
        obj_lines.append("# metadata " + encoded_metadata)
        obj_lines.extend("v " + " ".join(format(float(value), ".17g") for value in vertex) for vertex in mesh.vertices)
        obj_lines.extend("f " + " ".join(str(int(value) + vertex_offset) for value in face) for face in mesh.faces)
        vertex_offset += len(mesh.vertices)
    (output_dir / "model.glb").write_bytes(glb_bytes)
    doc.saveas(output_dir / "model.dxf")
    (output_dir / "model.obj").write_text("\n".join(obj_lines) + "\n", encoding="utf-8")
    write_json(output_dir / "dxf_layer_map.json", layer_map)
    return {name: {"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": hashlib.sha256((output_dir / name).read_bytes()).hexdigest()} for name in ("model.glb", "model.dxf", "model.obj", "dxf_layer_map.json")}


def _reject_nonfinite(token):
    raise GeometryInputError(f"JSON contains nonfinite number token: {token}")


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise GeometryInputError(f"JSON contains duplicate key: {key}")
        value[key] = item
    return value


def convert_model(input_path, output_dir, schema_path=None) -> Dict[str, Any]:
    input_path, output_dir = Path(input_path), Path(output_dir)
    report = {
        "backend_version": VERSION,
        "status": "failed",
        "purpose": "Geometry conversion diagnostics only. No domain inference or answer scoring. Original input preserved; bounded rotation projections logged separately.",
        "input": str(input_path.resolve()),
        "units": {"internal": "mm", "glb": "m", "dxf": "mm", "obj": "mm"},
        "axis_conversion": {"input": "right-handed Z up", "glb": "right-handed Y up; [x,y,z] -> [x,z,-y]/1000", "dxf_obj": "right-handed Z up; no axis rotation"},
        "tessellation": {"circular_sections": CURVE_SECTIONS, "polygon_engine": "mapbox_earcut"},
        "rotation_policy": dict(ROTATION_POLICY),
        "rotation_corrections": [], "rotation_correction_count": 0,
        "global_errors": [], "frame_errors": [], "entities": [], "exports": {},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    protected = [name for name in ("model.glb", "model.dxf", "model.obj", "conversion_report.json", "model_input.json") if (output_dir / name).exists()]
    if protected:
        report["global_errors"].append("Output directory already contains run artifacts; use a new directory: " + ", ".join(protected))
        return report  # Never overwrite a prior run's report or files.
    try:
        data = input_path.read_bytes()
        report["input_sha256"] = hashlib.sha256(data).hexdigest()
        (output_dir / "model_input.json").write_bytes(data)
        model = json.loads(data.decode("utf-8-sig"), parse_constant=_reject_nonfinite, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(model, dict) or not isinstance(model.get("entities"), list):
            raise GeometryInputError("model must be an object containing an entities array")
        if schema_path is not None:
            from jsonschema import Draft202012Validator
            schema = json.loads(Path(schema_path).read_text(encoding="utf-8-sig"))
            # Meta-schema resources bundled with jsonschema stay available in
            # the resolver store, but neither validation pass may use a URL fetch.
            meta_errors = schema_errors(local_schema_validator(Draft202012Validator.META_SCHEMA), schema)
            if meta_errors:
                raise GeometryInputError("Invalid common model schema: " + meta_errors[0].message)
            instance_errors = sorted(schema_errors(local_schema_validator(schema), model), key=lambda error: str(list(error.absolute_path)))
            if instance_errors:
                raise GeometryInputError("Common model schema failed: " + "; ".join(str(list(error.absolute_path)) + ": " + error.message for error in instance_errors[:20]))
            report["schema_validation"] = "passed"
        else:
            report["schema_validation"] = "not_requested"
        seen = set()
        for entity in model["entities"]:
            if not isinstance(entity, dict) or not isinstance(entity.get("id"), str) or not entity["id"]:
                raise GeometryInputError("every entity must have a nonempty string id")
            if entity["id"] in seen:
                raise GeometryInputError(f"duplicate entity id: {entity['id']}")
            seen.add(entity["id"])
        resolver = FrameResolver(model.get("coordinate_frames", []), report["rotation_corrections"])
        for frame_id in resolver.frames:
            try:
                resolver.resolve(frame_id)
            except GeometryInputError as exc:
                report["frame_errors"].append({"frame_id": frame_id, "error": str(exc)})
        write_json(output_dir / "unknown_review.json", inspect_unknowns(model))
        meshes = []
        for entity in model["entities"]:
            row = {"id": entity["id"], "label": entity.get("label"), "kind": entity.get("kind"), "status": "skipped", "errors": [], "primitives": []}
            report["entities"].append(row)
            geometry = entity.get("geometry")
            if geometry is None:
                row["status"] = "no_geometry"
                continue
            try:
                if not isinstance(geometry, dict) or not isinstance(geometry.get("primitives"), list):
                    raise GeometryInputError("geometry.primitives must be an array")
                transform = resolver.pose(entity.get("pose"), entity_id=entity["id"])
            except GeometryInputError as exc:
                row["errors"].append(str(exc))
                continue
            entity_meshes = []
            for index, primitive in enumerate(geometry["primitives"]):
                entry = {"index": index, "kind": primitive.get("kind") if isinstance(primitive, dict) else None, "status": "skipped"}
                row["primitives"].append(entry)
                try:
                    mesh = make_primitive(primitive)
                    mesh.apply_transform(transform)
                    if not np.isfinite(mesh.vertices).all():
                        raise GeometryInputError("transformed vertices exceed numeric range")
                    # GLB positions are float32; reject conversion overflow explicitly.
                    if np.max(np.abs(mesh.vertices)) / 1000 > np.finfo(np.float32).max:
                        raise GeometryInputError("geometry exceeds GLB float32 coordinate range")
                    meshes.append((entity, index, mesh))
                    entity_meshes.append(mesh)
                    entry.update({"status": "converted", "vertices": len(mesh.vertices), "triangles": len(mesh.faces), "bounds_mm": mesh.bounds.tolist()})
                except Exception as exc:
                    entry["error"] = str(exc)
            if entity_meshes:
                row["status"] = "partial" if any(item["status"] == "skipped" for item in row["primitives"]) else "converted"
                all_bounds = np.asarray([mesh.bounds for mesh in entity_meshes])
                row["bounds_mm"] = [all_bounds[:, 0].min(axis=0).tolist(), all_bounds[:, 1].max(axis=0).tolist()]
            elif not geometry["primitives"]:
                row["status"] = "empty_geometry"
        report["mesh_count"] = len(meshes)
        report["converted_entity_count"] = sum(row["status"] in ("converted", "partial") for row in report["entities"])
        if meshes:
            # Do not expose a GLB from a failed DXF/OBJ conversion as a completed run.
            with tempfile.TemporaryDirectory(prefix="_export_", dir=str(output_dir)) as temporary:
                stage = Path(temporary)
                exports = export_meshes(meshes, stage)
                for name in exports:
                    (stage / name).replace(output_dir / name)
                report["exports"] = exports
            report["status"] = "partial" if report["frame_errors"] or any(row["status"] in ("skipped", "partial", "empty_geometry") for row in report["entities"]) else "ok"
        else:
            report["status"] = "empty"
            report["global_errors"].append("No convertible geometry; no model geometry files were written.")
    except Exception as exc:
        report["global_errors"].append(str(exc))
        report["status"] = "failed"
    report["rotation_correction_count"] = len(report["rotation_corrections"])
    write_json(output_dir / "conversion_report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Model JSON path")
    parser.add_argument("--output", required=True, help="New output directory")
    parser.add_argument("--schema", help="Optional generic model JSON Schema")
    args = parser.parse_args()
    report = convert_model(args.input, args.output, args.schema)
    print(json.dumps({"status": report["status"], "mesh_count": report.get("mesh_count", 0), "global_errors": report["global_errors"], "report": str(Path(args.output).resolve() / "conversion_report.json")}, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 2 if report["status"] == "partial" else 3


if __name__ == "__main__":
    raise SystemExit(main())
