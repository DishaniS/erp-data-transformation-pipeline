"""Phase 10 - explicit sensitivity, and protection for what it marks.

Two properties matter most here and they pull in opposite directions:

``test_a_field_declaration_never_downgrades_a_source_declaration`` - a
classification must never get weaker by accident.

``test_a_legacy_plaintext_row_still_resolves`` - adding encryption must not make
an existing corpus unreadable.

Nothing in this file implements or tests authorization. Sensitivity is
data-handling metadata; who may see a record is Member 1's decision.
"""

from __future__ import annotations

import json
import os

import pytest

from erp_pipeline.orchestration.representation_crypto import (
    ENCRYPT_AT_OR_ABOVE,
    ENVELOPE_PREFIX,
    EncryptionKeyUnavailableError,
    EnvironmentRepresentationKeyProvider,
    RepresentationCipher,
    RepresentationEncryptionError,
    StaticRepresentationKeyProvider,
    encryption_metadata,
    is_encrypted,
    requires_encryption,
)
from erp_pipeline.orchestration.representation_store import (
    InMemoryRepresentationStore,
)
from erp_pipeline.orchestration.service import BoundedExtractionCache
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.schemas.sensitivity import (
    DEFAULT_SENSITIVITY,
    SENSITIVITY_ORDER,
    coerce,
    field_sensitivity,
    job_sensitivity,
    most_restrictive,
    rank,
    resolve,
)
from erp_pipeline.sync.propagation import AIRepresentation

SECRET_TEXT = "SECRET_BIRTH_CERTIFICATE_TEXT Nimal Silva 1997-03-20"
KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes, AES-256


def cipher(key: bytes = KEY) -> RepresentationCipher:
    return RepresentationCipher(StaticRepresentationKeyProvider(key))


def representation(text=SECRET_TEXT, sensitivity="restricted", identifier="ai:document:x"):
    return AIRepresentation(
        representation_id=identifier,
        entity_type="document",
        text_for_ai=text,
        metadata={"content_kind": "document_chunk", "sensitivity": sensitivity},
    )


# ======================================================================
# Severity order
# ======================================================================


def test_the_order_is_least_to_most_restrictive():
    assert SENSITIVITY_ORDER == (
        SensitivityLevel.PUBLIC,
        SensitivityLevel.INTERNAL,
        SensitivityLevel.CONFIDENTIAL,
        SensitivityLevel.RESTRICTED,
    )


def test_the_order_is_not_alphabetical():
    """Alphabetical would rank confidential below internal below public."""
    values = [level.value for level in SENSITIVITY_ORDER]

    assert values != sorted(values)


def test_rank_increases_with_restriction():
    assert rank("public") < rank("internal") < rank("confidential") < rank(
        "restricted"
    )


def test_the_default_is_unchanged_from_earlier_phases():
    """Changing a default retroactively reclassifies a corpus nobody reviewed."""
    assert DEFAULT_SENSITIVITY is SensitivityLevel.INTERNAL


# ======================================================================
# TEST H / I - precedence and no downgrade
# ======================================================================


def test_the_strictest_declaration_wins():
    assert resolve(artifact="restricted", job="confidential") is (
        SensitivityLevel.RESTRICTED
    )


def test_a_field_declaration_never_downgrades_a_source_declaration():
    """The asymmetry is the point.

    Treating restricted data as internal is a disclosure; treating internal
    data as restricted is an inconvenience. Those are not comparable mistakes.
    """
    assert resolve(artifact="internal", source="restricted") is (
        SensitivityLevel.RESTRICTED
    )


def test_a_job_declaration_never_downgrades_an_inherited_one():
    assert resolve(job="public", inherited="confidential") is (
        SensitivityLevel.CONFIDENTIAL
    )


@pytest.mark.parametrize(
    "declarations, expected",
    [
        ({"artifact": "restricted"}, SensitivityLevel.RESTRICTED),
        ({"job": "confidential"}, SensitivityLevel.CONFIDENTIAL),
        ({"source": "public"}, SensitivityLevel.PUBLIC),
        ({"inherited": "restricted", "job": "public"}, SensitivityLevel.RESTRICTED),
        ({}, DEFAULT_SENSITIVITY),
    ],
)
def test_resolution_across_declaration_scopes(declarations, expected):
    assert resolve(**declarations) is expected


def test_nothing_declared_falls_back_to_the_default():
    assert resolve() is DEFAULT_SENSITIVITY


def test_absent_is_distinguishable_from_public():
    """A missing configuration must not become the least restrictive answer."""
    assert coerce(None) is None
    assert coerce("") is None
    assert coerce("public") is SensitivityLevel.PUBLIC


def test_an_invalid_value_is_refused_not_defaulted():
    with pytest.raises(ValueError):
        coerce("ultra_secret_magic")


def test_most_restrictive_ignores_undeclared_values():
    assert most_restrictive(None, "internal", None) is SensitivityLevel.INTERNAL
    assert most_restrictive(None, None) is None


# ======================================================================
# Option readers
# ======================================================================


def test_a_job_option_declares_a_class():
    assert job_sensitivity({"sensitivity": "restricted"}) is (
        SensitivityLevel.RESTRICTED
    )
    assert job_sensitivity({}) is None


def test_a_per_field_map_declares_a_class_for_one_field():
    """One ERP row genuinely mixes classes."""
    options = {
        "field_sensitivity": {
            "birth_certificate": "restricted",
            "profile_photo": "confidential",
        }
    }

    assert field_sensitivity(options, "birth_certificate") is (
        SensitivityLevel.RESTRICTED
    )
    assert field_sensitivity(options, "profile_photo") is (
        SensitivityLevel.CONFIDENTIAL
    )
    assert field_sensitivity(options, "full_name") is None


def test_no_field_map_means_no_field_declaration():
    assert field_sensitivity({}, "birth_certificate") is None
    assert field_sensitivity(None, "birth_certificate") is None


# ======================================================================
# TEST D / E - one row, several classes
# ======================================================================


def test_each_attachment_keeps_its_own_class_without_contaminating_the_others():
    options = {
        "field_sensitivity": {
            "birth_certificate": "restricted",
            "profile_photo": "confidential",
            "employment_contract": "confidential",
        }
    }
    resolved = {
        name: resolve(
            artifact=field_sensitivity(options, name), inherited="internal"
        )
        for name in ("birth_certificate", "profile_photo", "employment_contract",
                     "full_name")
    }

    assert resolved["birth_certificate"] is SensitivityLevel.RESTRICTED
    assert resolved["profile_photo"] is SensitivityLevel.CONFIDENTIAL
    assert resolved["employment_contract"] is SensitivityLevel.CONFIDENTIAL
    # A field with no declaration keeps what the record carries.
    assert resolved["full_name"] is SensitivityLevel.INTERNAL


# ======================================================================
# Which classes require encryption
# ======================================================================


@pytest.mark.parametrize(
    "level, expected",
    [("public", False), ("internal", False), ("confidential", True),
     ("restricted", True), (None, False)],
)
def test_encryption_applies_at_or_above_confidential(level, expected):
    assert requires_encryption(level) is expected


def test_the_threshold_is_declared_not_implied():
    assert ENCRYPT_AT_OR_ABOVE is SensitivityLevel.CONFIDENTIAL


# ======================================================================
# TEST O / Q / R - the cipher
# ======================================================================


def test_encrypted_text_does_not_contain_the_plaintext():
    stored = cipher().encrypt(SECRET_TEXT)

    assert SECRET_TEXT not in stored
    assert "Nimal Silva" not in stored
    assert is_encrypted(stored)


def test_the_same_plaintext_encrypts_differently_each_time():
    """A fresh random nonce per encryption, as AES-GCM requires."""
    first = cipher().encrypt(SECRET_TEXT)
    second = cipher().encrypt(SECRET_TEXT)

    assert first != second
    assert cipher().decrypt(first) == cipher().decrypt(second) == SECRET_TEXT


def test_encryption_round_trips():
    assert cipher().decrypt(cipher().encrypt(SECRET_TEXT)) == SECRET_TEXT


def test_a_wrong_key_fails_rather_than_returning_garbage():
    """GCM authenticates: a wrong key is a refusal, not plausible nonsense."""
    stored = cipher().encrypt(SECRET_TEXT)
    other = cipher(b"ffffffffffffffffffffffffffffffff")

    with pytest.raises(RepresentationEncryptionError):
        other.decrypt(stored)


def test_a_tampered_ciphertext_fails_to_decrypt():
    stored = cipher().encrypt(SECRET_TEXT)
    envelope = json.loads(stored[len(ENVELOPE_PREFIX):])
    envelope["c"] = envelope["c"][:-4] + "AAAA"
    tampered = ENVELOPE_PREFIX + json.dumps(envelope)

    with pytest.raises(RepresentationEncryptionError):
        cipher().decrypt(tampered)


def test_a_malformed_envelope_never_echoes_its_contents():
    with pytest.raises(RepresentationEncryptionError) as raised:
        cipher().decrypt(ENVELOPE_PREFIX + "not json at all")

    assert "not json at all" not in str(raised.value)


def test_encryption_metadata_carries_no_key_and_no_text():
    stored = cipher().encrypt(SECRET_TEXT)
    metadata = encryption_metadata(stored)

    assert metadata["encrypted"] is True
    assert metadata["algorithm"] == "AES-256-GCM"

    blob = json.dumps(dict(metadata))

    assert SECRET_TEXT not in blob
    assert KEY.decode("latin-1") not in blob


# ======================================================================
# TEST P - fail closed
# ======================================================================


def test_a_missing_key_is_a_refusal_not_a_plaintext_fallback():
    provider = EnvironmentRepresentationKeyProvider(variable="ERP_ABSENT_KEY_X")

    assert provider.is_available() is False

    with pytest.raises(EncryptionKeyUnavailableError):
        provider.get_key()


def test_a_short_key_is_refused():
    with pytest.raises(EncryptionKeyUnavailableError):
        StaticRepresentationKeyProvider(b"too-short").get_key()


def test_a_non_base64_environment_key_is_refused(monkeypatch):
    monkeypatch.setenv("ERP_REPRESENTATION_ENCRYPTION_KEY", "!!!not base64!!!")

    with pytest.raises(EncryptionKeyUnavailableError):
        EnvironmentRepresentationKeyProvider().get_key()


def test_the_key_provider_never_prints_its_key():
    """A provider in a traceback must not carry the key with it."""
    assert "0123456789" not in repr(StaticRepresentationKeyProvider(KEY))
    assert "redacted" in repr(StaticRepresentationKeyProvider(KEY))


def test_a_restricted_representation_is_not_stored_when_no_key_exists():
    """Persistence fails, so - by Phase 5's ordering - nothing is embedded."""
    store = InMemoryRepresentationStore(
        cipher=RepresentationCipher(
            EnvironmentRepresentationKeyProvider(variable="ERP_ABSENT_KEY_Y")
        )
    )

    with pytest.raises(EncryptionKeyUnavailableError):
        store.upsert(representation(sensitivity="restricted"))


def test_a_non_sensitive_representation_stores_without_a_key():
    """A missing key is a contained refusal, never a total outage."""
    store = InMemoryRepresentationStore(
        cipher=RepresentationCipher(
            EnvironmentRepresentationKeyProvider(variable="ERP_ABSENT_KEY_Z")
        )
    )
    stored = store.upsert(representation(sensitivity="internal"))

    assert stored.text_for_ai == SECRET_TEXT


# ======================================================================
# TEST S - legacy rows keep working
# ======================================================================


def test_a_legacy_plaintext_row_still_resolves():
    """Adding encryption must not make an existing corpus unreadable."""
    assert cipher().decrypt("plain old text from before Phase 10") == (
        "plain old text from before Phase 10"
    )


def test_plaintext_is_not_mistaken_for_an_envelope():
    assert is_encrypted("BIRTH CERTIFICATE") is False
    assert is_encrypted(None) is False
    assert encryption_metadata("BIRTH CERTIFICATE") == {"encrypted": False}


# ======================================================================
# TEST W / X - the bounded upload cache
# ======================================================================


def test_the_cache_is_bounded():
    cache = BoundedExtractionCache(max_entries=3)

    for index in range(10):
        cache[f"up_{index}"] = f"document {index}"

    assert len(cache) == 3
    assert cache.evictions == 7


def test_the_cache_evicts_least_recently_used():
    cache = BoundedExtractionCache(max_entries=2)
    cache["a"] = 1
    cache["b"] = 2
    cache.get("a")          # 'a' becomes most recent
    cache["c"] = 3          # so 'b' is evicted

    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache


def test_an_evicted_entry_reports_a_miss_rather_than_stale_data():
    """The job re-extracts on a miss - the cache is never authoritative."""
    cache = BoundedExtractionCache(max_entries=1)
    cache["up_1"] = "document one"
    cache["up_2"] = "document two"

    assert cache.get("up_1") is None
    assert cache.get("up_1", "re-extract") == "re-extract"


def test_the_cache_can_never_be_configured_unlimited():
    """A zero or negative limit would restore the unbounded behaviour."""
    assert BoundedExtractionCache(max_entries=0).max_entries == 1
    assert BoundedExtractionCache(max_entries=-50).max_entries == 1


def test_the_default_limit_is_finite():
    assert 0 < BoundedExtractionCache().max_entries < 10_000


def test_the_cache_reads_its_limit_from_the_environment(monkeypatch):
    monkeypatch.setenv("ERP_UPLOAD_CACHE_MAX_ENTRIES", "4")

    assert BoundedExtractionCache().max_entries == 4


def test_a_nonsense_limit_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ERP_UPLOAD_CACHE_MAX_ENTRIES", "not a number")

    assert BoundedExtractionCache().max_entries == (
        BoundedExtractionCache.DEFAULT_MAX_ENTRIES
    )


def test_the_cache_behaves_like_the_mapping_it_replaced():
    """Existing call sites use `in`, `[]` and `.get`."""
    cache = BoundedExtractionCache(max_entries=4)
    cache["up_1"] = "document"

    assert "up_1" in cache
    assert cache["up_1"] == "document"
    assert cache.get("up_1") == "document"
    assert cache.get("missing") is None

    with pytest.raises(KeyError):
        cache["missing"]


# ======================================================================
# TEST U - Phase 14 writes no temporary plaintext
# ======================================================================


def test_phase_14_asset_extraction_has_no_temp_file_path():
    """The bytes go in memory, as Phase 3 established for the same reason."""
    import ast
    import inspect

    from erp_pipeline.response_adaptation import assets

    source = inspect.getsource(assets)
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "mkstemp" not in called
    assert "_temp_file" not in called
    assert "NamedTemporaryFile" not in called


def test_phase_14_writes_nothing_to_the_filesystem_while_adapting(tmp_path):
    """Measured, not merely asserted about the source."""
    import tempfile

    pytest.importorskip("pymupdf")

    import pymupdf as fitz

    from erp_pipeline.response_adaptation.assets import AssetAdapter, AssetOptions

    document = fitz.open()
    document.new_page().insert_text((72, 96), SECRET_TEXT)
    payload = document.tobytes()
    document.close()

    temp_dir = tempfile.gettempdir()
    before = set(os.listdir(temp_dir))

    adapter = AssetAdapter(AssetOptions())
    adapted = adapter.adapt_bytes(payload, declared_content_type="application/pdf")

    assert set(os.listdir(temp_dir)) - before == set()
    assert adapted is not None


# ======================================================================
# TEST Z - the Member 1 boundary
# ======================================================================


@pytest.mark.parametrize(
    "module_name",
    [
        "erp_pipeline.schemas.sensitivity",
        "erp_pipeline.orchestration.representation_crypto",
    ],
)
def test_phase_10_adds_no_authorization_logic(module_name):
    """Member 4 supplies the label. Member 1 decides what it permits."""
    import ast
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    source = inspect.getsource(module)
    tree = ast.parse(source)
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in (
        "user", "role", "roles", "permission", "permissions", "rbac",
        "authorize", "authorization", "approve", "approval", "deny",
    ):
        assert forbidden not in names, forbidden


def test_sensitivity_is_never_used_to_refuse_a_caller():
    """Resolving a class returns a label - it has no allow/deny outcome."""
    for level in SENSITIVITY_ORDER:
        assert isinstance(resolve(artifact=level.value), SensitivityLevel)
