# -*- coding: utf-8 -*-
"""Frozen multi-provider A/B/C experiments with isolated, bounded slot conversations."""
import argparse
import base64
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
import providers

VERSION = "0.4.0"
ENDPOINTS = {"openai": "https://api.openai.com/v1/responses",
             "anthropic": "https://api.anthropic.com/v1/messages"}
CONDITIONS = ("A", "B", "C")
KNOWLEDGE_START = "<additional_knowledge>\n"
KNOWLEDGE_END = "\n</additional_knowledge>"
CODE_FILES = ("experiment.py", "knowledge_builder.py", "review_export.py", "providers.py", "model_catalog.json")
# Fixed by file extension so the media type sent to providers never depends on the host mimetypes registry.
IMAGE_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
AUTONOMOUS_INSTRUCTION = (
    "Complete the generation using your recommended interpretation without asking follow-up questions."
)
CONTINUATION_TEXT = (
    "Continue with your recommended interpretation and return one model JSON object in the specified format."
)


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(Path(path).read_bytes())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("IDs must use 1-80 ASCII letters, digits, underscores or hyphens.")
    return value


def resolve_input(base, value):
    return (Path(base) / value).resolve()


def contained(base, relative):
    base = Path(base).resolve()
    path = (base / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise ValueError("Path escapes experiment directory: " + str(relative))
    return path


def runtime_info():
    versions = {}
    for name in ("numpy", "trimesh", "ezdxf", "jsonschema", "shapely", "manifold3d", "mapbox-earcut"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "platform": platform.platform(), "packages": versions}


def validate_config(cfg):
    allowed = {"version", "provider", "model", "model_version_note", "reasoning_effort",
               "thinking_budget_tokens", "auto_continue_limit", "max_output_tokens",
               "temperature", "top_p", "repetitions", "randomization_seed", "timeout_seconds",
               "study_role", "knowledge_source", "common_instruction", "output_schema",
               "geometry_contract", "backend_dir", "cases", "input_protocol_version"}
    if set(cfg) - allowed:
        raise ValueError("Unrecognized configuration keys: " + str(sorted(set(cfg) - allowed)))
    if "input_protocol_version" in cfg:
        safe_id(cfg["input_protocol_version"])
    for name in ("model", "model_version_note", "study_role"):
        if not isinstance(cfg.get(name), str) or not cfg[name].strip():
            raise ValueError("Missing string: " + name)
    if cfg.get("provider") not in ENDPOINTS:
        raise ValueError("provider must be openai or anthropic.")
    if cfg.get("reasoning_effort") is not None and (
            not isinstance(cfg["reasoning_effort"], str) or not cfg["reasoning_effort"].strip()):
        raise ValueError("reasoning_effort must be null or a nonempty string.")
    if cfg.get("thinking_budget_tokens") is not None and (
            type(cfg["thinking_budget_tokens"]) is not int or cfg["thinking_budget_tokens"] < 1):
        raise ValueError("thinking_budget_tokens must be null or a positive integer.")
    cfg.setdefault("auto_continue_limit", 2)
    cfg.setdefault("thinking_budget_tokens", None)
    cfg.setdefault("reasoning_effort", None)
    if type(cfg["auto_continue_limit"]) is not int or not 0 <= cfg["auto_continue_limit"] <= 5:
        raise ValueError("auto_continue_limit must be an integer in [0, 5].")
    for name in ("max_output_tokens", "repetitions", "timeout_seconds"):
        if type(cfg.get(name)) is not int or cfg[name] < 1:
            raise ValueError("Positive integer required: " + name)
    if type(cfg.get("randomization_seed")) is not int:
        raise ValueError("randomization_seed must be an integer (schedule only, not a model seed).")
    if cfg["study_role"] not in ("development_pilot", "held_out"):
        raise ValueError("study_role must be development_pilot or held_out.")
    if not isinstance(cfg.get("cases"), list) or not cfg["cases"]:
        raise ValueError("At least one case is required.")
    ids = set()
    for case in cfg["cases"]:
        if set(case) != {"case_id", "scope", "text", "images", "used_for_ontology_development", "provenance"}:
            raise ValueError("Case must contain case_id, scope, text, images, provenance and used_for_ontology_development.")
        case_id = safe_id(case["case_id"])
        if case_id in ids:
            raise ValueError("Duplicate case ID.")
        ids.add(case_id)
        if type(case["used_for_ontology_development"]) is not bool:
            raise ValueError("Declare whether each case informed ontology development.")
        if cfg["study_role"] == "held_out" and case["used_for_ontology_development"]:
            raise ValueError("A development case cannot be labeled held_out.")
        if not isinstance(case["images"], list) or not case["images"]:
            raise ValueError("Each case needs an ordered image list.")
    # Unsupported parameters are never silently dropped or changed after a failure.
    if cfg.get("temperature") is not None and (
            type(cfg["temperature"]) not in (int, float) or not (0 <= cfg["temperature"] <= 2)):
        raise ValueError("temperature must be null or in [0, 2].")
    if cfg.get("top_p") is not None and (
            type(cfg["top_p"]) not in (int, float) or not (0 < cfg["top_p"] <= 1)):
        raise ValueError("top_p must be null or in (0, 1].")
    providers.validate_model_settings(cfg)


def make_schedule(cfg):
    rng = random.Random(cfg["randomization_seed"])
    blocks = [(case["case_id"], repetition) for case in cfg["cases"]
              for repetition in range(1, cfg["repetitions"] + 1)]
    rng.shuffle(blocks)
    runs = []
    for block, (case_id, repetition) in enumerate(blocks, 1):
        conditions = list(CONDITIONS)
        rng.shuffle(conditions)
        for condition in conditions:
            runs.append({"run_id": "r%04d" % (len(runs) + 1), "case_id": case_id,
                         "repetition": repetition, "condition": condition, "block": block})
    return {"method": "randomized case-repetition blocks; random A/B/C order within each block",
            "seed": cfg["randomization_seed"], "model_seed": None, "runs": runs}


def prepare(config_path, experiment_dir):
    from knowledge_builder import build_knowledge
    config_path = Path(config_path).resolve()
    exp = Path(experiment_dir).resolve()
    cfg = read_json(config_path)
    validate_config(cfg)
    base = config_path.parent
    # Validate paths before creating an experiment. The source files are copied, never edited.
    paths = [cfg[k] for k in ("knowledge_source", "common_instruction", "output_schema", "geometry_contract")]
    paths += [case[k] for case in cfg["cases"] for k in ("scope", "text", "provenance")]
    paths += [image for case in cfg["cases"] for image in case["images"]]
    for name in paths:
        if not resolve_input(base, name).is_file():
            raise ValueError("Input file not found: " + name)
    for name in CODE_FILES:
        if not (Path(__file__).parent / name).is_file():
            raise ValueError("Missing runner module: " + name)
    backend = resolve_input(base, cfg["backend_dir"]) / "geometry_backend.py"
    if not backend.is_file():
        raise ValueError("Missing geometry_backend.py")
    exp.mkdir(parents=True, exist_ok=False)
    for folder in ("frozen/shared", "frozen/knowledge", "frozen/cases", "software/backend", "runs", "admin"):
        (exp / folder).mkdir(parents=True, exist_ok=True)
    for key, name in (("common_instruction", "common_instruction.md"), ("output_schema", "common_output.schema.json"),
                      ("geometry_contract", "geometry_contract.md")):
        shutil.copy2(resolve_input(base, cfg[key]), exp / "frozen/shared" / name)
    alignment = build_knowledge(resolve_input(base, cfg["knowledge_source"]), exp / "frozen/knowledge")
    normalized = copy.deepcopy(cfg)
    normalized.update({"common_instruction": "frozen/shared/common_instruction.md",
                       "output_schema": "frozen/shared/common_output.schema.json",
                       "geometry_contract": "frozen/shared/geometry_contract.md",
                       "knowledge_source": "frozen/knowledge/source_facts.json", "backend_dir": "software/backend"})
    source_records = []
    for original, case in zip(cfg["cases"], normalized["cases"]):
        target = exp / "frozen/cases" / case["case_id"]
        target.mkdir()
        for key, name in (("scope", "scope.md"), ("text", "source_text.json"), ("provenance", "provenance.json")):
            src = resolve_input(base, original[key])
            shutil.copy2(src, target / name)
            case[key] = (target / name).relative_to(exp).as_posix()
            source_records.append({"original_path": str(src), "sha256": sha(src), "role": key})
        case["images"] = []
        for index, name in enumerate(original["images"]):
            src = resolve_input(base, name)
            if src.suffix.lower() not in IMAGE_MEDIA_TYPES:
                raise ValueError("Use PNG/JPEG/WEBP image files.")
            dest = target / ("image_%02d" % index + src.suffix.lower())
            shutil.copy2(src, dest)
            case["images"].append(dest.relative_to(exp).as_posix())
            source_records.append({"original_path": str(src), "sha256": sha(src), "role": "image"})
    for name in CODE_FILES:
        shutil.copy2(Path(__file__).parent / name, exp / "software" / name)
    shutil.copy2(backend, exp / "software/backend/geometry_backend.py")
    write_json(exp / "config.json", normalized)
    write_json(exp / "schedule.json", make_schedule(normalized))
    write_json(exp / "admin/source_provenance.json", source_records)
    integrity = input_integrity(exp, write_prompts=True)
    write_json(exp / "input_integrity.json", integrity)
    immutable = [exp / "config.json", exp / "schedule.json", exp / "input_integrity.json"]
    immutable += sorted(p for tree in ("frozen", "software") for p in (exp / tree).rglob("*") if p.is_file())
    manifest = {"version": VERSION, "created_utc": now(), "study_role": cfg["study_role"],
                "grader": "human only; operational checks are not accuracy scores",
                "provider": cfg["provider"], "endpoint": ENDPOINTS[cfg["provider"]],
                "retries_per_run": 0, "repair_calls": 0,
                "auto_continue_limit": cfg["auto_continue_limit"],
                "continuation_text": CONTINUATION_TEXT,
                "continuation_policy": "Only explicit clarification; same slot, fixed reply, never repair model JSON",
                "failure_policy": "HTTP/transport errors pause batch; never retry an attempted run",
                "input_protocol_version": cfg.get("input_protocol_version", VERSION),
                "postprocessing": "same geometry adapter for all conditions; near_rotation_so3_v1 corrects only bounded numeric rotation deviations and logs original/corrected matrices; no semantic, dimension, topology or schema repairs",
                "formal_reasoner": False, "knowledge_alignment": alignment,
                "model_version_note": cfg["model_version_note"],
                "runtime": runtime_info(), "files": {p.relative_to(exp).as_posix(): sha(p) for p in immutable}}
    write_json(exp / "manifest.json", manifest)
    (exp / "manifest.sha256").write_text(sha(exp / "manifest.json") + "\n", encoding="ascii")
    slots = len(read_json(exp / "schedule.json")["runs"])
    return {"experiment": str(exp), "scheduled_calls": slots, "scheduled_slots": slots,
            "max_api_turns": slots * (1 + cfg["auto_continue_limit"]),
            "integrity": integrity["passed"], "status": "prepared_no_api_calls"}


def common_prompt(exp, case):
    exp = Path(exp)
    parts = [AUTONOMOUS_INSTRUCTION, contained(exp, case["scope"]).read_text(encoding="utf-8")]
    for tag, path in (("common_instruction", "frozen/shared/common_instruction.md"),
                      ("output_schema", "frozen/shared/common_output.schema.json"),
                      ("geometry_grammar", "frozen/shared/geometry_contract.md"),
                      ("source_text", case["text"])):
        parts.append("<%s>\n%s\n</%s>" % (tag, contained(exp, path).read_text(encoding="utf-8"), tag))
    common = "\n\n".join(parts)
    if KNOWLEDGE_START in common or KNOWLEDGE_END in common:
        raise ValueError("Reserved knowledge delimiter appears in common input.")
    return common


def prompt_for(exp, case, condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown condition")
    knowledge = "" if condition == "A" else (Path(exp) / "frozen/knowledge" / (condition + ".txt")).read_text(encoding="utf-8")
    if KNOWLEDGE_START in knowledge or KNOWLEDGE_END in knowledge:
        raise ValueError("Reserved delimiter appears in knowledge.")
    return common_prompt(exp, case) + "\n\n" + KNOWLEDGE_START + knowledge + KNOWLEDGE_END + "\n\nReturn one JSON object.\n"


def build_payload(exp, case, condition):
    cfg = read_json(Path(exp) / "config.json")
    images = []
    for relative in case["images"]:
        path = contained(exp, relative)
        mime = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if mime is None:
            raise ValueError("Unsupported image file: " + relative)
        images.append({"media_type": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
    payload = providers.build_request(cfg, prompt_for(exp, case, condition), images)
    assert_isolated_payload(payload)
    return payload


def assert_isolated_payload(payload):
    if any(key in payload for key in ("previous_response_id", "conversation", "session")):
        raise ValueError("Shared server-side conversation state is forbidden.")
    if "input" in payload:
        allowed = {"model", "instructions", "input", "store", "stream", "tools", "tool_choice", "truncation",
                   "reasoning", "max_output_tokens", "text", "service_tier", "temperature", "top_p", "include"}
        messages = payload["input"]
        if payload.get("tools") != [] or payload.get("tool_choice") != "none" or payload.get("store") is not False:
            raise ValueError("Tools and response storage must be disabled.")
        if payload.get("truncation") != "disabled":
            raise ValueError("Silent truncation is not part of this protocol.")
    else:
        allowed = {"model", "system", "messages", "max_tokens", "thinking", "output_config",
                   "stream", "tools", "tool_choice", "temperature", "top_p"}
        messages = payload.get("messages", [])
        if payload.get("tools", []) or payload.get("tool_choice") not in (None, {"type": "none"}):
            raise ValueError("Tools must be disabled.")
    if set(payload) - allowed or len(messages) != 1:
        raise ValueError("Unexpected request field/history.")
    if messages[0].get("role") != "user":
        raise ValueError("Unexpected conversation role.")
    if payload.get("stream", False) is not False:
        raise ValueError("Streaming is not part of this protocol.")


def payload_prompt(payload):
    messages = payload["input"] if "input" in payload else payload["messages"]
    return messages[0]["content"][0]["text"]


def mask_knowledge(payload):
    result = copy.deepcopy(payload)
    messages = result["input"] if "input" in result else result["messages"]
    text = messages[0]["content"][0]["text"]
    before, rest = text.split(KNOWLEDGE_START)
    _, after = rest.split(KNOWLEDGE_END)
    messages[0]["content"][0]["text"] = before + KNOWLEDGE_START + KNOWLEDGE_END + after
    return result


def input_integrity(exp, write_prompts=False):
    exp = Path(exp)
    cfg = read_json(exp / "config.json")
    rows = []
    for case in cfg["cases"]:
        payloads = {condition: build_payload(exp, case, condition) for condition in CONDITIONS}
        common_hashes = {digest(dumps(mask_knowledge(payload)).encode("utf-8")) for payload in payloads.values()}
        if len(common_hashes) != 1:
            raise ValueError("Conditions differ outside the intended knowledge block.")
        for condition, payload in payloads.items():
            prompt = payload_prompt(payload)
            if write_prompts:
                path = exp / "frozen/cases" / case["case_id"] / (condition + "_prompt.txt")
                path.write_text(prompt, encoding="utf-8")
            rows.append({"case_id": case["case_id"], "condition": condition,
                         "payload_sha256": digest(dumps(payload).encode("utf-8")),
                         "prompt_sha256": digest(prompt.encode("utf-8")), "prompt_utf8_bytes": len(prompt.encode("utf-8")),
                         "image_sha256_in_order": [sha(contained(exp, name)) for name in case["images"]],
                         "knowledge_masked_payload_sha256": next(iter(common_hashes)),
                         "input_tokens": None, "token_note": "Actual text+image/cached/reasoning usage is recorded from API response."})
    return {"passed": True, "sole_input_difference": "additional_knowledge block", "rows": rows}


def verify_frozen(exp):
    exp = Path(exp)
    if sha(exp / "manifest.json") != (exp / "manifest.sha256").read_text(encoding="ascii").strip():
        raise ValueError("Manifest changed after freezing.")
    manifest = read_json(exp / "manifest.json")
    changed = [name for name, expected in manifest["files"].items()
               if not contained(exp, name).is_file() or sha(contained(exp, name)) != expected]
    if changed:
        raise ValueError("Frozen files changed: " + ", ".join(changed))
    for name in CODE_FILES:
        if sha(Path(__file__).parent / name) != manifest["files"]["software/" + name]:
            raise ValueError("Runner code differs from the frozen version: " + name)
    actual = input_integrity(exp)
    if actual != read_json(exp / "input_integrity.json"):
        raise ValueError("Request reconstruction differs from frozen integrity record.")
    return {"passed": True, "immutable_files": len(manifest["files"]), "case_count": len(read_json(exp / "config.json")["cases"])}


TransportError = providers.TransportError


def normalized_api_key(value):
    if not isinstance(value, str):
        raise ValueError("API key must be entered as text; its value is not logged.")
    value = value.strip()
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("API key contains whitespace or unsupported characters; re-enter it locally.")
    return value


def parse_model_text(raw_text):
    text = raw_text.strip()
    # A whole-response Markdown fence may be unwrapped; never salvage partial JSON.
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    def reject_constant(value):
        raise ValueError("Non-finite JSON number: " + value)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key.")
            result[key] = value
        return result
    value = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("Expected a single JSON object.")
    return value


def clarification_kind(text, model=None):
    """Recognize explicit questions, never interpret or repair a model object."""
    if model is not None:
        allowed = {"type", "status", "question", "questions", "clarification",
                   "needs_clarification", "message", "options", "reason",
                   "recommendation", "recommended_option", "recommended_approach", "choices"}
        model_fields = {"schema_version", "model_id", "entities", "relations", "unknowns",
                        "coordinate_frames", "geometry", "geometries", "model", "schema"}
        def has_model_fields(value):
            if isinstance(value, dict):
                return bool(set(value) & model_fields) or any(has_model_fields(x) for x in value.values())
            return isinstance(value, list) and any(has_model_fields(x) for x in value)
        if not model or set(model) - allowed or has_model_fields(model):
            return None
        markers = {"clarification", "clarification_required", "needs_clarification",
                   "question", "awaiting_confirmation", "confirmation_required"}
        explicit = (any(isinstance(model.get(key), str) and model[key] in markers for key in ("type", "status"))
                    or model.get("needs_clarification") is True
                    or any(key in model for key in ("question", "questions", "clarification")))
        questions = [model.get(key) for key in ("question", "questions", "clarification", "message")]
        substantive = any((isinstance(value, str) and value.strip()) or
                          (isinstance(value, list) and any(isinstance(x, str) and x.strip() for x in value))
                          for value in questions)
        return "explicit_clarification_json" if explicit and substantive else None
    text = text.strip()
    if not text or len(text) > 4000 or any(token in text for token in ("{", "}", "\x60\x60\x60", "<svg", "entities")):
        return None
    question = re.search(
        r"(?im)(?:^|\n|\.\s+)(?:would you like|shall I|should I|can I|do you want|"
        r"can you (?:confirm|clarify)|could you (?:confirm|clarify)|which (?:option|approach)|"
        r"please (?:confirm|choose|clarify|select))\b", text)
    interrogative = re.search(
        r"(?im)(?:^|\n)(?:what|which|how|where|when|is|are|would|should|could|can|do|does|may|shall)"
        r"\b[^\n?]{0,500}\?", text)
    korean = re.search(r"(?:\uc9c4\ud589|\uc120\ud0dd|\ud655\uc815|\uc0ac\uc6a9|\uac00\uc815|\uc0dd\uc131|\uc801\uc6a9|\ucc98\ub9ac).{0,70}(?:\ud560\uae4c\uc694\s*\??|\ud574\ub3c4 \ub420\uae4c\uc694\s*\??|\ud574\ub3c4 \ub418\ub098\uc694\s*\??)|"
                       r"(?:\ud655\uc778|\uc120\ud0dd|\uacb0\uc815).{0,35}(?:\ubd80\ud0c1|\ud574\uc8fc\uc138\uc694|\ud574 \uc8fc\uc138\uc694)|"
                       r"(?:\uc5b4\ub290|\uc5b4\ub5a4|\ubb34\uc5c7).{0,70}(?:\uc120\ud0dd|\uc0ac\uc6a9|\uc801\uc6a9).{0,20}(?:\uae4c\uc694|\ud569\ub2c8\uae4c)\s*\??|"
                       r"(?:\uae4c\uc694|\uc778\uac00\uc694|\ub098\uc694|\uc2b5\ub2c8\uae4c|\ub429\ub2c8\uae4c|\uc77c\uae4c\uc694)\s*[?？]", text)
    return "explicit_plain_question" if question or interrogative or korean else None


def consume_response(run_dir, body, metadata, provider="openai"):
    run_dir = Path(run_dir)
    (run_dir / "response.raw.json").write_bytes(body)
    result = {"finished_utc": now(), **metadata, "response_sha256": digest(body), "status": "invalid_api_response"}
    try:
        parsed = providers.parse_response(provider, body)
        response = parsed.get("raw") or {}
        text = parsed.get("text") or ""
        (run_dir / "raw_response.txt").write_text(text, encoding="utf-8")
        result.update({"response_id": response.get("id"), "returned_model": parsed.get("returned_model"),
                       "provider": provider, "provider_status": parsed.get("provider_status"),
                       "usage": parsed.get("usage"), "status": parsed["status"],
                       "service_tier": response.get("service_tier"), "system_fingerprint": response.get("system_fingerprint"),
                       "incomplete_details": response.get("incomplete_details"),
                       "refusal": parsed["status"] == "refused"})
        if parsed["status"] == "completed":
            try:
                model = parse_model_text(text)
            except (ValueError, TypeError):
                kind = clarification_kind(text)
                result["status"] = "clarification_requested" if kind else "invalid_model_json"
            else:
                kind = clarification_kind(text, model)
                if kind:
                    result["status"] = "clarification_requested"
                else:
                    write_json(run_dir / "model_input.json", model)
                    result.update({"status": "completed", "model_sha256": sha(run_dir / "model_input.json")})
            if kind:
                result["clarification_kind"] = kind
    except (ValueError, TypeError, AttributeError, KeyError):
        result["status"] = "invalid_api_response"
    write_json(run_dir / "result.json", result)
    return result


def aggregate_usage(turn_results):
    usages = [row.get("usage") for row in turn_results if isinstance(row.get("usage"), dict)]
    if not usages:
        return None
    def total(path):
        values = []
        for usage in usages:
            value = usage
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if type(value) in (int, float):
                values.append(value)
        return sum(values) if values else None
    return {**{key: total((key,)) for key in ("input_tokens", "output_tokens", "total_tokens")},
            "input_tokens_details": {"cached_tokens": total(("input_tokens_details", "cached_tokens"))},
            "output_tokens_details": {"reasoning_tokens": total(("output_tokens_details", "reasoning_tokens"))}}


def assert_continuation(previous, updated):
    history = "input" if "input" in previous else "messages"
    old_settings = {key: value for key, value in previous.items() if key != history}
    new_settings = {key: value for key, value in updated.items() if key != history}
    if old_settings != new_settings or updated[history][:len(previous[history])] != previous[history]:
        raise ValueError("Continuation changed fixed settings or prior slot input.")
    last = updated[history][-1]
    content = last.get("content")
    text = content if isinstance(content, str) else (
        content[0].get("text") if isinstance(content, list) and len(content) == 1 else None)
    if last.get("role") != "user" or text != CONTINUATION_TEXT:
        raise ValueError("Continuation must contain the fixed reply only.")


def finish_slot(run_dir, last_turn, turn_results, status=None):
    final = copy.deepcopy(turn_results[-1])
    if status:
        final["status"] = status
    final.update({"api_turns": len(turn_results), "continuation_count": max(0, len(turn_results) - 1),
                  "usage": aggregate_usage(turn_results),
                  "elapsed_seconds": round(sum(row.get("elapsed_seconds") or 0 for row in turn_results), 3),
                  "turns": [{"turn": index, "status": row["status"],
                             "request_sha256": row.get("request_sha256"),
                             "response_sha256": row.get("response_sha256")}
                            for index, row in enumerate(turn_results)]})
    for name in ("response.raw.json", "raw_response.txt", "model_input.json"):
        if (last_turn / name).is_file():
            shutil.copy2(last_turn / name, run_dir / name)
    write_json(run_dir / "result.json", final)
    return final


def run_batch(experiment_dir, api_key=None, transport=None, max_new_calls=None, test_mode=False, stop_file=None):
    exp = Path(experiment_dir).resolve()
    verify_frozen(exp)
    cfg = read_json(exp / "config.json")
    if max_new_calls is not None and (type(max_new_calls) is not int or max_new_calls < 1):
        raise ValueError("max_new_calls is a positive slot limit or null.")
    stop_file = Path(stop_file) if stop_file is not None else None
    def stopped():
        return stop_file is not None and stop_file.exists()
    if transport is None:
        if test_mode:
            raise ValueError("test_mode requires an injected offline transport; live network is forbidden.")
    elif not test_mode:
        raise ValueError("Injected transports are restricted to explicitly labeled test experiments.")
    elif transport is providers.send_request:
        raise ValueError("Live transport cannot be used for offline tests.")
    mode = "offline_test_only" if test_mode else "live_api"
    mode_path = exp / "execution_mode.json"
    if mode_path.exists() and read_json(mode_path)["mode"] != mode:
        raise ValueError("Cannot mix mock and live outcomes in an experiment.")
    key_env = "OPENAI_API_KEY" if cfg["provider"] == "openai" else "ANTHROPIC_API_KEY"
    api_key = "OFFLINE_TEST_NO_CREDENTIAL" if test_mode else (api_key if api_key is not None else os.environ.get(key_env, ""))
    api_key = normalized_api_key(api_key)
    lock = exp / "run.lock"
    try:
        lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("A runner lock exists. Check the running process; do not start a concurrent runner.")
    os.write(lock_fd, dumps({"pid": os.getpid(), "started_utc": now()}).encode("utf-8"))
    os.close(lock_fd)
    attempted = 0
    api_turns = 0
    results = []
    def batch_result(status, **details):
        value = {"status": status, "new_calls": attempted, "new_slots": attempted,
                 "new_api_turns": api_turns, "results": results, **details}
        write_json(exp / "batch_status.json", value)
        return value
    try:
        write_json(mode_path, {"mode": mode})
        violation = exp / "admin/protocol_violation.json"
        if violation.exists():
            raise ValueError("Protocol deviation was recorded; preserve this experiment and prepare a new one.")
        runtime_path = exp / "admin/execution_runtime.json"
        runtime = runtime_info()
        if runtime_path.exists() and read_json(runtime_path) != runtime:
            raise ValueError("Execution runtime changed; use the original environment or prepare a new experiment.")
        if not runtime_path.exists():
            write_json(runtime_path, runtime)
        cases = {case["case_id"]: case for case in cfg["cases"]}
        for row in read_json(exp / "schedule.json")["runs"]:
            run_dir = exp / "runs" / safe_id(row["run_id"])
            if (run_dir / "attempt.json").exists():
                # Neither the initial request nor an interrupted continuation is ever resumed.
                if not (run_dir / "result.json").exists():
                    turn_dirs = sorted((run_dir / "turns").glob("turn_*"))
                    known = [read_json(p / "result.json") for p in turn_dirs if (p / "result.json").is_file()]
                    write_json(run_dir / "result.json", {
                        "status": "interrupted_outcome_unknown", "finished_utc": now(),
                        "api_turns": sum((p / "attempt.json").exists() for p in turn_dirs),
                        "continuation_count": max(0, sum((p / "attempt.json").exists() for p in turn_dirs) - 1),
                        "usage": aggregate_usage(known)})
                continue
            if max_new_calls is not None and attempted >= max_new_calls:
                break
            if stopped():
                return batch_result("stopped_after_current_request")
            verify_frozen(exp)
            payload = build_payload(exp, cases[row["case_id"]], row["condition"])
            payload_bytes = dumps(payload).encode("utf-8")
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "request.json").write_bytes(payload_bytes)
            if stopped():
                return batch_result("stopped_after_current_request")
            turn_results = []
            for turn in range(cfg["auto_continue_limit"] + 1):
                if turn and stopped():
                    result = finish_slot(run_dir, turn_dir, turn_results, "stopped_before_continuation")
                    results.append({"run_id": row["run_id"], "status": result["status"]})
                    return batch_result("stopped_after_current_request")
                verify_frozen(exp)
                payload_bytes = dumps(payload).encode("utf-8")
                if stopped():
                    if turn_results:
                        result = finish_slot(run_dir, turn_dir, turn_results, "stopped_before_continuation")
                        results.append({"run_id": row["run_id"], "status": result["status"]})
                    return batch_result("stopped_after_current_request")
                turn_dir = run_dir / "turns" / ("turn_%02d" % turn)
                turn_dir.mkdir(parents=True)
                (turn_dir / "request.json").write_bytes(payload_bytes)
                request_hash = digest(payload_bytes)
                client_request_id = str(uuid.uuid4())
                if turn == 0:
                    write_json(run_dir / "attempt.json", {"started_utc": now(), "mode": mode,
                               "provider": cfg["provider"], "endpoint": ENDPOINTS[cfg["provider"]],
                               "client_request_id": client_request_id, "request_sha256": request_hash,
                               "attempt_number": 1, "runtime": runtime})
                    attempted += 1
                write_json(turn_dir / "attempt.json", {"started_utc": now(), "mode": mode, "turn": turn,
                           "provider": cfg["provider"], "endpoint": ENDPOINTS[cfg["provider"]],
                           "client_request_id": client_request_id, "request_sha256": request_hash,
                           "attempt_number": 1})
                api_turns += 1
                start = time.monotonic()
                print(dumps({"event": "call_started", "run_id": row["run_id"], "mode": mode, "turn": turn}), flush=True)
                try:
                    if test_mode:
                        body, metadata = transport(payload_bytes, api_key, cfg["timeout_seconds"], client_request_id)
                    else:
                        body, metadata = providers.send_request(
                            cfg["provider"], payload_bytes, api_key, cfg["timeout_seconds"], client_request_id)
                    metadata = {key: metadata.get(key) for key in ("http_status", "request_id")}
                    metadata.update({"elapsed_seconds": round(time.monotonic() - start, 3),
                                     "mode": mode, "turn": turn, "request_sha256": request_hash})
                    turn_result = consume_response(turn_dir, body, metadata, cfg["provider"])
                except TransportError as exc:
                    turn_result = {"status": exc.category, "http_status": exc.http_status, "request_id": exc.request_id,
                                   "request_sha256": request_hash, "turn": turn,
                                   "elapsed_seconds": round(time.monotonic() - start, 3), "finished_utc": now(), "mode": mode}
                    write_json(turn_dir / "result.json", turn_result)
                    turn_results.append(turn_result)
                    result = finish_slot(run_dir, turn_dir, turn_results)
                    results.append({"run_id": row["run_id"], "status": result["status"]})
                    return batch_result("paused_after_transport_failure", last_run=row["run_id"])
                turn_results.append(turn_result)
                actual_model = turn_result.get("returned_model")
                observed_path = exp / "admin/observed_model.json"
                if actual_model:
                    if observed_path.exists() and read_json(observed_path)["returned_model"] != actual_model:
                        previous = read_json(observed_path)["returned_model"]
                        write_json(violation, {"reason": "returned_model_changed", "previous": previous,
                                   "current": actual_model, "run_id": row["run_id"], "turn": turn, "recorded_utc": now()})
                        turn_result["protocol_deviation"] = "returned_model_changed"
                        turn_result["generation_status_before_deviation"] = turn_result["status"]
                        turn_result["status"] = "protocol_deviation"
                        write_json(turn_dir / "result.json", turn_result)
                        result = finish_slot(run_dir, turn_dir, turn_results)
                        results.append({"run_id": row["run_id"], "status": result["status"]})
                        return batch_result("paused_after_model_change", last_run=row["run_id"])
                    if not observed_path.exists():
                        write_json(observed_path, {"provider": cfg["provider"], "returned_model": actual_model,
                                   "note": "Observed ID, not proof of immutable weights."})
                print(dumps({"event": "call_finished", "run_id": row["run_id"],
                             "status": turn_result["status"], "turn": turn}), flush=True)
                if turn_result["status"] != "clarification_requested":
                    result = finish_slot(run_dir, turn_dir, turn_results)
                    break
                if turn >= cfg["auto_continue_limit"]:
                    result = finish_slot(run_dir, turn_dir, turn_results, "clarification_limit_reached")
                    break
                updated = providers.append_continuation(cfg["provider"], payload, json.loads(body), CONTINUATION_TEXT)
                assert_continuation(payload, updated)
                payload = updated
            results.append({"run_id": row["run_id"], "status": result["status"]})
        attempted_total = sum((exp / "runs" / row["run_id"] / "attempt.json").exists()
                              for row in read_json(exp / "schedule.json")["runs"])
        scheduled = len(read_json(exp / "schedule.json")["runs"])
        status = "all_slots_attempted" if attempted_total == scheduled else "paused_by_call_limit"
        return batch_result(status, attempted_total=attempted_total, scheduled=scheduled)
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Freeze inputs and randomized schedule; no API call")
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("check", help="Verify immutable inputs; no API call")
    p.add_argument("--experiment", required=True)
    p = sub.add_parser("run", help="Run unattempted slots using the provider API key; makes billable calls")
    p.add_argument("--experiment", required=True)
    p.add_argument("--max-new-calls", type=int)
    p.add_argument("--stop-file", help="Stop before the next paid request when this file exists")
    p = sub.add_parser("review", help="Apply identical mesh conversion and export anonymous review files")
    p.add_argument("--experiment", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.config, args.output)
        elif args.command == "check":
            result = verify_frozen(args.experiment)
        elif args.command == "run":
            if args.max_new_calls is not None and args.max_new_calls < 1:
                raise ValueError("--max-new-calls must be positive.")
            result = run_batch(args.experiment, max_new_calls=args.max_new_calls, stop_file=args.stop_file)
        else:
            from review_export import export_review
            exp = Path(args.experiment).resolve()
            verify_frozen(exp)
            runtime_path = exp / "admin/execution_runtime.json"
            if runtime_path.exists() and read_json(runtime_path) != runtime_info():
                raise ValueError("Review conversion must use the same recorded runtime.")
            result = export_review(exp, exp / "software/backend", exp / "frozen/shared/common_output.schema.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in ("paused_after_transport_failure", "paused_after_model_change") else 0
    except (ValueError, OSError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
