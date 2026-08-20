"""Steps 42, 45, 57, 58: the boundaries that define what Phase 7 is.

Phase 7 is DOCUMENTATION UNDERSTANDING, not API EXECUTION. That distinction is
the whole reason this component and the teammate's integration/MCP component
are separate, so it is enforced by static analysis rather than by convention:

    no HTTP client, no endpoint call, no token acquisition, no OAuth flow
    no remote $ref fetch
    no unsafe YAML loader
    no canonical record, no mapping profile, no semantic guessing

``httpx`` happens to be installed in this environment, which makes the
no-network assertions meaningful rather than vacuous - importing a client
would succeed if the code tried.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

API_SPECS_ROOT = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src" / "erp_pipeline" / "api_specs"
)
PRODUCTION_MODULES = sorted(API_SPECS_ROOT.rglob("*.py"))


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


def _referenced_attributes(module_path: pathlib.Path) -> set[str]:
    return {
        node.attr
        for node in ast.walk(_tree(module_path))
        if isinstance(node, ast.Attribute)
    }


# ============================================================
# Step 42 / 58: no endpoint execution
# ============================================================

#: Every HTTP client and low-level networking module. Importing any of them
#: from this package would mean the boundary had been crossed.
NETWORK_MODULES = frozenset(
    {
        "requests", "httpx", "aiohttp", "urllib3", "socket", "ssl",
        "http", "http.client", "urllib.request", "ftplib", "telnetlib",
        "websockets", "grpc", "paramiko", "smtplib", "asyncio",
    }
)

#: Call names that perform or prepare a network operation.
NETWORK_CALLS = frozenset(
    {
        "urlopen", "urlretrieve", "Request", "HTTPConnection",
        "HTTPSConnection", "getresponse", "ClientSession", "Session",
        "connect", "sendall", "recv", "create_connection", "getaddrinfo",
        "fetch", "download",
    }
)


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_network_client(module_path):
    offenders = sorted(_imports(module_path) & NETWORK_MODULES)

    assert offenders == [], (
        f"{module_path.name} imports networking: {offenders}. Runtime ERP/API "
        "execution belongs to the integration component, not Phase 7."
    )


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_module_performs_a_network_call(module_path):
    offenders = sorted(_called_names(module_path) & NETWORK_CALLS)

    assert offenders == [], f"{module_path.name} performs network calls: {offenders}"


def test_the_package_exposes_no_execution_entry_point():
    import erp_pipeline.api_specs as api_specs

    forbidden = {
        "call", "invoke", "execute", "send", "request", "get", "post", "put",
        "patch", "delete", "authenticate", "acquire_token", "get_token",
        "refresh_token", "oauth", "login", "run_operation", "call_endpoint",
    }
    assert not (set(dir(api_specs)) & forbidden)


def test_importing_the_package_loads_no_http_client():
    """``httpx`` is installed here, so this would catch a real import."""
    import subprocess
    import sys

    src_root = API_SPECS_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.api_specs;"
                "print(sorted(m for m in sys.modules "
                "if m.split('.')[0] in "
                "{'requests','httpx','aiohttp','urllib3','socket'}))"
            )
            % src_root,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_no_credential_acquisition_vocabulary_exists():
    """Security metadata is descriptive. Nothing obtains, refreshes or sends
    a credential."""
    forbidden = {
        "acquire_token", "request_token", "refresh_token", "client_credentials",
        "authorization_code_grant", "sign_request", "authenticate",
    }
    offenders = []

    for module_path in PRODUCTION_MODULES:
        for name in _called_names(module_path) | _referenced_attributes(module_path):
            if name in forbidden:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


def test_a_documented_url_is_never_opened(spec_fixtures):
    """The fixtures contain reachable-looking URLs; parsing must not touch
    them. A socket monkeypatched to explode proves it."""
    import socket

    from erp_pipeline.api_specs import parse_api_spec

    original = socket.socket

    class ExplodingSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "Phase 7 attempted to open a socket while parsing a "
                "specification."
            )

    socket.socket = ExplodingSocket
    try:
        for filename in (
            "openapi_3_basic.json",
            "openapi_3_refs.yaml",
            "openapi_3_security.yaml",
            "postman_auth_secrets.json",
        ):
            parse_api_spec(spec_fixtures / filename)
    finally:
        socket.socket = original


# ============================================================
# Step 45: safe YAML only
# ============================================================

UNSAFE_YAML_LOADERS = frozenset(
    {"load", "unsafe_load", "full_load", "Loader", "UnsafeLoader", "FullLoader"}
)


def test_yaml_is_only_ever_loaded_safely():
    """``yaml.load`` with a permissive loader turns "parse this spec" into
    "run this code"."""
    offenders = []

    for module_path in PRODUCTION_MODULES:
        for node in ast.walk(_tree(module_path)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in UNSAFE_YAML_LOADERS:
                offenders.append(f"{module_path.name}: yaml.{node.func.attr}")

    assert offenders == [], f"unsafe YAML loading: {offenders}"


def test_safe_load_is_actually_used():
    """Positive control, so the test above cannot pass by yaml being unused."""
    source = (API_SPECS_ROOT / "safety.py").read_text(encoding="utf-8")

    assert "yaml.safe_load(" in source


# ============================================================
# Steps 21, 57: no semantic mapping, no canonical artifacts
# ============================================================

def test_no_canonical_artifact_is_constructed():
    forbidden = {
        "CanonicalRecord", "CanonicalDocument", "CanonicalEnvelope",
        "make_canonical_record_id", "make_canonical_document_id",
        "MappingProfile", "FieldMapping", "TransformationRule",
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

    assert offenders == [], f"Phase 7 built canonical artifacts: {offenders}"


def test_the_public_api_exposes_no_mapping_entry_point():
    import erp_pipeline.api_specs as api_specs

    forbidden = {
        "map_fields", "suggest_mapping", "apply_mapping", "to_canonical",
        "to_canonical_record", "infer_semantic_type", "transform", "run_etl",
        "embed", "upload_vectors",
    }
    assert not (set(dir(api_specs)) & forbidden)


def test_no_semantic_vocabulary_appears_in_the_package():
    """Step 57: customerNumber must not be mapped to canonical.customer_id."""
    forbidden = ("canonical_customer", "canonical.customer", "canonical_invoice",
                 "semantic_map", "guess_semantic")
    offenders = []

    for module_path in PRODUCTION_MODULES:
        source = module_path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in source:
                offenders.append(f"{module_path.name}: {token}")

    assert offenders == []


def test_field_names_are_described_never_translated(spec_fixtures):
    """`customerNumber` stays `customernumber`; it does not become
    `customer_id`."""
    from erp_pipeline.api_specs import parse_api_spec

    result = parse_api_spec(spec_fixtures / "swagger_2_basic.json")
    fields = {
        f.normalized_name: f
        for f in result.schema.entity_by_normalized_name("legacycustomer").fields
    }

    assert "customernumber" in fields
    assert "customer_id" not in fields
    assert fields["customernumber"].source_name == "customerNumber"
    assert fields["customernumber"].semantic_type is None


def test_no_embeddings_or_vector_storage():
    forbidden = {"sentence_transformers", "qdrant_client", "numpy", "torch"}
    offenders = []

    for module_path in PRODUCTION_MODULES:
        for name in sorted(_imports(module_path) & forbidden):
            offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


def test_no_api_or_ui_framework():
    forbidden = {"fastapi", "flask", "django", "starlette", "uvicorn"}
    offenders = []

    for module_path in PRODUCTION_MODULES:
        for name in sorted(_imports(module_path) & forbidden):
            offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


# ============================================================
# Package isolation
# ============================================================

def test_the_package_has_no_bpi2020_import():
    offenders = [
        module_path.name
        for module_path in PRODUCTION_MODULES
        if "bpi2020" in _imports(module_path)
    ]
    assert offenders == []


def test_importing_api_specs_does_not_load_bpi2020():
    import subprocess
    import sys

    src_root = API_SPECS_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.api_specs;"
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
    """Phase 7 must not have loosened the Phase 1 purity boundary."""
    import sys

    schemas_root = API_SPECS_ROOT.parents[0] / "schemas"
    allowed = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in schemas_root.rglob("*.py"):
        for name in _imports(module_path):
            if name.split(".")[0] not in allowed:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == []


def test_schemas_does_not_import_api_specs():
    """Dependency direction stays one-way: api_specs -> schemas."""
    schemas_root = API_SPECS_ROOT.parents[0] / "schemas"
    offenders = [
        module_path.name
        for module_path in schemas_root.rglob("*.py")
        if "erp_pipeline.api_specs" in module_path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ============================================================
# Read-only on disk
# ============================================================

WRITE_CALLS = frozenset(
    {"write", "write_text", "write_bytes", "writelines", "mkdir", "makedirs",
     "unlink", "rmdir", "rmtree", "rename", "touch", "chmod", "copyfile",
     "copy2", "save", "dump", "safe_dump"}
)


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_module_writes_to_the_filesystem(module_path):
    offenders = sorted(_called_names(module_path) & WRITE_CALLS)

    assert offenders == [], f"{module_path.name} writes: {offenders}"


def test_parsing_leaves_the_specification_file_untouched(spec_fixtures, tmp_path):
    import hashlib

    from erp_pipeline.api_specs import parse_api_spec

    path = tmp_path / "copy.json"
    path.write_bytes((spec_fixtures / "openapi_3_basic.json").read_bytes())
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    parse_api_spec(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# ============================================================
# Resource limits (Step 44)
# ============================================================

def test_every_configured_limit_is_enforced_somewhere():
    """A budget nobody reads is not a safety feature."""
    sources = "\n".join(
        module_path.read_text(encoding="utf-8") for module_path in PRODUCTION_MODULES
    )

    for option in (
        "max_spec_size_bytes", "max_operations", "max_schemas",
        "max_fields_per_schema", "max_nesting_depth", "max_reference_depth",
        "max_examples_per_operation", "max_example_body_bytes",
        "max_enum_values", "max_warnings",
    ):
        assert sources.count(option) >= 2, f"{option} is never enforced"


def test_the_warning_budget_is_bounded():
    from erp_pipeline.api_specs.safety import WarningBudget

    budget = WarningBudget(max_warnings=3)
    for index in range(10):
        budget.add(index)

    assert len(budget.items()) == 3
    assert budget.suppressed_count == 7


def test_options_reject_a_nonsensical_budget():
    from erp_pipeline.api_specs import ApiSpecOptions

    with pytest.raises(ValueError):
        ApiSpecOptions(max_operations=0)

    with pytest.raises(ValueError):
        ApiSpecOptions(max_reference_depth=-1)
