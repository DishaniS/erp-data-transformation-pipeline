"""ERP-Aware Data Transformation Pipeline - the generic research framework.

WHAT THIS PACKAGE IS
--------------------
One source-independent path from heterogeneous legacy ERP data to a validated
canonical representation, a deterministic AI-ready projection, a local
embedding, and a policy-placed vector - with provenance preserved at every step
and identity that never depends on a database sequence or a request id.

    connectors      PostgreSQL · MySQL · SQL Server · MongoDB, read-only
    discovery       declared relational catalogs, observed document structure
    ingestion       CSV · PDF · images · OCR · document classification
    api_specs       OpenAPI · Swagger · Postman, parsed as CONTRACTS only
    catalog         immutable, versioned schema and mapping snapshots
    mapping         explainable source-to-canonical field matching
    transformation  rules, typed conversion, validation, quality thresholds
    process         event logs -> process cases -> observed process models
    sync            watermarks, schema drift, mapping impact, propagation
    ai              deterministic representations, chunking, local embeddings
    storage         HOT / WARM / COLD routing, migration, cost and metrics
    verification    cross-store integrity between records, state and vectors
    orchestration   capability-aware job plans and durable stage execution
    api             the FastAPI control plane over all of the above
    runtime         production composition, settings and schema bootstrap

    schemas         the frozen contracts every layer above speaks

BOUNDARIES THIS PACKAGE KEEPS
-----------------------------
* ``schemas`` performs no I/O and depends on the Python standard library alone.
* Embeddings are generated LOCALLY. There is no remote inference client, no
  LLM, and no generated prose anywhere in this framework.
* Parsed API specifications are contracts. Their endpoints are never called.
* No dataset-specific knowledge lives in any module here. Where a particular
  ERP puts its case id, its activity column or its document vocabulary is
  configuration supplied by the caller.

DATASETS
--------
BPI Challenge 2020 is a dataset this framework is demonstrated and evaluated
against - see ``scripts/demos/run_bpi2020_demo.py`` and
``examples/bpi2020/``. It is not part of this package, and nothing here
imports or depends on it.
"""

from __future__ import annotations

from erp_pipeline.version import (
    CANONICAL_MODEL_VERSION,
    MAPPING_MODEL_VERSION,
    RUN_MODEL_VERSION,
    SOURCE_MODEL_VERSION,
)

__all__ = [
    "CANONICAL_MODEL_VERSION",
    "SOURCE_MODEL_VERSION",
    "MAPPING_MODEL_VERSION",
    "RUN_MODEL_VERSION",
]
