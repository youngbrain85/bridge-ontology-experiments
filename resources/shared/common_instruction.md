# Instructions supplied identically to every generation condition

Use the supplied drawings and the material attached to this run to describe a 3D model of the requested scope. Return one JSON object conforming to the supplied `common_output.schema.json`. This is an intermediate representation passed to the geometry generator.

For each object, record an identifier, name, interpreted type, geometry representation and parameters, placement and sources. Record relationships between objects in `relations`. Type and relationship names are free strings; this output format provides no domain-specific type list or connection rules.

Give each object a unique `id`. Relationship `subject_id` and `object_id` values must reference IDs recorded in `entities`. When referencing a coordinate frame, record a frame with the same ID in `coordinate_frames`.

Record units and coordinate frames so that dimensions and coordinates can be interpreted. If using a placement matrix, write a homogeneous transformation acting on column vectors as a row-by-row 4-by-4 array. If placement is unclear, use `null` and `unknowns`; do not fill an identity matrix or zero coordinates as correct values without evidence. Choose geometry types and parameters that can be represented from the current material and state the representation used.

Distinguish directly read information from inferred information in the source `basis`. Sources must identify the supplied material and a location that can be found again, such as a drawing, page, view, detail or region. Even when explanatory knowledge helps interpret geometry, do not cite it as a substitute source for actual drawing dimensions.

When attachments conflict or required information is missing, record the affected items in `unknowns`. Do not present numbers or relationships unconfirmed by the drawings as established facts. Distinguish missing values from evidenced inferences.

Do not modify the original drawing files. Do not score the result or compare its quality with another condition.
