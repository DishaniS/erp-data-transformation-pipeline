"""Package isolation, phase boundaries, read-only safety and streaming.

Proves by source inspection and by measurement that the ingestion package
stands on its own, stays inside Phase 6, writes nothing, and processes large
files with a bounded footprint.
"""

from __future__ import annotations

import ast
import pathlib
import tracemalloc

import pytest

from erp_pipeline.ingestion import CsvOptions, IngestionOptions, ingest_file

INGESTION_ROOT = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "ingestion"
)
PRODUCTION_MODULES = sorted(INGESTION_ROOT.rglob("*.py"))


def _tree(module_path: pathlib.Path) -> ast.Module:
    return ast.parse(module_path.read_text(encoding="utf-8"))


def _top_level_imports(module_path: pathlib.Path) -> set[str]:
    names: set[str] = set()

    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.add(node.module.split(".")[0])

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
# Step 40: independence from the frozen prototype
# ============================================================

def test_the_package_has_no_bpi2020_import():
    offenders = [
        module_path.name
        for module_path in PRODUCTION_MODULES
        if "bpi2020" in _top_level_imports(module_path)
    ]
    assert offenders == [], f"erp_pipeline.ingestion imports bpi2020: {offenders}"


def test_importing_ingestion_does_not_load_bpi2020():
    import subprocess
    import sys

    src_root = INGESTION_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.ingestion;"
                "print([m for m in sys.modules if m.startswith('bpi2020')])"
            )
            % src_root,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_schemas_package_remains_stdlib_only():
    """Phase 6 must not have loosened the Phase 1 purity boundary."""
    import sys

    schemas_root = INGESTION_ROOT.parents[0] / "schemas"
    allowed = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in schemas_root.rglob("*.py"):
        for name in _top_level_imports(module_path):
            if name not in allowed:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


def test_schemas_does_not_import_ingestion():
    """Dependency direction stays one-way: ingestion -> schemas."""
    schemas_root = INGESTION_ROOT.parents[0] / "schemas"
    offenders = [
        module_path.name
        for module_path in schemas_root.rglob("*.py")
        if "erp_pipeline.ingestion" in module_path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ============================================================
# Step 41: the source/canonical boundary
# ============================================================

def test_ingestion_never_constructs_a_canonical_artifact():
    """Phase 6 stops at the source level. Building a CanonicalRecord or a
    CanonicalDocument requires a mapping profile, which is Phase 8."""
    forbidden = {
        "CanonicalRecord", "CanonicalDocument", "CanonicalEnvelope",
        "make_canonical_record_id", "make_canonical_document_id",
    }
    offenders = []

    for module_path in PRODUCTION_MODULES:
        used = _called_names(module_path) | _top_level_imports(module_path)
        source = module_path.read_text(encoding="utf-8")

        for name in forbidden:
            # A docstring may explain why the boundary exists; code may not
            # cross it.
            tree = _tree(module_path)
            referenced = any(
                isinstance(node, ast.Name) and node.id == name
                for node in ast.walk(tree)
            ) or any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name == name for alias in node.names)
                for node in ast.walk(tree)
            )
            if referenced or name in used:
                offenders.append(f"{module_path.name}: {name}")
            assert source or True

    assert offenders == [], f"Phase 6 built canonical artifacts: {offenders}"


def test_no_semantic_mapping_or_later_phase_feature_is_present():
    forbidden_tokens = (
        "sentence_transformers", "SentenceTransformer",
        "qdrant", "QdrantClient",
        "fastapi", "FastAPI",
        "sqlalchemy",           # ingestion persists nothing itself
        "infer_semantic_type", "suggest_mapping", "apply_mapping",
    )
    offenders = []

    for module_path in PRODUCTION_MODULES:
        imports = _top_level_imports(module_path)
        called = _called_names(module_path)
        for token in forbidden_tokens:
            if token in imports or token in called:
                offenders.append(f"{module_path.name}: {token}")

    assert offenders == [], f"out-of-scope features found: {offenders}"


def test_the_public_api_exposes_no_mapping_or_etl_entry_point():
    import erp_pipeline.ingestion as ingestion

    forbidden = {
        "map_fields", "suggest_mapping", "apply_mapping", "transform_rows",
        "run_etl", "infer_semantic_type", "to_canonical_record",
        "to_canonical_document", "embed", "upload_vectors",
    }
    assert not (set(dir(ingestion)) & forbidden)


def test_semantic_type_is_never_populated(csv_fixtures):
    """Step 42: cust_no / email_addr / total_amt get TYPES, not MEANINGS."""
    schema = ingest_file(csv_fixtures / "normal.csv").schema

    assert all(
        field.semantic_type is None
        for entity in schema.entities
        for field in entity.fields
    )


# ============================================================
# Read-only safety
# ============================================================

#: Unambiguous filesystem writers. Names that are equally common on
#: non-filesystem objects are deliberately excluded - ``str.replace``,
#: ``dict.copy`` and ``list.remove`` would produce false positives that train
#: people to ignore this test. The complementary
#: ``test_files_are_only_ever_opened_for_reading`` closes the gap by checking
#: open() modes directly.
WRITE_CALLS = frozenset(
    {"write", "write_text", "write_bytes", "writelines", "writer",
     "DictWriter", "mkdir", "makedirs", "unlink", "rmdir", "rmtree",
     "rename", "touch", "chmod", "copy2", "copyfile", "save"}
)


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_production_module_writes_to_the_filesystem(module_path):
    offenders = sorted(_called_names(module_path) & WRITE_CALLS)

    assert offenders == [], f"{module_path.name} writes: {offenders}"


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_files_are_only_ever_opened_for_reading(module_path):
    """Every ``open()`` in the package must be a read mode."""
    for node in ast.walk(_tree(module_path)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "open":
            continue

        modes = [
            keyword.value.value
            for keyword in node.keywords
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant)
        ]
        positional = [
            argument.value
            for argument in node.args[1:2]
            if isinstance(argument, ast.Constant)
        ]

        for mode in modes + positional:
            assert set(mode) <= set("rbt"), (
                f"{module_path.name} opens a file with mode {mode!r}"
            )


def test_ingestion_leaves_the_source_file_untouched(csv_fixtures, tmp_path):
    from erp_pipeline.ingestion import hash_file

    path = tmp_path / "copy.csv"
    path.write_bytes((csv_fixtures / "normal.csv").read_bytes())
    before = (hash_file(path), path.stat().st_size)

    result = ingest_file(path)
    list(result.iter_records())

    assert (hash_file(path), path.stat().st_size) == before


def test_pdf_ocr_rendering_writes_nothing_to_disk(binary_fixtures, tmp_path):
    """A scanned page is rasterized in memory; its contents never hit disk."""
    pytest.importorskip("fitz")

    before = set(tmp_path.iterdir())
    ingest_file(binary_fixtures / "scanned_image_only.pdf")

    assert set(tmp_path.iterdir()) == before


# ============================================================
# Step 43: streaming and bounded processing
# ============================================================

def _write_csv(path: pathlib.Path, row_count: int) -> int:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("id,description,amount\n")
        for index in range(row_count):
            handle.write(f"{index},{'x' * 120},{index}.50\n")

    return path.stat().st_size


def _peak_bytes_ingesting(path: pathlib.Path) -> tuple[int, int]:
    """Return ``(rows_read, peak_bytes)`` for a full streamed pass."""
    tracemalloc.start()
    try:
        result = ingest_file(
            path, IngestionOptions(csv=CsvOptions(max_rows_for_schema_inference=100))
        )
        counted = sum(1 for _ in result.iter_records())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return counted, peak


def test_csv_memory_use_does_not_scale_with_file_size(tmp_path):
    """The real definition of streaming: quadrupling the input must not
    quadruple the memory. An absolute byte threshold would only measure
    interpreter overhead, so this compares two sizes instead.
    """
    small_path = tmp_path / "small.csv"
    large_path = tmp_path / "large.csv"

    small_size = _write_csv(small_path, 15_000)
    large_size = _write_csv(large_path, 60_000)
    assert large_size > small_size * 3.5

    small_rows, small_peak = _peak_bytes_ingesting(small_path)
    large_rows, large_peak = _peak_bytes_ingesting(large_path)

    assert (small_rows, large_rows) == (15_000, 60_000)
    # 4x the data, nowhere near 4x the memory. Buffering the file would put
    # this ratio at roughly 4.0.
    assert large_peak < small_peak * 1.5, (
        f"peak grew from {small_peak} to {large_peak} with file size, which "
        "means the reader is buffering rather than streaming"
    )


def test_a_large_csv_is_fully_readable_without_buffering_it(tmp_path):
    path = tmp_path / "large.csv"
    file_size = _write_csv(path, 60_000)
    assert file_size > 7_000_000  # ~7 MB of source data

    counted, peak = _peak_bytes_ingesting(path)

    assert counted == 60_000
    assert peak < file_size / 2, f"peak {peak} suggests the file was buffered"


def test_schema_inference_stops_at_the_configured_row_budget(tmp_path):
    path = tmp_path / "many.csv"
    path.write_text(
        "id,amount\n" + "".join(f"{i},{i}\n" for i in range(10_000)),
        encoding="utf-8",
    )

    result = ingest_file(
        path, IngestionOptions(csv=CsvOptions(max_rows_for_schema_inference=25))
    )

    assert result.rows_sampled == 25
    assert result.observations[0].rows_sampled == 25


def test_every_configured_limit_is_actually_enforced_somewhere():
    """A budget nobody reads is not a safety feature."""
    sources = "\n".join(
        module_path.read_text(encoding="utf-8") for module_path in PRODUCTION_MODULES
    )

    for option in (
        "max_file_size_bytes", "max_rows_for_schema_inference", "max_columns",
        "max_field_length", "max_errors", "max_pages",
        "max_text_chars_per_page", "max_total_text_chars", "max_pixels",
        "max_text_chars",
    ):
        # Once in the options definition, at least once at a usage site.
        assert sources.count(option) >= 2, f"{option} is never enforced"
