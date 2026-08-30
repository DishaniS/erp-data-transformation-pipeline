"""Generic ERP process/case modelling.

WHAT THIS PACKAGE IS FOR
------------------------
An ERP event log records that something *happened*: an approval was granted, a
declaration was submitted, a payment was handled. Grouped by case, those events
describe a business process instance - and a process instance is the unit a
governance model or a workflow engine reasons about. Neither can be expressed
as a flat canonical record, which is why this layer exists alongside
``schemas`` rather than inside it.

WHAT IT PRODUCES
----------------
    raw event rows
        -> ProcessEvent          (normalized against an EventLogConfig)
        -> ProcessCase           (ordered, timed, with a current state)
        -> ProcessModel          (directly-follows, giving allowed next states)
        -> CanonicalRecord       (record_type=CASE)
        -> AIRepresentation      (embedded and stored by the existing pipeline)

The last two projections matter most: once a case is an ``AIRepresentation`` it
flows through the existing embedding, storage and retrieval path unchanged.
There is no case-specific code downstream of this package.

WHAT IT DOES NOT CONTAIN
------------------------
No column names, no dataset names, no process names. Where the case id,
activity and timestamp live is described by an ``EventLogConfig`` supplied by
the caller. A new ERP event log is a new configuration, never a code change.

This package never imports a dataset-specific module.
"""

from __future__ import annotations

from erp_pipeline.process.cascade import (
    CaseEventSource,
    CaseKeyIndex,
    InMemoryCaseEventSource,
    ProcessCaseRepresentationBuilder,
    ProcessCaseResolver,
    build_case_cascade,
)
from erp_pipeline.process.case_builder import (
    activity_sequence,
    apply_process_model,
    build_case,
    build_case_summary,
    build_cases,
    build_process_model,
    case_duration_seconds,
    extract_entity_references,
    group_events,
    sort_events,
    unique_activities,
)
from erp_pipeline.process.errors import (
    CaseBuildError,
    EventNormalizationError,
    ProcessConfigurationError,
    ProcessError,
)
from erp_pipeline.process.event_normalizer import (
    coerce_timestamp,
    extract_attributes,
    normalize_event,
    normalize_events,
    resolve_process_type,
)
from erp_pipeline.process.models import (
    DEFAULT_PROCESS_TYPE,
    DEFAULT_SUMMARY_CONFIG,
    PROCESS_ENGINE_VERSION,
    CaseSummaryConfig,
    EventLogConfig,
    ProcessCase,
    ProcessEvent,
    ProcessModel,
    make_case_record_id,
    normalize_process_type,
)
from erp_pipeline.process.service import ProcessCaseService

__all__ = [
    # version
    "PROCESS_ENGINE_VERSION",
    "DEFAULT_PROCESS_TYPE",
    # configuration
    "EventLogConfig",
    "CaseSummaryConfig",
    "DEFAULT_SUMMARY_CONFIG",
    # contracts
    "ProcessEvent",
    "ProcessCase",
    "ProcessModel",
    "make_case_record_id",
    "normalize_process_type",
    # normalization
    "coerce_timestamp",
    "resolve_process_type",
    "extract_attributes",
    "normalize_event",
    "normalize_events",
    # building
    "sort_events",
    "activity_sequence",
    "unique_activities",
    "case_duration_seconds",
    "extract_entity_references",
    "build_case_summary",
    "build_case",
    "group_events",
    "build_cases",
    "build_process_model",
    "apply_process_model",
    # incremental cascade
    "CaseEventSource",
    "CaseKeyIndex",
    "ProcessCaseResolver",
    "ProcessCaseRepresentationBuilder",
    "InMemoryCaseEventSource",
    "build_case_cascade",
    # service
    "ProcessCaseService",
    # errors
    "ProcessError",
    "ProcessConfigurationError",
    "EventNormalizationError",
    "CaseBuildError",
]
