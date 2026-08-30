"""Stand-ins for Members 1, 2 and 3 (Phase 11).

WHY THESE LIVE IN THE TEST TREE
-------------------------------
Member 4 must be provable against the other three members without containing
any of them. A ``PolicyGateClient`` or an ERP execution client inside
``src/erp_pipeline`` would make the boundary an intention rather than a fact,
and would create exactly the circular runtime dependency the architecture
forbids. So the group actors are TEST DOUBLES, importable only by tests, and
``test_boundaries.py`` scans production code to prove no equivalent exists
there.

WHAT EACH FAKE IS FOR
---------------------
They are counters more than simulations. The interesting assertions of this
phase are arithmetic - the ERP was called once, Member 4 evaluated zero
policies, a denied operation executed zero times - so each fake records what it
was asked to do and refuses to do anything it was not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Read one recorded cross-member payload."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class PolicyViolation(RuntimeError):
    """The harness tried to execute something governance did not permit."""


# ----------------------------------------------------------------------
# Member 1 - the policy / governance gate
# ----------------------------------------------------------------------


@dataclass
class FakeMember1:
    """Answers whether an operation is permitted, and nothing else.

    It never touches Member 4. That is the architecture, not a simplification:
    a governance component that consulted the vector index for a transactional
    fact would be reading a snapshot whose freshness is bounded by a poll
    interval.
    """

    decision_fixture: str = "member1_allow.json"
    #: Conditions the harness has since satisfied, by id.
    satisfied: set[str] = field(default_factory=set)
    invocations: int = 0
    seen_operations: list[str] = field(default_factory=list)

    def evaluate(self, operation: str, subject: str | None = None) -> dict[str, Any]:
        self.invocations += 1
        self.seen_operations.append(operation)

        decision = load_fixture(self.decision_fixture)
        decision.pop("_comment", None)

        return decision

    def permits_execution(self, decision: Mapping[str, Any]) -> bool:
        """Whether Member 2 may proceed. Evaluated HERE, never in Member 4."""
        verdict = str(decision.get("decision", "")).upper()

        if verdict == "ALLOW":
            return True

        if verdict != "ALLOW_WITH_CONDITIONS":
            return False

        return all(
            condition.get("satisfied") or condition.get("id") in self.satisfied
            for condition in decision.get("conditions", ())
        )

    def satisfy(self, condition_id: str) -> None:
        self.satisfied.add(condition_id)


# ----------------------------------------------------------------------
# The legacy ERP, behind Member 2
# ----------------------------------------------------------------------


@dataclass
class FakeLegacyErp:
    """The system of record. Counts calls so double-execution is visible."""

    calls: int = 0
    call_log: list[str] = field(default_factory=list)

    def request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.call_log.append(endpoint)

        return payload


# ----------------------------------------------------------------------
# Member 2 - the ERPBridge / MCP bridge
# ----------------------------------------------------------------------


@dataclass
class FakeMember2:
    """Selects an ERP operation, executes it, then hands the RAW result to
    Member 4 for adaptation.

    The ordering is the contract: execute first, adapt second. Member 4 is
    never asked which endpoint to call, and is never given the credentials used
    to call it.
    """

    client: Any
    erp: FakeLegacyErp = field(default_factory=FakeLegacyErp)
    api_key: str | None = None
    #: The ERP credential. Held here, deliberately never forwarded.
    erp_authorization: str = "Bearer SUPER_SECRET_ERP_TOKEN"
    executions: int = 0
    adaptations: int = 0

    def execute(self, fixture_name: str) -> dict[str, Any]:
        """Call the live ERP. Member 2's job, and only Member 2's."""
        recorded = load_fixture(fixture_name)
        recorded.pop("_comment", None)
        self.executions += 1

        return self.erp.request(recorded["endpoint"], recorded)

    def adapt(
        self,
        raw: Mapping[str, Any],
        query: str,
        *,
        source_system_id: str = "legacy_hr",
        include_credentials: bool = False,
        body_base64: str | None = None,
        content_type: str | None = None,
    ) -> Any:
        """Send the raw ERP response to Member 4.

        ``include_credentials`` exists ONLY so a redaction test can prove the
        headers are scrubbed. The production contract does not send them.
        """
        self.adaptations += 1

        request: dict[str, Any] = {
            "query": query,
            "source_system_id": source_system_id,
            "endpoint": raw.get("endpoint"),
            "http_status": raw.get("http_status"),
            "content_type": content_type or raw.get("content_type"),
        }

        if body_base64 is not None:
            request["body_base64"] = body_base64
        else:
            request["body"] = raw.get("body")

        if include_credentials:
            request["headers"] = {
                "Authorization": self.erp_authorization,
                "Cookie": "session=SECRET_COOKIE",
                "X-ERP-Api-Secret": "SECRET_ERP_API_SECRET",
            }

        headers = {"X-API-Key": self.api_key} if self.api_key else {}

        return self.client.post("/v1/responses/adapt", json=request, headers=headers)


# ----------------------------------------------------------------------
# Member 3 - the frontend, through its trusted backend
# ----------------------------------------------------------------------


@dataclass
class FakeMember3:
    """The UI's server side - the BFF that holds the service API key.

    Modelled as a backend rather than a browser on purpose: the browser must
    never hold Member 4's service key, so the fake that does hold it is the
    trusted server tier.
    """

    client: Any
    api_key: str | None = None
    member1: FakeMember1 | None = None
    member2: FakeMember2 | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    # -- direct Member 4 calls (indexed knowledge) ---------------------

    def upload_document(
        self,
        filename: str,
        payload: bytes,
        media_type: str = "application/pdf",
        **identity: str,
    ) -> Any:
        return self.client.post(
            "/v1/files/documents",
            files={"file": (filename, payload, media_type)},
            data=identity,
            headers=self.headers,
        )

    def upload_csv(self, filename: str, payload: bytes, **form: str) -> Any:
        return self.client.post(
            "/v1/files/csv",
            files={"file": (filename, payload, "text/csv")},
            data=form,
            headers=self.headers,
        )

    def job(self, job_id: str) -> Any:
        return self.client.get(f"/v1/jobs/{job_id}", headers=self.headers)

    def search(self, query: str, **body: Any) -> Any:
        return self.client.post(
            "/v1/search", json={"query": query, **body}, headers=self.headers
        )

    def resolve(self, representation_id: str) -> Any:
        return self.client.get(
            f"/v1/representations/{representation_id}", headers=self.headers
        )

    # -- the governed live path (current facts) ------------------------

    def ask_live(self, question: str, operation: str, fixture: str) -> Any:
        """The full group flow: governance, then execution, then adaptation.

        Member 3 drives it. Member 4 appears only at the last step, and only
        after the ERP has already answered.
        """
        assert self.member1 is not None and self.member2 is not None

        decision = self.member1.evaluate(operation, subject=question)

        if not self.member1.permits_execution(decision):
            return decision

        raw = self.member2.execute(fixture)

        return self.member2.adapt(raw, query=question)


__all__ = [
    "FIXTURE_DIR",
    "FakeLegacyErp",
    "FakeMember1",
    "FakeMember2",
    "FakeMember3",
    "PolicyViolation",
    "load_fixture",
]
