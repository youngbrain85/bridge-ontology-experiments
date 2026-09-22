"""Build two single-representation projections of the same frozen ontology facts.

Only B.txt and C.txt are model inputs. alignment.json and source_facts.json are
local audit records, never model context. This module does not call an API or an
OWL reasoner. All validation concerns input construction, not generated models.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict


EXPECTED_FACT_COUNT = 880
SOURCE_META_FIELDS = (
    "version", "namespace", "namespace_note", "instruction",
    "evidence_basis_legend",
)
FACT_META_FIELDS = ("id", "scope", "evidence_basis")
TRIPLE_FIELDS = ("subject", "predicate", "object", "object_kind")
SOURCE_FACT_FIELDS = set(FACT_META_FIELDS + TRIPLE_FIELDS + ("statement_en",))
SOURCE_TOP_FIELDS = set(SOURCE_META_FIELDS + ("representation", "facts"))
MODEL_INPUT_NAMES = ("B.txt", "C.txt")
AUDIT_ONLY_NAMES = ("alignment.json", "source_facts.json")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _value_digest(value: Any) -> str:
    stable = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _digest(stable)


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _load_source(source_path: Path) -> tuple:
    raw = source_path.read_bytes()
    source = json.loads(raw.decode("utf-8-sig"))
    _require(isinstance(source, dict), "Source must be a JSON object")
    _require(set(source) == SOURCE_TOP_FIELDS,
             "Source top-level keys changed; review all new or missing metadata")
    facts = source["facts"]
    _require(isinstance(facts, list), "facts must be an array")
    _require(len(facts) == EXPECTED_FACT_COUNT,
             "Frozen v0.1 English input must contain exactly 880 facts; review version drift")
    _require(isinstance(source["evidence_basis_legend"], dict),
             "evidence_basis_legend must be an object")
    for field in ("version", "namespace", "namespace_note", "instruction", "representation"):
        _require(isinstance(source[field], str) and bool(source[field].strip()),
                 "Invalid source metadata: " + field)
    seen = set()
    for index, fact in enumerate(facts):
        _require(isinstance(fact, dict) and set(fact) == SOURCE_FACT_FIELDS,
                 "Fact fields changed at index " + str(index))
        for field in FACT_META_FIELDS + TRIPLE_FIELDS + ("statement_en",):
            if field == "evidence_basis":
                continue
            _require(isinstance(fact[field], str) and bool(fact[field].strip()),
                     "Invalid " + field + " at index " + str(index))
        _require(fact["id"] not in seen, "Duplicate fact ID: " + fact["id"])
        seen.add(fact["id"])
        _require(fact["object_kind"] in ("iri", "literal"),
                 "Unknown object kind: " + fact["id"])
        bases = fact["evidence_basis"]
        _require(isinstance(bases, list) and bool(bases)
                 and all(isinstance(item, str) for item in bases),
                 "Invalid evidence metadata: " + fact["id"])
        _require(all(item in source["evidence_basis_legend"] for item in bases),
                 "Evidence code missing from common legend: " + fact["id"])
    return source, raw


def _shared_metadata(source: dict) -> dict:
    metadata = {key: copy.deepcopy(source[key]) for key in SOURCE_META_FIELDS}
    # These identifier/format explanations are supplied identically to B and C.
    # The old representation field describes a duplicated text+triple format;
    # it is retained in source_facts.json, not forwarded to either condition.
    metadata["prefixes"] = {
        "csbo": source["namespace"],
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }
    metadata["format_legend"] = {
        "facts": "Each item is one representation of one independent fact, listed in the source order.",
        "id": "An identifier tracing a source fact; not an identifier of a bridge component instance.",
        "scope": "The scope in which the fact or generation instruction applies.",
        "evidence_basis": "An evidence category defined in evidence_basis_legend. Detailed source files are not included in this input.",
        "statement_en": "An English statement of the fact. Separate relationship fields for the same fact are not also supplied.",
        "subject_predicate_object": "subject, predicate and object specify the three parts of the relationship. A separate verbalization of the same fact is not also supplied.",
        "object_kind": "iri denotes a concept identifier; literal denotes a string. Unprefixed identifiers belong to namespace; prefixes are defined in prefixes.",
        "execution": "This JSON is a knowledge representation supplied to an LLM. Parsing JSON does not execute an OWL reasoner, and inferred conclusions have not been added in advance. Generation instructions are not executable OWL constraints.",
    }
    # Source verbalizations contain a few explanatory cautions beyond a bare
    # triple (notably aliases and symmetry). Put the same general explanation
    # in both inputs so it is available to C without duplicating each statement.
    metadata["predicate_semantics"] = {
        "rdf:type": "The subject is an instance of the object class. owl:Class declares an OWL class; owl:ObjectProperty declares a relationship between entities; owl:DatatypeProperty declares a property linking an entity to a data value.",
        "rdfs:label": "An English label in this material.",
        "skos:definition": "The definition of a concept or property.",
        "skos:altLabel": "An alias for search and interpretation. This string alone does not establish component type or identity.",
        "rdfs:subClassOf": "Every instance of the subject class is an instance of the object class.",
        "rdfs:domain": "An axiom inferring the subject type for a relationship using this property; not a missing-data check.",
        "rdfs:range": "An axiom specifying the object type or datatype meaning for a relationship using this property; not a missing-data check.",
        "owl:inverseOf": "The subject and object properties are inverse relationships. Reversing the subject and object of one relationship gives the other.",
        "owl:SymmetricProperty": "A symmetric relationship type. If the relationship holds from a to b, it also holds from b to a. This declaration does not assert transitivity.",
        "rdfs:subPropertyOf": "Whenever the subject property holds, the object property also holds between the same two entities.",
        "owl:disjointWith": "Classes disjoint in this ontology's representational distinction. Do not declare one entity as an instance of both classes.",
        "ruleCondition": "The application condition of a generation instruction.",
        "ruleAction": "The content of a generation instruction; not an executable OWL constraint.",
        "relatedConcept": "A concept or property referenced by a generation instruction.",
    }
    return metadata


def _project(source: dict, metadata: dict, condition: str) -> dict:
    fields = ("statement_en",) if condition == "B" else TRIPLE_FIELDS
    return {
        "metadata": copy.deepcopy(metadata),
        "facts": [
            {key: copy.deepcopy(fact[key]) for key in FACT_META_FIELDS + fields}
            for fact in source["facts"]
        ],
    }


def audit_knowledge(source_path: Path, output_dir: Path) -> Dict[str, Any]:
    """Verify exact projection, single representation, metadata and source bytes.

    Matching records does not prove natural-language/OWL semantic equivalence.
    No generated geometry or engineering result is assessed by this audit.
    """
    source_path, output_dir = Path(source_path), Path(output_dir)
    source, source_raw = _load_source(source_path)
    b_raw = (output_dir / "B.txt").read_bytes()
    c_raw = (output_dir / "C.txt").read_bytes()
    b, c = json.loads(b_raw), json.loads(c_raw)
    archived = json.loads((output_dir / "source_facts.json").read_bytes())
    metadata = _shared_metadata(source)
    _require(archived == source, "Audit source archive differs from source JSON")
    _require(b == _project(source, metadata, "B"), "B is not the exact text projection")
    _require(c == _project(source, metadata, "C"), "C is not the exact triple projection")
    _require(b["metadata"] == c["metadata"], "B/C metadata differs")
    records = []
    for index, (original, b_fact, c_fact) in enumerate(zip(source["facts"], b["facts"], c["facts"])):
        original_meta = {key: original[key] for key in FACT_META_FIELDS}
        records.append({
            "index": index,
            "id": original["id"],
            "metadata_sha256": _value_digest(original_meta),
            "source_fact_sha256": _value_digest(original),
            "B_statement_sha256": _value_digest(b_fact["statement_en"]),
            "C_triple_sha256": _value_digest({key: c_fact[key] for key in TRIPLE_FIELDS}),
        })
    return {
        "audit_version": "1.0.0-en.1",
        "status": "passed",
        "source_version": source["version"],
        "source_path": str(source_path.resolve()),
        "source_sha256": _digest(source_raw),
        "fact_count": len(source["facts"]),
        "checks": {
            "exact_880_facts": True,
            "source_fact_ids_unique": True,
            "fact_order_identical_to_source": True,
            "shared_metadata_identical": True,
            "fact_metadata_identical": True,
            "B_only_statement_en_plus_fact_metadata": True,
            "C_only_triple_fields_plus_fact_metadata": True,
            "B_original_statements_unchanged": True,
            "C_original_triples_unchanged": True,
            "source_audit_archive_complete": True,
            "no_inferred_facts_added": True,
            "no_owl_reasoner_executed": True,
        },
        "predicate_counts": dict(Counter(f["predicate"] for f in source["facts"])),
        "object_kind_counts": dict(Counter(f["object_kind"] for f in source["facts"])),
        "model_inputs": {
            "B": {"file": "B.txt", "sha256": _digest(b_raw), "utf8_bytes": len(b_raw), "characters": len(b_raw.decode("utf-8"))},
            "C": {"file": "C.txt", "sha256": _digest(c_raw), "utf8_bytes": len(c_raw), "characters": len(c_raw.decode("utf-8"))},
        },
        "model_input_allowlist": list(MODEL_INPUT_NAMES),
        "audit_only_files_never_send_to_model": list(AUDIT_ONLY_NAMES),
        "external_archives_never_send_to_model": ["ontology/csbo.owl", "ontology/csbo.ttl", "ontology/knowledge_catalog.json"],
        "source_metadata_handling": {
            "forwarded_unchanged_to_both": list(SOURCE_META_FIELDS),
            "representation": "Retained in the audit source archive only; replaced identically in both inputs by format_legend because the original describes a text+triple duplication.",
            "added_identically_to_both": ["prefixes", "format_legend", "predicate_semantics"],
        },
        "limitations": [
            "This is an exact record-projection audit, not proof of semantic equivalence between English sentences and formal OWL meanings, or between English and earlier Korean inputs.",
            "B and C use different representations of the same source ontology, not absence versus presence of ontology knowledge.",
            "Token count and model familiarity with the representation can differ; record actual API usage separately.",
            "The JSON representation does not execute formal OWL reasoning or supply inferred closure.",
        ],
        "alignment": records,
    }


def build_knowledge(source_path: Path, output_dir: Path) -> Dict[str, Any]:
    """Write B.txt, C.txt and two audit-only JSON files, returning their manifest."""
    source_path, output_dir = Path(source_path), Path(output_dir)
    source, source_raw = _load_source(source_path)
    metadata = _shared_metadata(source)
    payloads = {
        "B.txt": _json_bytes(_project(source, metadata, "B")),
        "C.txt": _json_bytes(_project(source, metadata, "C")),
        "source_facts.json": _json_bytes(source),
    }
    for name in tuple(payloads) + ("alignment.json",):
        _require((output_dir / name).resolve() != source_path.resolve(),
                 "An output file would overwrite the source ontology context")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, raw in payloads.items():
        (output_dir / name).write_bytes(raw)
    _require(source_path.read_bytes() == source_raw, "Source changed during build")
    audit = audit_knowledge(source_path, output_dir)
    (output_dir / "alignment.json").write_bytes(_json_bytes(audit))
    return {
        "status": audit["status"],
        "fact_count": audit["fact_count"],
        "source_sha256": audit["source_sha256"],
        "output_dir": str(output_dir.resolve()),
        "files": {name: str((output_dir / name).resolve()) for name in MODEL_INPUT_NAMES + AUDIT_ONLY_NAMES},
        "model_inputs": audit["model_inputs"],
        "audit_only_files": list(AUDIT_ONLY_NAMES),
        "checks": audit["checks"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_knowledge(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
