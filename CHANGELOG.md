# Changelog

## Geometry backend schema validation — 2026-09-23

- Resolve JSON Schema references through the `referencing` registry on jsonschema 4.18 and later, keeping the legacy `RefResolver` path only for the pinned jsonschema 4.4 environment. The previous code passed `resolver=` to newer releases, whose compatibility shim leaks `$id` scopes during the meta-schema pass, so every schema-validated conversion reported `failed` with a misleading "External schema reference is disabled" message.
- External schema references are still refused offline on both paths, now with a regression test; bundled Draft 2020-12 vocabulary meta-schemas resolve without any network access.
- The geometry backend version is unchanged; conversion output for valid input is byte-identical.

## English edition — 2026-09-22

- Publish the latest local source snapshot: experiment engine 0.4.0, web application 0.2.5, geometry backend 0.1.2, including run visibility updates.
- Translate repository documentation, application messages, prompts, and ontology resources into English in a separate edition.
- Preserve the 880-record B/C projection structure while assigning the translated input its own protocol identity.
- Replace private bridge inputs with an explicitly synthetic English demonstration packet and a hand-authored converter fixture.
- Remove machine-specific Python paths from the launcher.
- Add release checks for English text, safe repository contents, configuration paths, and ontology projection integrity.
- Record the September 17 diagnostic findings and unresolved limitations.

This is a source distribution and language update. It does not alter the original local application, historical frozen inputs, saved credentials, or experimental results, and does not claim a newly measured quality improvement.
