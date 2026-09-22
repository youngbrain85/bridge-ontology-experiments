# Provider configuration

The application has separate adapters for the OpenAI Responses API and the Anthropic Messages API. The bundled `model_catalog.json` preserves the local research catalog, whose recorded verification date is September 7, 2026. This repository update does not claim a fresh live availability or pricing check.

## Reasoning controls

| Provider mode | Request fields | Interpretation |
| --- | --- | --- |
| OpenAI reasoning effort | `reasoning.effort` | The selected model's supported effort label |
| Claude adaptive thinking | `thinking.type=adaptive`, `output_config.effort` | Provider-specific effort guidance |
| Claude budgeted thinking | `thinking.type=enabled`, `thinking.budget_tokens` | A model-specific target thinking budget |
| Claude thinking disabled | `thinking.type=disabled` | No explicit extended-thinking budget |

The UI shows catalog-supported settings for each model. These settings are held constant within that model's A/B/C experiment. Identical label names across providers do not establish compute equivalence. Unsupported parameters are not silently dropped after an API failure.

The catalog contains OpenAI Astra, Sol, Terra, and Luna models and Claude Fable, Opus, Sonnet, and Haiku models. Account permissions and the provider's current API determine whether an identifier is accessible. Do not interpret a catalog entry as proof of access.

## Output budgets and continuations

The configured output cap includes reasoning and the final answer. Raising it does not require the model to use it. The default cap is 128,000, subject to the selected models' limits. Haiku 4.5 is configured with a smaller maximum; use a common cap at or below the lowest selected limit.

The model is asked to finish using its recommended interpretation. An explicit clarification request can receive the same fixed continuation text in that slot only. The default permits two such continuations. Invalid JSON, refusals, and incomplete responses are recorded, not automatically repaired or retried. A previously attempted slot is not automatically resent after an interruption.

## Credentials

The Windows UI encrypts keys with the current user's DPAPI and stores the encrypted files under ignored `webapp/runtime/credentials/`. The UI returns presence and availability status, not key values. Worker keys are passed through standard input, not command-line arguments. A key encrypted for one Windows account is not a portable credential backup.

## Reference documentation

- [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/)
- [OpenAI model documentation](https://developers.openai.com/api/docs/models)
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

Per-model references and the original verification date are retained in `model_catalog.json`. Check official documentation before changing the catalog or running a new model study.
