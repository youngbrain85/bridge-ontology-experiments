# Prepare a drawing packet

The bundled `SYNTHETIC_DEMO` packet is a small English software example. It is not an engineering reference or a reproduction of the private pilot drawings.

## Packet structure

Store private project material in the ignored `resources/private_data/` directory. The web preview requires input resources to remain inside `resources/`:

```text
resources/private_data/my_case/
  scope.md
  source_text.json
  provenance.json
  images/
    01_overview.png
    02_section.png
    03_detail.png
```

Use a short scope that identifies the requested component and boundaries. Keep the common task prompt brief. Do not put the expected assembly solution into the shared prompt when the experiment is intended to measure the effect of ontology knowledge.

`source_text.json` is included as text in the request. It can contain drawing text and dimension records with source identifiers. Preserve distinctions between an object's geometry, a drawing's display coordinates, and the coordinates chosen for a generated 3D model.

`provenance.json` records how the packet was produced and is frozen for audit. Images are sent in the exact configured order. The current engine does not automatically insert per-image captions; visible drawing IDs and any corresponding textual index must be part of the versioned input.

## Configuration

Copy `config.json` to ignored `config.local.json`, then replace the `cases` entry with paths to your packet. Keep the case object fields shown in the sample configuration: `case_id`, `scope`, `text`, `images`, `provenance`, and `used_for_ontology_development`.

For a CLI-only preparation:

```powershell
python -B -X utf8 experiment.py prepare --config config.local.json --output experiments/my_case_pilot
python -B -X utf8 experiment.py check --experiment experiments/my_case_pilot
```

These commands do not call a model API. The web application currently reads `config.json` as its preparation template. To use the packet in the web UI, apply the local configuration to that file and keep your private paths out of commits. Do not commit credentials into either configuration file.

## Changes and comparisons

Use a new protocol identifier when changing the input presentation. For example, test a caption-only variant separately from a new section-crop variant. Apply each variant identically to both providers and all A/B/C conditions. Retain old prepared experiments and hashes.

The included ontology was informed by development material. Mark cases used to develop it as `used_for_ontology_development: true`. Do not label such a case as held out. For a held-out study, prepare independent drawings and document how they were separated from ontology development.
