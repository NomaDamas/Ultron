import json

import pytest

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, PolicyDenied, TriageApp
from ultron.evolution.planner import PendingVariationApproval
from ultron.evolution.variation import VariationPrimitive
from ultron.module.model import PromotionState
from ultron.registry.store import ModuleLifecycle
from ultron.ui.runtime import ComponentType


RAW_REQUEST = "RAW_REQ_SECRET_9d2c"
RAW_COMMENT = "RAW_COMMENT_SECRET_4b1a"


def _contains(value, needle: str) -> bool:
    return needle in json.dumps(value, default=str, sort_keys=True)


def _seed_usage(app: TriageApp, *, request=RAW_REQUEST, rating=1, comment=RAW_COMMENT):
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, request)
    app.submit_feedback(run["run_manifest"].run_id, rating=rating, comment=comment)
    return run


def test_personalization_summary_is_non_raw_and_deterministic():
    app = TriageApp()
    _seed_usage(app)

    summary = app.build_personalization_summary(DEFAULT_SCOPE, DEFAULT_WORKFLOW)
    summary_again = app.build_personalization_summary(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    dumped = summary.model_dump(mode="json")
    assert summary.summary_hash == summary_again.summary_hash
    assert dumped["n_runs"] == 1
    assert dumped["n_feedback"] == 1
    assert dumped["explicit_user_acceptances"] == 1
    assert dumped["redaction"]["raw_request_text"] is True
    assert dumped["redaction"]["raw_feedback_comments"] is True
    assert not _contains(dumped, RAW_REQUEST)
    assert not _contains(dumped, RAW_COMMENT)
    assert not _contains(dumped, "SECRET")


def test_personalize_registers_one_canary_without_promotion_or_raw_payload_and_rolls_back():
    app = TriageApp()
    _seed_usage(app)

    result = app.personalize(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    candidate = result["candidate"]
    proposal = result["proposal"]
    candidate_hash = candidate.content_hash
    assert proposal.primitive in set(VariationPrimitive)
    assert len(proposal.change) == 1
    assert not _contains(proposal.change, RAW_REQUEST)
    assert not _contains(candidate.model_dump(mode="json"), RAW_REQUEST)
    assert app.registry.get(candidate_hash).lifecycle is ModuleLifecycle.CANDIDATE
    assert candidate.fitness.promotion_state is PromotionState.CANDIDATE
    assert app.canary_active(result["canary_id"])
    assert result["promotable"] is False
    with pytest.raises(PolicyDenied):
        app.approve_promotion(candidate_hash, app.current_pointer_version())

    report = app.rollback_controller.rollback(result["canary_id"], actor="tester")
    assert report.dropped_namespaces
    assert app.canary_active(result["canary_id"]) is False


def test_more_usage_feedback_changes_summary_and_proposal_causally_without_auto_promote():
    app1 = TriageApp()
    _seed_usage(app1, request="first raw sentinel", rating=1, comment="first comment")
    summary1 = app1.build_personalization_summary(DEFAULT_SCOPE, DEFAULT_WORKFLOW)
    result1 = app1.personalize(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    app2 = TriageApp()
    _seed_usage(app2, request="first raw sentinel", rating=1, comment="first comment")
    _seed_usage(app2, request="second raw sentinel", rating=1, comment="second comment")
    summary2 = app2.build_personalization_summary(DEFAULT_SCOPE, DEFAULT_WORKFLOW)
    result2 = app2.personalize(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    assert summary1.summary_hash != summary2.summary_hash
    assert result1["proposal"].rationale != result2["proposal"].rationale
    assert len(result2["proposal"].change) == 1
    assert result2["promotable"] is False
    with pytest.raises(PolicyDenied):
        app2.approve_promotion(result2["candidate"].content_hash, app2.current_pointer_version())


def test_permission_expansion_summary_is_deferred_to_human_approval():
    app = TriageApp()
    for idx in range(3):
        _seed_usage(app, request=f"negative sentinel {idx}", rating=-1, comment=f"correction sentinel {idx}")

    result = app.personalize(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    assert isinstance(result, PendingVariationApproval)
    assert result.primitive is VariationPrimitive.TOOLSET_TOGGLE
    assert "permission expansion" in result.reason
    assert app.last_candidate_hash is None


def test_personalization_signal_card_is_non_raw_and_envelope_validates():
    app = TriageApp()
    run = _seed_usage(app)
    canary = app.propose_and_canary(VariationPrimitive.UI_PANEL_PRIORITY, {"ui_panel_contract_hash": "FEEDBACK_PANEL:40"}, request_text=RAW_REQUEST)

    envelope = app.build_inline_genui_envelope(run, canary)

    cards = [component for component in envelope.components if component.type is ComponentType.PERSONALIZATION_SIGNAL_CARD]
    assert len(cards) == 1
    props = cards[0].props
    assert props["signal_counts"]["runs"] >= 1
    assert props["summary_hash"].startswith("sha256:")
    assert not _contains(props, RAW_REQUEST)
    assert not _contains(props, RAW_COMMENT)
    assert envelope.manifest_signature_ok is True
