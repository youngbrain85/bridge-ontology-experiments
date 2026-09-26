# Changelog

## Frozen-runtime and review-export tests — 2026-09-26

- Add `verification/test_frozen_review.py`. The frozen-software loader is now covered directly: `verified_software` accepts a freshly frozen tree and rejects a modified frozen module, an unrecorded module placed in `software/`, and a modified manifest; `load_in_worker` executes the frozen source rather than the live modules; `check_in_process` runs the frozen engine in a child process and fails on tampering.
- The blinded review export is covered end to end on a synthetic box model: review slots carry only anonymous identifiers and status fields, the administrator key maps every scheduled run once with matching condition, case, and repetition, GLB metadata and OBJ object names are anonymized, a second export reuses conversions without re-running the backend and archives earlier copies, a review key that no longer matches the schedule is refused, invalid model JSON is reported without touching the original, and a missing backend or schema is refused before any write.
- Correct the worker test comment that cited a nonexistent "version compatibility suite".

## Credential store under cloud-synced folders — 2026-09-26

- Only reparse points that redirect the path (symbolic links, junctions, mount points; the name-surrogate tag bit) disable the saved-key store. Cloud-file placeholders such as OneDrive Files On-Demand and deduplicated files are also reparse points but resolve in place, so a repository kept under OneDrive no longer reports "Cannot load the saved key" for every provider. A reparse point whose tag cannot be read is still rejected.
- The web tests inject an in-memory credential store on non-Windows hosts, so the `/api/run` launch, stop, and key-isolation tests now run everywhere instead of failing without DPAPI. Windows CI keeps using the real store.

## Interrupted slot recovery — 2026-09-26

- Close an interrupted slot from its recorded turns when the runner died after the last API response was written but before the slot result was assembled. The final turn's response, text, and parsed model are promoted to the slot exactly as they would have been, the slot status keeps its recorded outcome (`completed`, `refused`, and so on), and `closed_on_resume: true` marks the recovery. A recorded clarification request whose fixed reply was never sent is closed as `interrupted_before_continuation`, or `clarification_limit_reached` when the ceiling had been reached. A started turn without a recorded response still yields `interrupted_outcome_unknown`; no request is ever re-sent.
- Discard a turn folder that a dead runner created before writing the slot's `attempt.json`. Nothing had been sent to the provider at that point, so the slot starts normally instead of failing forever with a misleading "already in use" message.
- `review_export.py` recognizes the new `interrupted_before_continuation` execution state. Experiments prepared before this change keep their frozen copies of both modules.

## Input handling and batch preparation — 2026-09-25

- Determine each drawing's media type from a fixed extension table (`.png`, `.jpg`/`.jpeg`, `.webp`) instead of the host `mimetypes` registry. Python 3.9 on Windows has no `.webp` entry, so preparing an experiment with a WEBP drawing failed after the experiment folder was created, with a message that did not name the cause. PNG and JPEG inputs are unaffected and produce identical requests.
- Release the batch name after a failed preparation: the web application now removes the `batches/<id>.reserved` marker and only the `experiments/<id>_mNN` folders that the failed preparation itself created. Previously the name stayed "already in use" forever and partial experiment folders were left behind. Successful preparations and pre-existing folders are untouched.
- Experiments prepared before this change keep their frozen copy of `experiment.py`.

## Geometry backend schema validation — 2026-09-23

- Resolve JSON Schema references through the `referencing` registry on jsonschema 4.18 and later, keeping the legacy `RefResolver` path only for the pinned jsonschema 4.4 environment. The previous code passed `resolver=` to newer releases, whose compatibility shim leaks `$id` scopes during the meta-schema pass, so every schema-validated conversion reported `failed` with a misleading "External schema reference is disabled" message.
- External schema references are still refused offline on both paths, now with a regression test; bundled Draft 2020-12 vocabulary meta-schemas resolve without any network access.
- The geometry backend version is unchanged; conversion output for valid input is byte-identical.
- Map `http.client.HTTPException` (a connection dropped while reading a long response body, or a malformed status line) to `transport_error_outcome_unknown` in the provider transport. Previously it escaped `send_request`, so the worker ended with a generic error and the slot was later synthesized as `interrupted_outcome_unknown` instead of pausing the batch as the failure policy states. Experiments prepared before this change keep their frozen copy of `providers.py`.

## English edition — 2026-09-22

- Publish the latest local source snapshot: experiment engine 0.4.0, web application 0.2.5, geometry backend 0.1.2, including run visibility updates.
- Translate repository documentation, application messages, prompts, and ontology resources into English in a separate edition.
- Preserve the 880-record B/C projection structure while assigning the translated input its own protocol identity.
- Replace private bridge inputs with an explicitly synthetic English demonstration packet and a hand-authored converter fixture.
- Remove machine-specific Python paths from the launcher.
- Add release checks for English text, safe repository contents, configuration paths, and ontology projection integrity.
- Record the September 17 diagnostic findings and unresolved limitations.

This is a source distribution and language update. It does not alter the original local application, historical frozen inputs, saved credentials, or experimental results, and does not claim a newly measured quality improvement.
