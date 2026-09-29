"""
Google Ads Automated Optimization Loop — apply/revert dispatcher tests.

The fake client below is NOT a shallow mock: get_type()/enums proxy to the
REAL installed google-ads SDK message/enum classes (proven in development
to construct correctly without needing OAuth — only a live network call
needs real credentials, which this test environment doesn't have). Only
get_service() is faked, to capture the built mutate operation instead of
sending it over the network. This means these tests exercise the actual
protobuf message construction and update_mask logic
(_gads_update_bid_sync/_gads_update_budget_sync/etc.), not just the
guardrail math around them.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from google.ads.googleads.v24.services.types.ad_group_criterion_service import AdGroupCriterionOperation
from google.ads.googleads.v24.services.types.campaign_budget_service import CampaignBudgetOperation
from google.ads.googleads.v24.enums.types.ad_group_criterion_status import AdGroupCriterionStatusEnum
from google.ads.googleads.v24.enums.types.keyword_match_type import KeywordMatchTypeEnum

from main import (
    _gads_apply_recommendation, _gads_revert_recommendation,
    _gads_update_bid_sync, _gads_update_budget_sync, _gads_add_negative_keyword_sync,
    _gads_add_phrase_match_variant_sync, _gads_remove_criterion_sync,
)

_TYPE_MAP = {"AdGroupCriterionOperation": AdGroupCriterionOperation, "CampaignBudgetOperation": CampaignBudgetOperation}


class _FakeEnumsNamespace:
    AdGroupCriterionStatusEnum = AdGroupCriterionStatusEnum.AdGroupCriterionStatus
    KeywordMatchTypeEnum = KeywordMatchTypeEnum.KeywordMatchType


class _FakeMutateService:
    def __init__(self, resource_name="customers/1/fakeResource/999", raise_exc=None):
        self.resource_name = resource_name
        self.raise_exc = raise_exc
        self.calls = []

    def _mutate(self, customer_id, operations):
        self.calls.append({"customer_id": customer_id, "operations": list(operations)})
        if self.raise_exc:
            raise self.raise_exc

        class _Result:
            resource_name = self.resource_name

        class _Response:
            results = [_Result()]
        return _Response()

    def mutate_ad_group_criteria(self, customer_id, operations):
        return self._mutate(customer_id, operations)

    def mutate_campaign_budgets(self, customer_id, operations):
        return self._mutate(customer_id, operations)


class _FakeClient:
    def __init__(self, resource_name="customers/1/fakeResource/999", raise_exc=None):
        self.enums = _FakeEnumsNamespace()
        self._service = _FakeMutateService(resource_name, raise_exc)

    def get_type(self, name):
        return _TYPE_MAP[name]()

    def get_service(self, name):
        return self._service


DEFAULT_SETTINGS = {"max_cpc_cap_micros": 50_000_000, "max_daily_budget_cap_micros": 500_000_000, "max_bid_change_pct": 30}


# ── low-level mutation functions — real message construction, fake network ──

def test_update_bid_sync_builds_a_real_update_operation_with_correct_mask():
    client = _FakeClient()
    rn = _gads_update_bid_sync(client, "1", "customers/1/adGroupCriteria/2~3", 8_000_000)
    assert rn == "customers/1/fakeResource/999"
    op = client._service.calls[0]["operations"][0]
    assert op.update.resource_name == "customers/1/adGroupCriteria/2~3"
    assert op.update.cpc_bid_micros == 8_000_000
    assert set(op.update_mask.paths) == {"resource_name", "cpc_bid_micros"}


def test_update_budget_sync_builds_a_real_update_operation_with_correct_mask():
    client = _FakeClient()
    rn = _gads_update_budget_sync(client, "1", "customers/1/campaignBudgets/2", 25_000_000)
    op = client._service.calls[0]["operations"][0]
    assert op.update.resource_name == "customers/1/campaignBudgets/2"
    assert op.update.amount_micros == 25_000_000
    assert set(op.update_mask.paths) == {"resource_name", "amount_micros"}


def test_add_negative_keyword_sync_creates_with_negative_true():
    client = _FakeClient()
    _gads_add_negative_keyword_sync(client, "1", "customers/1/adGroups/2", "irrelevant term")
    op = client._service.calls[0]["operations"][0]
    assert op.create.negative is True
    assert op.create.keyword.text == "irrelevant term"


def test_add_phrase_match_variant_sync_creates_without_negative():
    client = _FakeClient()
    _gads_add_phrase_match_variant_sync(client, "1", "customers/1/adGroups/2", "rare term")
    op = client._service.calls[0]["operations"][0]
    assert op.create.negative is False
    assert op.create.keyword.text == "rare term"


def test_remove_criterion_sync_sets_the_remove_field():
    client = _FakeClient()
    _gads_remove_criterion_sync(client, "1", "customers/1/adGroupCriteria/2~3")
    op = client._service.calls[0]["operations"][0]
    assert op.remove == "customers/1/adGroupCriteria/2~3"


# ── _gads_apply_recommendation: guardrail re-check ──────────────────────────

@pytest.mark.asyncio
async def test_apply_raise_cpc_succeeds_within_guardrails():
    rec = {"type": "raise_cpc", "current_value": "10000000", "proposed_value": "13000000", "target_resource": "customers/1/adGroupCriteria/2~3"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is True
    assert result["previous_value_micros"] == 10_000_000
    assert result["applied_resource_name"] == "customers/1/fakeResource/999"


@pytest.mark.asyncio
async def test_apply_raise_cpc_is_reclamped_when_stored_proposal_exceeds_current_cap():
    # Recommendation was generated when the cap was higher; settings tightened
    # since. current=12M, 30% step would allow 15.6M, but the now-tighter
    # 15M user cap binds first — the applied value must respect BOTH caps,
    # whichever is lower, not just re-apply the stale stored proposal.
    rec = {"type": "raise_cpc", "current_value": "12000000", "proposed_value": "80000000", "target_resource": "rn"}
    tighter_settings = {**DEFAULT_SETTINGS, "max_cpc_cap_micros": 15_000_000}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", tighter_settings)
    assert result["success"] is True
    applied_op = client._service.calls[0]["operations"][0]
    assert applied_op.update.cpc_bid_micros == 15_000_000  # capped down from the stale 80M proposal


@pytest.mark.asyncio
async def test_apply_raise_cpc_direct_jump_is_not_reclamped_to_the_30pct_step():
    # The recommendation was flagged direct_jump_to_estimate=True at
    # generation time (current bid was under 25% of the 10M top-of-page
    # estimate). At apply time, a plain 30% step would only allow 2.6M —
    # but the direct jump must still apply the full 10M, not get silently
    # clamped back down to the step.
    rec = {
        "type": "raise_cpc", "current_value": "2000000", "proposed_value": "10000000", "target_resource": "rn",
        "supporting_metrics": {"top_of_page_bid_high_micros": 10_000_000, "direct_jump_to_estimate": True},
    }
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is True
    applied_op = client._service.calls[0]["operations"][0]
    assert applied_op.update.cpc_bid_micros == 10_000_000


@pytest.mark.asyncio
async def test_apply_raise_cpc_direct_jump_is_still_capped_by_the_current_user_cpc_cap():
    # Guardrails tightened since the recommendation was generated: the user
    # cap is now 5M, below the 10M top-of-page estimate — the cap must win
    # even though this is a direct-jump recommendation.
    rec = {
        "type": "raise_cpc", "current_value": "2000000", "proposed_value": "10000000", "target_resource": "rn",
        "supporting_metrics": {"top_of_page_bid_high_micros": 10_000_000, "direct_jump_to_estimate": True},
    }
    tighter_settings = {**DEFAULT_SETTINGS, "max_cpc_cap_micros": 5_000_000}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", tighter_settings)
    assert result["success"] is True
    applied_op = client._service.calls[0]["operations"][0]
    assert applied_op.update.cpc_bid_micros == 5_000_000


@pytest.mark.asyncio
async def test_apply_raise_cpc_fails_cleanly_when_guardrails_no_longer_allow_any_increase():
    rec = {"type": "raise_cpc", "current_value": "50000000", "proposed_value": "60000000", "target_resource": "rn"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)  # cap is 50M, current already there
    assert result["success"] is False
    assert "guardrails" in result["error"].lower()
    assert client._service.calls == []  # never attempted a mutation


@pytest.mark.asyncio
async def test_apply_raise_budget_succeeds_within_guardrails():
    rec = {"type": "raise_budget", "current_value": "100000000", "proposed_value": "130000000", "target_resource": "rn"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is True
    assert result["previous_value_micros"] == 100_000_000


@pytest.mark.asyncio
async def test_apply_add_negative_keyword_has_no_previous_value_to_snapshot():
    rec = {"type": "add_negative_keyword", "proposed_value": "irrelevant term", "target_resource": "customers/1/adGroups/2"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is True
    assert result["previous_value_micros"] is None
    assert result["applied_resource_name"] == "customers/1/fakeResource/999"


@pytest.mark.asyncio
async def test_apply_alert_only_type_succeeds_without_any_mutation_call():
    rec = {"type": "fix_disapproved_ad", "target_resource": "campaign/123/disapproved_ads"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is True
    assert result["applied_resource_name"] is None
    assert client._service.calls == []


@pytest.mark.asyncio
async def test_apply_surfaces_a_real_mutation_exception_as_a_failure_not_a_crash():
    rec = {"type": "raise_cpc", "current_value": "10000000", "proposed_value": "13000000", "target_resource": "rn"}
    client = _FakeClient(raise_exc=RuntimeError("simulated Ads API error"))
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is False
    assert "simulated Ads API error" in result["error"]


@pytest.mark.asyncio
async def test_apply_unknown_type_fails_cleanly():
    rec = {"type": "totally_made_up_type", "target_resource": "rn"}
    client = _FakeClient()
    result = await _gads_apply_recommendation(rec, client, "1", DEFAULT_SETTINGS)
    assert result["success"] is False


# ── _gads_revert_recommendation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revert_raise_cpc_restores_the_previous_bid():
    rec = {"type": "raise_cpc", "previous_value_micros": 10_000_000, "applied_resource_name": "customers/1/adGroupCriteria/2~3", "target_resource": "customers/1/adGroupCriteria/2~3"}
    client = _FakeClient()
    result = await _gads_revert_recommendation(rec, client, "1")
    assert result["success"] is True
    op = client._service.calls[0]["operations"][0]
    assert op.update.cpc_bid_micros == 10_000_000


@pytest.mark.asyncio
async def test_revert_negative_keyword_removes_the_created_criterion():
    rec = {"type": "add_negative_keyword", "applied_resource_name": "customers/1/adGroupCriteria/2~3"}
    client = _FakeClient()
    result = await _gads_revert_recommendation(rec, client, "1")
    assert result["success"] is True
    op = client._service.calls[0]["operations"][0]
    assert op.remove == "customers/1/adGroupCriteria/2~3"


@pytest.mark.asyncio
async def test_revert_alert_only_type_fails_with_a_clear_message():
    rec = {"type": "under_delivery_alert"}
    client = _FakeClient()
    result = await _gads_revert_recommendation(rec, client, "1")
    assert result["success"] is False
    assert "no applied change" in result["error"].lower()


@pytest.mark.asyncio
async def test_revert_without_a_recorded_previous_value_fails_cleanly():
    rec = {"type": "raise_cpc", "previous_value_micros": None}
    client = _FakeClient()
    result = await _gads_revert_recommendation(rec, client, "1")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_revert_without_a_recorded_applied_resource_fails_cleanly():
    rec = {"type": "add_negative_keyword", "applied_resource_name": None}
    client = _FakeClient()
    result = await _gads_revert_recommendation(rec, client, "1")
    assert result["success"] is False
