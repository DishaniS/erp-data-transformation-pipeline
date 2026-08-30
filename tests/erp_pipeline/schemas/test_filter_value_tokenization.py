"""Dynamic filter values are HMAC tokens in Qdrant, never plaintext.

Resolves the conflict between two previously-shipped, both-correct
behaviours: schema-driven dynamic filtering (department, shift_code, ...)
needing SOME value in the Qdrant payload to match against, and
``test_the_payload_never_contains_business_content`` requiring that no ERP
business value ever reaches it. The token function proven here is what makes
both true at once - see ``schemas.search_fields.filter_value_token``.
"""

from __future__ import annotations

from erp_pipeline.schemas.search_fields import filter_value_token, render_filter_value


# ======================================================================
# The token function itself - pure, no API, no fixtures
# ======================================================================


def test_token_is_deterministic_for_the_same_scope_and_value():
    first = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )
    second = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )

    assert first == second
    assert len(first) == 64  # hex-encoded SHA-256 digest
    assert first != "Finance"


def test_different_values_produce_different_tokens():
    finance = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )
    engineering = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Engineering",
    )

    assert finance != engineering


def test_the_same_value_in_a_different_scope_does_not_collide():
    """Same value, four different (system, entity, field) scopes.

    A caller who notices two payloads share a token must not be able to
    conclude they share a value ACROSS scopes - only within the SAME scope.
    Each axis (system, entity, field) is varied independently.
    """
    base = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )
    different_system = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_mongo",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )
    different_entity = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.contractors",
        field_name="department_name",
        value="Finance",
    )
    different_field = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="cost_center",
        value="Finance",
    )

    tokens = {base, different_system, different_entity, different_field}
    assert len(tokens) == 4  # all four are pairwise distinct


def test_the_secret_keys_the_token_an_unkeyed_hash_would_not():
    """Requirement: HMAC, never a bare digest a dictionary attack could crack."""
    with_secret_a = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )
    with_secret_b = filter_value_token(
        "secret-b",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )

    assert with_secret_a != with_secret_b

    import hashlib

    unkeyed = hashlib.sha256(b"Finance").hexdigest()
    assert with_secret_a != unkeyed
    assert with_secret_b != unkeyed


def test_render_filter_value_is_the_one_normalization_both_sides_use():
    """Ingestion and search tokenize AFTER this, so it must be one function.

    Enums compare by wire value; whitespace is stripped; a boolean renders
    as the lowercase spelling a caller types in a query string.
    """
    from erp_pipeline.schemas.enums import ContentKind

    assert render_filter_value("  Finance  ") == "Finance"
    assert render_filter_value(True) == "true"
    assert render_filter_value(False) == "false"
    assert render_filter_value(ContentKind.STRUCTURED_RECORD) == "structured_record"
    assert render_filter_value(None) == ""


def test_tokenizing_the_rendered_value_ignores_incidental_whitespace():
    """"Finance" and "  Finance  " must resolve to the SAME token - they are
    the same value once rendered, and a caller who types either should reach
    the same record."""
    padded = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="  Finance  ",
    )
    clean = filter_value_token(
        "secret-a",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department_name",
        value="Finance",
    )

    assert padded == clean
