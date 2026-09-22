# Common geometry generation syntax v0.1

This syntax is a renderer guide supplied identically to every generation condition. It supplies no correct answers about component types, shapes, dimensions or structural relationships. Use the geometry syntax below in model JSON conforming to `common_output.schema.json`. `entity.kind` and relationship names are free strings; the converter does not change geometry based on these values.

## Coordinates and units

- All input frames are right-handed; the overall model uses +Z upward.
- Each primitive must specify `parameters.length_unit` as `"mm"`, `"cm"` or `"m"`. A primitive with numeric dimensions but missing or null units is not converted.
- Each entity must specify `pose.frame_id` and `pose.transform`. Even when no separate placement is required, explicitly supply an evidence-supported identity matrix. The converter does not replace null or missing matrices with identity matrices.
- Write 4-by-4 matrices as **four rows** acting on column vectors. Rotation is the upper-left 3-by-3 block and translation is the last column. The last row is `[0,0,0,1]`. Rotations must be orthonormal with determinant +1. Scaling, shear and reflection are not supported; express them through dimensions or vertices instead.
- Entity-pose translations use the referenced frame's `length_unit`. Do not assume this is the same unit as primitive dimensions.
- Every frame specifies `parent_frame_id` and `transform_to_parent`. A root frame has `parent_frame_id:null`; its matrix transforms to overall model coordinates, with translation in the root frame's own unit. Child-frame translation uses **the parent frame's unit**.
- The converter first converts primitive dimensions to mm, converts matrix translations to mm, then composes rotations and translations. Different frame units do not scale rotations.
- Every primitive within one entity shares the same local origin and entity pose. Per-primitive `origin`, `axis` and `transform` are unsupported. For different placements, use separate entities and relationships, or explicit mesh vertices.

The example frame and pose demonstrate syntax only; they do not prescribe the placement of an actual model.

```json
{
  "coordinate_frames": [{
    "id": "model_frame",
    "description": "Explicit global modeling frame",
    "length_unit": "mm",
    "parent_frame_id": null,
    "transform_to_parent": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
    "source_evidence": []
  }],
  "pose": {
    "frame_id": "model_frame",
    "transform": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
    "source_evidence": []
  }
}
```

## Supported primitives

A primitive has the form `{"kind":"...","parameters":{...},"source_evidence":[...]}`. Only the parameters listed below are supported. Undefined parameters are not silently ignored: the affected primitive is omitted and an error is recorded. All lengths must be finite and positive.

| kind | parameters | Local geometry definition |
|---|---|---|
| `box` | `length_unit`, `extents:[x,y,z]` | Centered on the origin, with the specified full lengths along each axis |
| `cylinder` | `length_unit`, `radius`, `height` | Centered on the origin, +Z axis, z range `[-height/2,+height/2]` |
| `tube` | `length_unit`, `outer_radius`, `inner_radius`, `height` | Centered on the origin, +Z axis; `0 < inner_radius < outer_radius`; z range `[-height/2,+height/2]` |
| `extruded_polygon` | `length_unit`, `polygon:[[x,y],...]`, `height`, optional `holes:[[[x,y],...],...]` | Extrude an XY-plane section from `z=0` to `z=height` |
| `mesh` | `length_unit`, `vertices:[[x,y,z],...]`, `faces:[[i,j,k],...]` | Explicit vertices and triangles, with indices starting at 0 |

The polygon and each hole need at least three points. Repeating the first point at the end is optional. Self-intersections and invalid holes are recorded as errors without repair. Meshes must not contain repeated or out-of-range indices or zero-area triangles. Open mesh surfaces are allowed; solidity or watertightness is neither judged nor repaired. For extrusion in a negative direction, explicitly rotate and translate the entity pose.

## Conversion outputs and limitations

- `model.glb`: meters, Y-up, `[x,y,z] mm -> [x,z,-y]/1000 m`. Node and geometry extras preserve original `id`, `entity_id`, `label`, `kind` and `primitive_index`. Multiple primitives in one entity become separate nodes with the same `entity_id`.
- `model.dxf`: mm, Z-up, one `3DFACE` per triangle. Safe ASCII layer names are assigned per entity; `dxf_layer_map.json` maps them to original IDs, labels and kinds. XDATA records the mapping key and primitive index. This is not a CAD solid or editable feature history.
- `model.obj`: mm, Z-up. File comments record units and metadata, but OBJ does not enforce units, so set mm when importing.
- `model_input.json`: preserves the input JSON bytes unchanged. Conversion does not fill unspecified values or relationships.
- `unknown_review.json`: a reference list comparing fields declared in `unknowns` with populated fields syntactically. It is neither an inferred answer key nor a score and does not change rendered numeric values. Ambiguous field descriptions cannot be automatically interpreted, so their original text is retained.
- `conversion_report.json`: records conversion success, omission or error for each entity and primitive, mesh vertex and triangle counts, bounding boxes, format, axis and unit information, and output hashes. It computes no dimension-accuracy or structural-adequacy score.
- Cylinders and tubes both use 64 circumferential segments. Polygons are triangulated with mapbox-earcut. This approximates their screen and file representation while preserving the original parameters.
- Each primitive is limited to 500,000 vertices and 1,000,000 triangles. Oversized input is omitted without simplification. GLB uses float32 coordinates, so large global coordinates can reduce representational precision; the converter does not shift the origin automatically.

Unknown units, null poses, invalid frames, frame cycles and unsupported primitives cause the affected geometry to be omitted. Other valid primitives continue to convert. Duplicate entity or frame IDs, JSON or common-schema errors, and export failures are recorded as failures of the entire run. Items with `geometry:null` are preserved without geometry and are not errors by themselves.

## Execution

```powershell
python geometry_backend.py --input model.json --output new_run_geometry --schema common_output.schema.json
python -m unittest discover -s . -p test_geometry_backend.py -v
```

Python callers may use `convert_model(input_path, output_dir, schema_path=None)`. Result `status` is `ok`, `partial`, `failed` or `empty`; the respective CLI exit codes are 0, 2, 3 and 3. An output directory containing existing run artifacts is not reused. Dependencies are numpy, trimesh, shapely, mapbox-earcut and ezdxf; schema validation requires jsonschema. Supplied tests check generic geometry, units, matrices and file conversion, not the correctness of the target structure.
