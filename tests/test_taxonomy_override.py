"""
P0.4 tests. The audited "Universal Biotechnology" report was classified
`one_time` despite its own evidence text describing a reagent/kit/antibody
consumables business — this fixture's WRONG_REVENUE_MODEL + EVIDENCE_TEXT
must fail check_purchase_type_override, per the spec's own instruction
that the fixture fail every new check.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report_validators import (
    check_purchase_type_override,
    normalize_purchase_type,
    assert_retention_budget,
)
from tests.fixtures.universal_biotechnology import EVIDENCE_TEXT, WRONG_REVENUE_MODEL


def test_reagent_kit_evidence_triggers_recurring_override():
    assert WRONG_REVENUE_MODEL == "one_time"  # the audited (wrong) classification
    override = check_purchase_type_override(EVIDENCE_TEXT)
    assert override == "recurring_consumable"


def test_no_recurring_keywords_means_no_override():
    evidence = "We sell industrial CNC machines with a lifetime warranty and one-time installation."
    assert check_purchase_type_override(evidence) is None


def test_empty_evidence_means_no_override():
    assert check_purchase_type_override("") is None
    assert check_purchase_type_override(None) is None


def test_override_matches_case_insensitively():
    assert check_purchase_type_override("We sell REAGENTS and lab KITS.") == "recurring_consumable"


def test_normalize_maps_old_freeform_values_onto_fixed_enum():
    assert normalize_purchase_type("One-time") == "one_time"
    assert normalize_purchase_type("Subscription") == "subscription"
    assert normalize_purchase_type("Freemium") == "subscription"
    assert normalize_purchase_type("Commission") == "project_based"
    assert normalize_purchase_type("Project-based") == "project_based"


def test_normalize_defaults_ambiguous_mixed_to_project_based():
    assert normalize_purchase_type("Mixed") == "project_based"
    assert normalize_purchase_type("") == "project_based"
    assert normalize_purchase_type("something the model invented") == "project_based"


def test_normalize_passes_through_already_correct_enum_values():
    assert normalize_purchase_type("recurring_consumable") == "recurring_consumable"
    assert normalize_purchase_type("recurring consumable") == "recurring_consumable"


# ── assert_retention_budget ─────────────────────────────────────────────

def test_one_time_business_is_never_checked():
    media_plan = {"budget_allocation": "100% to Google Search acquisition campaigns."}
    assert assert_retention_budget(media_plan, "one_time") is None


def test_project_based_business_is_never_checked():
    media_plan = {"budget_allocation": "100% to Google Search acquisition campaigns."}
    assert assert_retention_budget(media_plan, "project_based") is None


def test_recurring_consumable_business_with_100pct_acquisition_fails():
    media_plan = {"budget_allocation": "Google 60%, Meta 40% — all toward new lead generation."}
    error = assert_retention_budget(media_plan, "recurring_consumable")
    assert error is not None
    assert "retention" in error.lower() or "acquisition" in error.lower()


def test_recurring_consumable_business_with_empty_budget_allocation_fails():
    media_plan = {"budget_allocation": ""}
    assert assert_retention_budget(media_plan, "recurring_consumable") is not None


def test_recurring_consumable_business_with_a_named_retention_line_passes():
    media_plan = {"budget_allocation": "Google 50%, Meta 30%, and 20% reserved for existing-customer reorder reminders and dormant-account reactivation."}
    assert assert_retention_budget(media_plan, "recurring_consumable") is None


def test_subscription_business_with_reactivation_line_passes():
    media_plan = {"budget_allocation": "80% acquisition, 20% win-back campaigns for churned subscribers."}
    assert assert_retention_budget(media_plan, "subscription") is None
