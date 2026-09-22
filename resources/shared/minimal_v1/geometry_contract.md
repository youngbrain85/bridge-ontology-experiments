# JSON input format for GLB conversion 0.2.0

Return one JSON object conforming to the supplied schema. The app converts this JSON to GLB.

Coordinates use a right-handed system with +Z upward. Length units are mm, cm or m. Each frame in coordinate_frames contains a unique id, length_unit, parent_frame_id and transform_to_parent. The root parent_frame_id is null.

Each item in entities contains a unique id, geometry.primitives and pose. pose requires frame_id and transform. Items without geometry may have null geometry and pose. Additional metadata, such as names and classifications, is optional and does not change geometry.

transform and transform_to_parent are 4-by-4 arrays written as four rows and multiplied by column vectors. The upper-left 3-by-3 block is rotation, the last column is translation and the last row is [0,0,0,1]. Pose translations use the referenced frame's unit, child-frame translations use the parent's unit and root-frame translations use the root's own unit. Rotations must be orthonormal with determinant +1. When both orthonormality and determinant errors are at most 0.0001, the common converter normalizes the small numeric deviation to the nearest rotation and preserves the original values and processing record. Larger deviations involving scale, shear or reflection are not permitted.

Each primitive has the form {"kind":"...","parameters":{...}}. Do not put parameters other than those listed below inside parameters. All primitives of one entity share the same origin and pose.

| kind | parameters | Local geometry |
|---|---|---|
| box | length_unit, extents:[x,y,z] | Centered on the origin; full lengths along the three axes |
| cylinder | length_unit, radius, height | Centered on the origin; Z axis; z=-height/2..height/2 |
| tube | length_unit, outer_radius, inner_radius, height | Centered on the origin; Z axis; 0<inner_radius<outer_radius |
| extruded_polygon | length_unit, polygon:[[x,y],...], height, optional holes:[[[x,y],...],...] | Extrude the XY section from z=0 to height |
| mesh | length_unit, vertices:[[x,y,z],...], faces:[[i,j,k],...] | Triangle indices start at 0 |

Dimensions must be finite and positive. A polygon needs at least three points and must not self-intersect. Each mesh face requires three distinct, valid vertex indices and nonzero area. Cylinders and tubes use 64 circumferential segments; polygons are triangulated. Each primitive is limited to 500,000 vertices and 1,000,000 faces. GLB uses meters and Y-up: [x,y,z] mm maps to [x,z,-y]/1000 m.
