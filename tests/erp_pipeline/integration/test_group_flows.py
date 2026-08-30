"""Tests F-I: the four-member flows, with Member 4 real and the rest faked.

WHAT THESE TESTS ARE ACTUALLY ASSERTING
---------------------------------------
Arithmetic, mostly. The architecture claims are countable:

* the ERP is executed once, by Member 2, and never by Member 4
* Member 4 evaluates zero policies
* a denied operation executes zero times and adapts zero times
* an unsatisfied condition executes zero times

A flow that produced the right answer while calling the ERP twice, or while
Member 4 quietly decided something was allowed, would pass a happy-path test
and fail the architecture. So each test counts.
"""

from __future__ import annotations

import pytest

from tests.erp_pipeline.integration.fakes import FakeMember1, FakeMember2, FakeMember3


@pytest.fixture
def group(client):
    """A whole group: real Member 4, faked 1, 2 and 3."""

    def build(decision_fixture: str = "member1_allow.json"):
        from tests.erp_pipeline.integration.conftest import SERVICE_API_KEY

        member1 = FakeMember1(decision_fixture=decision_fixture)
        member2 = FakeMember2(client=client, api_key=SERVICE_API_KEY)
        member3 = FakeMember3(
            client=client,
            api_key=SERVICE_API_KEY,
            member1=member1,
            member2=member2,
        )

        return member1, member2, member3

    return build


# ----------------------------------------------------------------------
# F - the live read: "Show EMP002's current employment status."
# ----------------------------------------------------------------------


class TestFLiveReadFlow:
    """M3 -> M1 -> M2 -> ERP -> M2 -> M4 adapt."""

    @pytest.fixture
    def executed(self, group):
        member1, member2, member3 = group()
        response = member3.ask_live(
            "Show EMP002's current employment status.",
            operation="hr.employee.read",
            fixture="member2_employee_response.json",
        )

        return member1, member2, response

    def test_the_flow_returns_an_adapted_answer(self, executed):
        _, _, response = executed

        assert response.status_code == 200, response.text
        body = response.json()

        assert body["success"] is True
        assert "ACTIVE" in str(body["llm_ready"]).upper()

    def test_governance_was_consulted_exactly_once(self, executed):
        member1, _, _ = executed

        assert member1.invocations == 1

    def test_the_erp_was_executed_exactly_once(self, executed):
        _, member2, _ = executed

        assert member2.executions == 1
        assert member2.erp.calls == 1

    def test_member4_adapted_exactly_once(self, executed):
        _, member2, _ = executed

        assert member2.adaptations == 1

    def test_member4_executed_no_erp_operation(self, executed):
        """The hard gate. Member 4's only ERP contact is the recorded bytes."""
        _, member2, _ = executed

        assert member2.erp.call_log == ["/api/hr/employees/EMP002"], (
            f"unexpected ERP traffic: {member2.erp.call_log}"
        )

    def test_member4_made_no_policy_decision(self, executed):
        """Every governance decision came from Member 1's counter."""
        member1, _, _ = executed

        assert member1.invocations == 1
        assert member1.seen_operations == ["hr.employee.read"]

    def test_the_erp_credential_never_reached_member4(self, executed, client):
        """Member 2 holds the token and does not forward it."""
        _, member2, response = executed

        assert "SUPER_SECRET_ERP_TOKEN" not in response.text
        assert member2.erp_authorization.startswith("Bearer ")


# ----------------------------------------------------------------------
# H - the allowed write: "Release payment INV-204."
# ----------------------------------------------------------------------


class TestHAllowedWriteFlow:
    def test_an_allowed_write_executes_once_and_adapts_once(self, group):
        member1, member2, member3 = group("member1_allow.json")

        response = member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        assert response.status_code == 200, response.text
        assert member1.invocations == 1
        assert member2.executions == 1, "the write executed more than once"
        assert member2.erp.calls == 1
        assert member2.adaptations == 1

    def test_the_adapted_confirmation_carries_the_result(self, group):
        _, _, member3 = group("member1_allow.json")

        body = member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        ).json()

        rendered = str(body["llm_ready"])

        assert "INV-204" in rendered
        assert "RELEASED" in rendered.upper()

    def test_member4_did_not_authorize_the_write(self, group):
        """Member 4 saw the result of a decision, never made one."""
        member1, member2, member3 = group("member1_allow.json")

        member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        # The whole decision record lives in Member 1.
        assert member1.seen_operations == ["finance.payment.release"]
        assert member2.adaptations == 1


# ----------------------------------------------------------------------
# G - the denied write
# ----------------------------------------------------------------------


class TestGDeniedWriteFlow:
    @pytest.fixture
    def denied(self, group):
        member1, member2, member3 = group("member1_deny.json")
        result = member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        return member1, member2, result

    def test_the_denial_is_returned_rather_than_a_result(self, denied):
        _, _, result = denied

        assert result["decision"] == "DENY"
        assert result["reason"]

    def test_the_erp_was_never_executed(self, denied):
        """The hard gate: a denied operation must not reach the ERP."""
        _, member2, _ = denied

        assert member2.executions == 0
        assert member2.erp.calls == 0

    def test_member4_never_adapted_anything(self, denied):
        _, member2, _ = denied

        assert member2.adaptations == 0

    def test_member4_could_not_have_caused_execution(self, denied):
        """Member 4 is not in the path at all before the decision.

        Stated as a test because the failure mode it guards against is
        architectural rather than behavioural: if Member 4 were consulted
        first, or held any part of the decision, a denial could still leave
        side effects behind it.
        """
        member1, member2, _ = denied

        assert member1.invocations == 1
        assert member2.erp.call_log == []


# ----------------------------------------------------------------------
# I - allow_with_conditions, unsatisfied
# ----------------------------------------------------------------------


class TestIUnsatisfiedConditionFlow:
    def test_an_unsatisfied_condition_blocks_execution(self, group):
        member1, member2, member3 = group("member1_allow_with_conditions.json")

        result = member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        assert result["decision"] == "ALLOW_WITH_CONDITIONS"
        assert member2.executions == 0, "an unsatisfied condition still executed"
        assert member2.erp.calls == 0
        assert member2.adaptations == 0

    def test_execution_proceeds_once_the_condition_is_satisfied(self, group):
        member1, member2, member3 = group("member1_allow_with_conditions.json")

        member1.satisfy("dual-approval")
        response = member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        assert response.status_code == 200, response.text
        assert member2.executions == 1
        assert member2.adaptations == 1

    def test_member4_never_evaluated_the_condition(self, group):
        """The condition is Member 1's vocabulary; Member 4 has no word for it."""
        member1, member2, member3 = group("member1_allow_with_conditions.json")

        member3.ask_live(
            "Release payment INV-204.",
            operation="finance.payment.release",
            fixture="member2_invoice_response.json",
        )

        # Member 4 was never called, so it cannot have evaluated anything.
        assert member2.adaptations == 0
        assert member1.invocations == 1


# ----------------------------------------------------------------------
# The distinction the group demo exists to show
# ----------------------------------------------------------------------


class TestLiveVersusIndexedKnowledge:
    """A current fact and an indexed fact come from different members."""

    def test_a_current_fact_comes_from_member2_not_the_index(self, group, client):
        """Member 4's index is never consulted for a transactional fact."""
        _, member2, member3 = group()

        member3.ask_live(
            "Show EMP002's current employment status.",
            operation="hr.employee.read",
            fixture="member2_employee_response.json",
        )

        # The answer came from the ERP, and the vector index was never queried
        # for it: nothing has been indexed in this deployment at all.
        assert member2.erp.calls == 1

        indexed = member3.search("EMP002 employment status")

        assert indexed.status_code == 200
        assert indexed.json()["hits"] == [], (
            "a live-read flow indexed something as a side effect"
        )

    def test_adaptation_does_not_index_the_erp_response(self, group):
        """``/v1/responses/adapt`` is stateless. It must not write vectors."""
        _, member2, member3 = group()

        raw = member2.execute("member2_employee_response.json")
        member2.adapt(raw, query="employment status")

        found = member3.search("Nimal Silva Finance")

        assert found.json()["hits"] == [], (
            "response adaptation persisted the ERP response into the index"
        )
