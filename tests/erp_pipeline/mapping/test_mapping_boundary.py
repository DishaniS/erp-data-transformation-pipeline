"""Steps 41-44, 49: privacy, determinism, offline operation, phase boundary.

Phase 8 decides mappings. It must not read data, call anything, embed
anything, or transform anything - and each of those is checked statically over
the package rather than asserted in prose.
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest

from erp_pipeline.mapping import (
    MAPPING_ENGINE_VERSION,
    FieldOutcome,
    MappingOptions,
    MappingService,
    ScoringWeights,
    generate_mapping,
)
from erp_pipeline.schemas.enums import FieldDataType, MappingStatus

from tests.erp_pipeline.mapping.conftest import (
    SECRETS,
    make_entity,
    make_field,
    make_schema,
)
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin, SourceType

MAPPING_ROOT = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "mapping"
)
PRODUCTION_MODULES = sorted(MAPPING_ROOT.rglob("*.py"))

T = FieldDataType


def _tree(module_path: pathlib.Path) -> ast.Module:
    return ast.parse(module_path.read_text(encoding="utf-8"))


def _imports(module_path: pathlib.Path) -> set[str]:
    names: set[str] = set()

    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names.add(node.module.split(".")[0])
            names.add(node.module)

    return names


def _called_names(module_path: pathlib.Path) -> set[str]:
    names: set[str] = set()

    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)

    return names


# ============================================================
# Step 42/43: offline, no AI, no embeddings
# ============================================================

NETWORK_AND_AI_MODULES = frozenset(
    {
        "requests", "httpx", "aiohttp", "urllib3", "socket", "ssl",
        "http", "http.client", "urllib.request",
        "openai", "anthropic", "google", "cohere", "transformers",
        "sentence_transformers", "torch", "tensorflow", "sklearn",
        "qdrant_client", "faiss", "chromadb", "numpy",
    }
)


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_network_or_ai_dependency(module_path):
    offenders = sorted(_imports(module_path) & NETWORK_AND_AI_MODULES)

    assert offenders == [], (
        f"{module_path.name} imports {offenders}. Phase 8 must work "
        "completely offline and deterministically."
    )


def test_importing_the_package_loads_no_ai_or_network_module():
    import subprocess
    import sys

    src_root = MAPPING_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.mapping;"
                "print(sorted(m for m in sys.modules if m.split('.')[0] in "
                "{'requests','httpx','openai','anthropic','torch',"
                "'sentence_transformers','qdrant_client','numpy','socket'}))"
            )
            % src_root,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_no_embedding_or_vector_vocabulary_exists():
    """Checked against IDENTIFIERS via the AST, not raw text - the module
    docstrings legitimately say what the phase does not do, and a text scan
    would flag its own disclaimer."""
    forbidden = {"embed", "embedding", "embeddings", "vector_store",
                 "cosine_similarity", "encode_text", "llm", "prompt",
                 "complete", "chat"}
    offenders = []

    for module_path in PRODUCTION_MODULES:
        tree = _tree(module_path)
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }

        for name in sorted(identifiers & forbidden):
            offenders.append(f"{module_path.name}: {name}")

    assert offenders == [], offenders


def test_the_engine_uses_no_randomness():
    offenders = []

    for module_path in PRODUCTION_MODULES:
        if {"random", "secrets", "uuid"} & _imports(module_path):
            offenders.append(module_path.name)

    assert offenders == []


# ============================================================
# Step 41/44: schemas only, never data
# ============================================================

def test_mapping_needs_no_business_values(all_source_schemas):
    """The schemas carry no values at all, and mapping still works - which is
    the whole privacy argument."""
    for name, schema in all_source_schemas.items():
        result = generate_mapping(schema)
        assert result.decisions, name


def test_no_schema_free_text_leaks_into_the_result(all_source_schemas):
    """Entity descriptions and field metadata can carry example values. The
    engine reads neither into its output."""
    for name, schema in all_source_schemas.items():
        payload = json.dumps(generate_mapping(schema).to_dict(), default=str)

        for secret in SECRETS:
            assert secret not in payload, f"{name} leaked {secret!r}"


def test_no_secret_reaches_a_mapping_profile(all_source_schemas):
    for name, schema in all_source_schemas.items():
        for profile in generate_mapping(schema).profiles:
            payload = json.dumps(profile.to_json_dict(), default=str)
            for secret in SECRETS:
                assert secret not in payload, f"{name} leaked {secret!r}"


def test_nothing_is_logged_during_mapping(all_source_schemas, caplog):
    with caplog.at_level(logging.DEBUG):
        for schema in all_source_schemas.values():
            generate_mapping(schema)

    for secret in SECRETS:
        assert secret not in caplog.text


def test_no_error_message_carries_schema_free_text():
    from erp_pipeline.mapping import CanonicalTargetNotFoundError, MappingOverride

    schema = make_schema(
        "probe", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(
            make_entity(
                "fin_invoice",
                (make_field("invoice_no", T.STRING, with_secret=True),),
                with_secret=True,
            ),
        ),
    )

    with pytest.raises(CanonicalTargetNotFoundError) as excinfo:
        generate_mapping(
            schema,
            overrides=(
                MappingOverride(source_field="invoice_no", target="nope.nothing"),
            ),
        )

    for secret in SECRETS:
        assert secret not in str(excinfo.value)


def test_the_candidate_model_cannot_hold_a_value():
    """Structural guarantee: there is nowhere to put one."""
    import dataclasses

    from erp_pipeline.mapping import MappingCandidate, MappingEvidence, NameEvidence

    forbidden = {"value", "values", "sample", "samples", "example", "examples",
                 "row", "rows", "data", "payload"}

    for model in (MappingCandidate, MappingEvidence, NameEvidence):
        names = {f.name for f in dataclasses.fields(model)}
        assert not (names & forbidden), model.__name__


# ============================================================
# Step 26/31: no transformation, no canonical records
# ============================================================

def test_no_canonical_record_is_ever_constructed():
    forbidden = {
        "CanonicalRecord", "CanonicalDocument", "CanonicalEnvelope",
        "make_canonical_record_id", "make_canonical_document_id",
    }
    offenders = []

    for module_path in PRODUCTION_MODULES:
        tree = _tree(module_path)
        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        for name in sorted(referenced & forbidden):
            offenders.append(f"{module_path.name}: {name}")

    assert offenders == [], offenders


def test_no_transformation_is_ever_executed():
    """A TransformationRule is inspected structurally and never dispatched.

    ``eval``/``exec``/``compile`` are checked as BARE calls only: ``re.compile``
    is an attribute call and is exactly the kind of false positive that trains
    people to ignore a security test.
    """
    dynamic_execution = {"eval", "exec", "compile"}
    transformation_dispatch = {
        "apply_transformation", "run_transformation", "transform_value",
        "apply_rule", "run_rule",
    }
    offenders = []

    for module_path in PRODUCTION_MODULES:
        tree = _tree(module_path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in dynamic_execution:
                offenders.append(f"{module_path.name}: {node.func.id}()")
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in transformation_dispatch:
                    offenders.append(f"{module_path.name}: .{node.func.attr}()")

    assert offenders == [], offenders


def test_the_public_api_exposes_no_execution_entry_point():
    import erp_pipeline.mapping as mapping

    forbidden = {
        "transform", "transform_record", "apply", "apply_mapping", "execute",
        "run_etl", "to_canonical_record", "load", "extract",
    }
    assert not (set(dir(mapping)) & forbidden)


def test_generated_mappings_declare_no_transformations(all_source_schemas):
    """Deciding a value needs a date parse is a mapping decision; choosing the
    format is a transformation decision, and that is Phase 9's."""
    for schema in all_source_schemas.values():
        for profile in generate_mapping(schema).profiles:
            for item in profile.field_mappings:
                assert item.transformations == ()


def test_a_profile_states_that_it_has_not_been_applied(all_source_schemas):
    for schema in all_source_schemas.values():
        for profile in generate_mapping(schema).profiles:
            assert profile.metadata["applied_to_data"] is False


def test_the_mapping_package_does_not_import_sqlalchemy():
    """It has no persistence of its own; the Phase 2 catalog is passed in."""
    offenders = [
        module_path.name
        for module_path in PRODUCTION_MODULES
        if "sqlalchemy" in _imports(module_path)
    ]
    assert offenders == []


def test_the_package_has_no_bpi2020_import():
    offenders = [
        module_path.name
        for module_path in PRODUCTION_MODULES
        if "bpi2020" in _imports(module_path)
    ]
    assert offenders == []


def test_the_schemas_package_remains_stdlib_only():
    """Phase 8 must not have loosened the Phase 1 purity boundary."""
    import sys

    schemas_root = MAPPING_ROOT.parents[0] / "schemas"
    allowed = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in schemas_root.rglob("*.py"):
        for name in _imports(module_path):
            if name.split(".")[0] not in allowed:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


def test_schemas_does_not_import_mapping():
    """Dependency direction stays one-way: mapping -> schemas."""
    schemas_root = MAPPING_ROOT.parents[0] / "schemas"
    offenders = [
        module_path.name
        for module_path in schemas_root.rglob("*.py")
        if "erp_pipeline.mapping" in module_path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ============================================================
# Step 49: determinism
# ============================================================

def _without_operational_timestamps(payload: dict) -> dict:
    """Strip the contract's own construction timestamps.

    ``MappingProfile.created_at``/``updated_at`` are required by the frozen
    Phase 1 contract and default to "now", exactly as Phase 6's
    ``extracted_at`` and Phase 7's ``parsed_at`` do. They are operational
    bookkeeping and feed no identity and no hash - so determinism is asserted
    over everything else, and profile IDENTITY is asserted separately.
    """
    cleaned = json.loads(json.dumps(payload, default=str))

    for profile in cleaned.get("profiles", []):
        profile.pop("created_at", None)
        profile.pop("updated_at", None)

    return cleaned


def test_repeated_generation_is_identical(all_source_schemas):
    for name, schema in all_source_schemas.items():
        first = generate_mapping(schema)
        second = generate_mapping(schema)

        assert _without_operational_timestamps(first.to_dict()) == (
            _without_operational_timestamps(second.to_dict())
        ), name


def test_only_the_contracts_own_timestamps_differ_between_runs(postgres_schema):
    """Proves the exclusion above is narrow: nothing else moves."""
    first = generate_mapping(postgres_schema).profiles[0].to_json_dict()
    second = generate_mapping(postgres_schema).profiles[0].to_json_dict()

    differing = {key for key in first if first[key] != second[key]}

    assert differing <= {"created_at", "updated_at"}


def test_candidate_order_and_scores_are_stable(all_source_schemas):
    for name, schema in all_source_schemas.items():
        first = generate_mapping(schema)
        second = generate_mapping(schema)

        for left, right in zip(first.decisions, second.decisions):
            assert [c.qualified_target for c in left.candidates] == [
                c.qualified_target for c in right.candidates
            ], name
            assert [c.score.total for c in left.candidates] == [
                c.score.total for c in right.candidates
            ], name
            assert left.confidence == right.confidence, name


def test_profile_identity_is_deterministic(all_source_schemas):
    for name, schema in all_source_schemas.items():
        first = {p.mapping_id for p in generate_mapping(schema).profiles}
        second = {p.mapping_id for p in generate_mapping(schema).profiles}

        assert first == second, name


def test_profile_identity_carries_no_timestamp_or_uuid(postgres_schema):
    profile = generate_mapping(postgres_schema).profiles[0]

    assert "2026" not in profile.mapping_id
    assert len(profile.mapping_id.split(".")) == 4


def test_changing_the_configuration_changes_the_profile_identity(postgres_schema):
    """A mapping generated under different rules must not silently overwrite
    one generated under the old ones (Step 29)."""
    default_ids = {p.mapping_id for p in generate_mapping(postgres_schema).profiles}
    tuned_ids = {
        p.mapping_id
        for p in generate_mapping(
            postgres_schema, options=MappingOptions(high_threshold=0.6)
        ).profiles
    }

    assert default_ids != tuned_ids


def test_changing_the_weights_changes_the_configuration_fingerprint():
    from erp_pipeline.mapping import MappingEngine

    default_engine = MappingEngine()
    tuned_engine = MappingEngine(
        options=MappingOptions(
            weights=ScoringWeights(name=0.6, type=0.2, entity=0.1, path=0.1)
        )
    )

    assert default_engine.config_fingerprint != tuned_engine.config_fingerprint


def test_the_engine_version_is_recorded_on_every_profile(all_source_schemas):
    for schema in all_source_schemas.values():
        for profile in generate_mapping(schema).profiles:
            assert profile.metadata["mapping_engine_version"] == (
                MAPPING_ENGINE_VERSION
            )
            assert profile.metadata["canonical_model_identity"] == "erp_core@1.0"
            assert profile.metadata["config_fingerprint"]


def test_the_target_model_version_is_recorded(postgres_schema):
    """Step 37: a V1 mapping must not pretend it was generated against V2."""
    profile = generate_mapping(postgres_schema).profiles[0]

    assert profile.metadata["canonical_model_id"] == "erp_core"
    assert profile.metadata["canonical_model_version"] == "1.0"


# ============================================================
# Step 48: explainability
# ============================================================

def test_every_candidate_carries_complete_evidence(all_source_schemas):
    """No opaque scores anywhere."""
    for name, schema in all_source_schemas.items():
        for decision in generate_mapping(schema).decisions:
            for candidate in decision.candidates:
                evidence = candidate.evidence

                assert evidence.name is not None, name
                assert evidence.name.kind is not None, name
                assert evidence.type_comparison is not None, name
                assert evidence.entity is not None, name
                assert evidence.path is not None, name
                assert candidate.explain(), name
                assert evidence.name.explain(), name
                assert evidence.type_comparison.explain(), name


def test_every_selected_mapping_explains_itself(all_source_schemas):
    for name, schema in all_source_schemas.items():
        for profile in generate_mapping(schema).profiles:
            for item in profile.field_mappings:
                assert item.reason, name
                assert item.metadata["evidence"], name
                assert item.metadata["score_components"], name


def test_the_score_decomposes_into_its_components(postgres_schema):
    decision = generate_mapping(postgres_schema).decision_for("total_amount")
    score = decision.selected.score

    total = (
        score.name_component + score.type_component
        + score.entity_component + score.path_component
    )
    assert abs(total - score.total) < 1e-6


def test_a_refusal_explains_itself_too(postgres_schema):
    """Not selecting is a decision, and needs a reason like any other."""
    decision = generate_mapping(postgres_schema).decision_for(
        "legacy_internal_flag_74"
    )

    assert decision.outcome is FieldOutcome.UNMAPPED
    assert decision.reason


def test_evidence_survives_into_the_persisted_profile(postgres_schema):
    """So a profile reloaded from the catalog still explains itself."""
    profile = generate_mapping(postgres_schema).profiles[0]
    item = profile.field_mappings[0]

    assert item.metadata["evidence"]["name_match"]
    assert item.metadata["evidence"]["type_compatibility"]
    assert item.metadata["confidence_level"] in ("high", "medium", "low")


# ============================================================
# Status vocabulary reuse
# ============================================================

def test_generated_mappings_reuse_the_existing_status_enum(postgres_schema):
    profile = generate_mapping(postgres_schema).profiles[0]

    assert profile.status is MappingStatus.SUGGESTED
    assert all(
        item.status is MappingStatus.AUTO_ACCEPTED
        for item in profile.field_mappings
    )


def test_a_whole_profile_is_never_auto_approved(all_source_schemas):
    """Individual fields may be auto-accepted on strong evidence; the profile
    as a whole still awaits a human."""
    for schema in all_source_schemas.values():
        for profile in generate_mapping(schema).profiles:
            assert profile.status is not MappingStatus.APPROVED
            assert profile.approved_by is None
