"""ERP identity a caller may declare when uploading a document (Phase 6).

WHY THIS IS DECLARED, NEVER INFERRED
------------------------------------
A file called ``EMP002_birth_certificate.jpg`` looks like it belongs to employee
EMP002. It might. It might also be a template someone saved under a colleague's
name, a re-scan filed under the wrong id, or a document whose name says
``EMP002`` because that was the last record the clerk had open.

Deriving identity from a filename would put a guess into the same
``business_key_value`` field that Phase 2 fills from a declared primary key and
Phase 4 filters on exactly. A search for EMP002 would then return documents that
merely LOOK like EMP002's, indistinguishable from ones that provably are - and
the caller could not tell which kind they got. The same argument rules out
inferring identity from OCR text, from the first field of anything, or from a
model.

So identity is supplied by the caller or it is absent. Absent is a perfectly
good answer: a company policy PDF belongs to no ERP record, indexes fine, and
carries no business key.

THE PAIR RULE
-------------
``business_key_name`` and ``business_key_value`` are ONE declaration in two
fields. Accepting half of it would store ``employee_id`` naming nothing, or
``EMP002`` of no stated kind - neither of which any filter can use, and both of
which look like data until someone queries them. Half a declaration is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from erp_pipeline.orchestration.errors import InvalidPipelineRequestError

#: Where a declared identity travels between the upload request and the stage
#: that builds representations. A single nested key rather than nine loose ones,
#: so ``JobRequest.options`` stays readable and the whole declaration can be
#: absent rather than present-and-empty.
DOCUMENT_IDENTITY_OPTION = "document_identity"

#: Every field a caller may declare. Explicitly enumerated rather than accepting
#: a free-form metadata blob: an open dictionary here would let a caller put an
#: Authorization header, a connection string or a local path into the payload
#: that is persisted with the job and echoed back through the API.
DOCUMENT_IDENTITY_FIELDS: tuple[str, ...] = (
    "source_system_id",
    "source_entity",
    "parent_record_id",
    "business_key_name",
    "business_key_value",
    "document_type",
    # Phase 10. Declared, never inferred from the filename or the content: a
    # classifier that guessed "birth_certificate" meant RESTRICTED would also
    # guess wrongly, and a wrong classification is worse than an absent one
    # because it looks authoritative.
    "sensitivity",
)

#: Rejected outright rather than silently dropped, so a caller who tries to
#: smuggle a credential through the identity fields is told, not ignored.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "authorization:",
    "bearer ",
    "password=",
    "postgresql://",
    "mysql://",
    "mongodb://",
    "mongodb+srv://",
)

#: Long enough for any real ERP key or entity name, short enough that a payload
#: is not a smuggling channel.
MAX_IDENTITY_VALUE_CHARS = 200


@dataclass(frozen=True)
class DocumentIdentity:
    """What the caller said this document belongs to. Possibly nothing."""

    source_system_id: str | None = None
    source_entity: str | None = None
    parent_record_id: str | None = None
    business_key_name: str | None = None
    business_key_value: str | None = None
    document_type: str | None = None
    #: A declared handling class. ``None`` means the caller said nothing, and
    #: the existing default applies - unchanged from every earlier phase.
    sensitivity: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(
            getattr(self, name) for name in DOCUMENT_IDENTITY_FIELDS
        )

    @property
    def has_business_key(self) -> bool:
        return bool(self.business_key_name and self.business_key_value)

    def to_options(self) -> dict[str, Any]:
        """The form carried on ``JobRequest.options``. Empty when nothing was said."""
        declared = {
            name: getattr(self, name)
            for name in DOCUMENT_IDENTITY_FIELDS
            if getattr(self, name) is not None
        }

        return {DOCUMENT_IDENTITY_OPTION: declared} if declared else {}

    def to_metadata(self) -> dict[str, Any]:
        """The identity fields as representation metadata.

        Only what was actually declared. A key that is absent stays absent
        rather than becoming ``None``, so a document with no business key is
        distinguishable from one whose key is unknown - and so a Phase 4 filter
        on a key this document does not have correctly excludes it.
        """
        return {
            name: getattr(self, name)
            for name in DOCUMENT_IDENTITY_FIELDS
            if getattr(self, name) is not None
        }

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "DocumentIdentity":
        """Read back what an upload declared, from a job's options."""
        declared = (options or {}).get(DOCUMENT_IDENTITY_OPTION) or {}

        if not isinstance(declared, Mapping):
            return cls()

        return cls(
            **{
                name: declared.get(name)
                for name in DOCUMENT_IDENTITY_FIELDS
                if declared.get(name) is not None
            }
        )

    @classmethod
    def declare(cls, **values: Any) -> "DocumentIdentity":
        """Validate a caller's declaration, or refuse it.

        Refuses rather than repairs. A half-declared business key is a mistake
        in the caller's request, and quietly dropping the half that arrived
        would index the document with an identity nobody intended.
        """
        cleaned: dict[str, str] = {}

        for name in DOCUMENT_IDENTITY_FIELDS:
            raw = values.get(name)

            if raw is None:
                continue

            text = str(raw).strip()

            if not text:
                # An empty form field is how a multipart client says "not
                # supplied"; it is not a declaration of emptiness.
                continue

            if len(text) > MAX_IDENTITY_VALUE_CHARS:
                raise InvalidPipelineRequestError(
                    f"{name!r} is longer than the {MAX_IDENTITY_VALUE_CHARS}-"
                    "character limit for a document identity field",
                    field=name,
                )

            lowered = text.lower()

            for marker in _FORBIDDEN_SUBSTRINGS:
                if marker in lowered:
                    raise InvalidPipelineRequestError(
                        f"{name!r} looks like a credential or connection "
                        "string; document identity fields carry ERP "
                        "identifiers only",
                        field=name,
                    )

            cleaned[name] = text

        # Validated against the enum, so a typo is a 4xx rather than a
        # silently-ignored field that leaves the document at the default.
        if cleaned.get("sensitivity"):
            from erp_pipeline.schemas.sensitivity import coerce

            try:
                cleaned["sensitivity"] = coerce(cleaned["sensitivity"]).value
            except ValueError:
                from erp_pipeline.schemas.enums import SensitivityLevel

                raise InvalidPipelineRequestError(
                    f"{cleaned['sensitivity']!r} is not a valid sensitivity; "
                    "expected one of: "
                    + ", ".join(level.value for level in SensitivityLevel),
                    field="sensitivity",
                ) from None

        identity = cls(**cleaned)

        # One declaration, two fields.
        if bool(identity.business_key_name) != bool(identity.business_key_value):
            missing = (
                "business_key_value"
                if identity.business_key_name
                else "business_key_name"
            )
            supplied = (
                "business_key_name"
                if identity.business_key_name
                else "business_key_value"
            )

            raise InvalidPipelineRequestError(
                f"{supplied!r} was supplied without {missing!r}. A business key "
                "is one declaration in two fields: half of it names nothing a "
                "search can match, so it is refused rather than stored.",
                field=missing,
            )

        return identity


__all__ = [
    "DOCUMENT_IDENTITY_FIELDS",
    "DOCUMENT_IDENTITY_OPTION",
    "MAX_IDENTITY_VALUE_CHARS",
    "DocumentIdentity",
]
