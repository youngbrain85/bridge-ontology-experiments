# Experimental protocol

## Research question

For a fixed drawing packet, model, and generation procedure, how does additional bridge knowledge and its representation affect the generated 3D model?

- **A:** shared drawing packet and common instructions, without additional knowledge.
- **B:** the same common input plus prose statements from the ontology.
- **C:** the same common input plus structured facts from the ontology.

B and C are projections of the same 880 source records. This controls source membership and ordering. It does not prove that a model assigns equivalent meanings to the two representations or that their token counts are equal.

## Isolation and frozen inputs

Preparation copies the selected inputs, software, schema, and settings into each experiment and records hashes. Each model receives a separate experiment directory and worker process. A slot's first request contains no responses from another slot or model. Only an explicitly permitted continuation can contain earlier messages from that same slot.

The drawing image bytes and order, scope, common prompt, technical output contract, and converter are shared. Provider-specific request envelopes and internal image processing can differ. Model names and reasoning labels are recorded; equal labels do not imply equal computation.

The schedule seed randomizes the order of case-repetition blocks and the A/B/C order within each block. It is not a provider sampling seed. Repetition defaults to one and is a pilot setting, not a statistical sufficiency claim.

## Ontology implementation

Concepts, properties, definitions, aliases, axioms, and generation guidance are represented in the ontology resources. The model receives only one projection per condition. Archive files and alignment audit records are not additional model context.

The current generation procedure does not execute OWL reasoning or use SHACL constraints to reject or repair the generated model. Geometry validation checks schema and numerical geometry, not compliance with bridge design intent. Keep a future reasoning/validation/repair treatment separate from a context-only ontology experiment.

## Clarification handling

The engine prepends a fixed sentence to every prompt asking the model to use its recommended interpretation and finish; it is part of the frozen prompt files, not of the common instruction file shown in the input preview. If an explicit clarification question is recognized, the same fixed answer is sent to that slot. The continuation limit is fixed across a comparison. Report actual request counts, because a common limit does not force the same number of calls: each slot records its `api_turns` in `runs/<run_id>/result.json`, and per-condition totals must currently be summed from those files.

The implementation does not retry malformed final JSON, refusals, or token-limit failures as repair prompts. Preserve these outcomes in the study record. Re-running a failed condition is a new experimental attempt, not a replacement for the failure.

## Evaluation

The researcher inspects the 3D models and assigns scores. Useful separate criteria include component coverage, dimensions, spatial orientation, connectivity, openings, and unsupported assumptions. Counts of tokens, entities, or meshes are diagnostics, not quality scores.

The comparison screen displays the condition mapping. Review labels such as S001 are local to an experiment and cannot be compared across models without the mapping. If blinded scoring is required, organize it separately and predefine when the mapping is revealed.

## English edition

The original local study used Korean prompts and ontology text. This repository contains an English translation and a synthetic input packet. It uses the `deck_assembly_minimal_en_v1` input protocol and a separately identified English ontology version. Historical Korean frozen experiments remain outside the repository and are not rewritten.

Changing a language, drawing crop, caption, model setting, or ontology version can affect outcomes. Treat such changes as new study inputs and maintain separate hashes. Do not attribute a difference between old and new protocols solely to the presence of ontology knowledge.
