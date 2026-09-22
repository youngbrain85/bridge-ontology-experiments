# Diagnostic findings from the September pilot

This note summarizes a read-only investigation performed on September 17, 2026. It concerns one completed cable-anchorage/deck case, with A/B/C generated once by Astra and once by Claude Fable 5.1. It is not a provider benchmark or a manual correctness score. The original Korean drawings, prompts, and outputs remain in the private study archive.

## Request and conversion integrity

The inspected batch used each provider's `high` setting and a maximum of 128,000 output tokens. All six slots completed in one request, without clarification continuations or output truncation. Common text and all 12 image bytes and their order matched across providers within each condition. Public final answer extraction matched the saved model JSON.

| Condition | Astra output / reasoning tokens | Claude output / reasoning tokens |
| --- | ---: | ---: |
| A | 29,689 / 17,556 | 57,726 / 46,779 |
| B | 33,041 / 12,195 | 50,837 / 38,146 |
| C | 34,559 / 17,233 | 57,186 / 45,901 |

Reasoning is included in total output. The token counts do not support token exhaustion as the explanation for this batch's geometry differences. Earlier maximum-effort failures were a separate batch and must not be conflated with these completed results.

## Observed differences

- Claude C represented a plate's 370 mm transverse width as a vertical dimension and its 42 mm thickness as a transverse extrusion. The supplied detail and parts table supported the other orientation.
- The deliberate R280 gusset opening in the detail was absent from all three Claude outputs. Astra A/C contained an opening representation.
- Claude B/C explicitly stated that some details had been omitted. They were choices in the completed answer, not data lost by the parser.
- Claude A contained two missing triangular gusset faces. The converter allowed that open mesh.
- Astra B also had an error: a self-intersecting gusset polygon was excluded from conversion. A more convincing overall appearance did not imply that every Astra component was correct.

These observations identify specific output defects or differences, not the model's hidden internal reason for producing them. Two gussets in some outputs may reflect confusion about the E/W scope of a quantity of two; that interpretation still requires domain review.

## Input and display limitations

The packet contained 233,333 characters of drawing extraction, including CAD text-placement coordinates. Twelve images followed a large text block without individual captions. The standard deck cross-section had no dedicated detailed crop. These factors can make drawing-to-text association difficult, but their causal contribution was not isolated.

The viewer fits each full model independently. Long cable stubs can reduce the apparent size of the anchorage, and different axis choices can expose different sides under the same nominal camera direction. Display alignment should be separated from a geometry quality assessment.

## Follow-up experiments

Preserve existing results. Compare neutral input-presentation changes one at a time: image labels, a dedicated cross-section crop, and a separately documented extraction cleanup. Apply each variant to all A/B/C slots and both models. Keep ontology constraint checking or automatic repair as a separate treatment if added later.

The English release records these findings; it does not claim that those experimental or viewer improvements have already been implemented.
