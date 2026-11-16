"""Verdict / draft model validation: illegal values must fail loudly."""

import pytest
from pydantic import ValidationError

from issue_triage.models import TriageDraft, TriageVerdict

BASE = {
    "issue_type": "bug",
    "severity": "high",
    "component": "area:brain",
    "route": "ready-to-fix",
    "confidence": 0.8,
    "rationale": "test",
}


def test_valid_verdict():
    v = TriageVerdict.model_validate(BASE)
    assert v.issue_type == "bug"
    assert v.suggested_labels == []
    assert v.assignee is None


def test_illegal_issue_type_rejected():
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({**BASE, "issue_type": "failure"})


def test_illegal_severity_rejected():
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({**BASE, "severity": "urgent"})


def test_illegal_route_rejected():
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({**BASE, "route": "ship-it"})


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({**BASE, "confidence": 1.5})
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({**BASE, "confidence": -0.1})


def test_confidence_bounds_accepted():
    assert TriageVerdict.model_validate({**BASE, "confidence": 0}).confidence == 0
    assert TriageVerdict.model_validate({**BASE, "confidence": 1}).confidence == 1


def test_all_issue_types_accepted():
    for t in ("bug", "feature", "question", "docs", "chore", "duplicate"):
        assert TriageVerdict.model_validate({**BASE, "issue_type": t}).issue_type == t


def test_draft_requires_text():
    with pytest.raises(ValidationError):
        TriageDraft.model_validate({})
    assert TriageDraft.model_validate({"draft": "hi"}).draft == "hi"
