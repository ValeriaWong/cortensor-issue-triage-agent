"""Eval metrics + ground-truth mapping (pure, no model calls)."""

from issue_triage.eval import compute_metrics, ground_truth_type
from issue_triage.models import EvalRecord


def _record(truth, predicted, confidence=0.9):
    return EvalRecord(
        repo="o/r", issue_number=1, title="t", ground_truth=truth,
        predicted=predicted, severity="high", confidence=confidence,
        correct=truth == predicted,
    )


def test_ground_truth_mapping():
    assert ground_truth_type(["bug"]) == "bug"
    assert ground_truth_type(["enhancement"]) == "feature"
    assert ground_truth_type(["question"]) == "question"
    assert ground_truth_type(["documentation"]) == "docs"
    assert ground_truth_type(["area:brain"]) is None
    assert ground_truth_type([]) is None


def test_ground_truth_ambiguous_is_none():
    assert ground_truth_type(["bug", "enhancement"]) is None
    assert ground_truth_type(["bug", "bug"]) == "bug"


def test_metrics_perfect():
    records = [_record("bug", "bug"), _record("feature", "feature")]
    metrics = compute_metrics(records)
    assert metrics["accuracy"] == 1.0
    assert metrics["per_class"]["bug"]["precision"] == 1.0
    assert metrics["per_class"]["bug"]["recall"] == 1.0


def test_metrics_with_errors():
    records = [
        _record("bug", "bug"),
        _record("bug", "feature"),   # fn bug, fp feature
        _record("feature", "feature"),
        _record("question", "bug"),  # fn question, fp bug
    ]
    metrics = compute_metrics(records)
    assert metrics["accuracy"] == 0.5
    assert metrics["per_class"]["bug"]["precision"] == 0.5  # 1 tp / 2 predicted bug
    assert metrics["per_class"]["bug"]["recall"] == 0.5     # 1 tp / 2 true bug
    assert metrics["per_class"]["feature"]["recall"] == 1.0
    assert metrics["per_class"]["question"]["recall"] == 0.0


def test_calibration_buckets():
    records = [
        _record("bug", "bug", 0.9),
        _record("bug", "bug", 0.85),
        _record("bug", "feature", 0.4),
        _record("feature", "feature", 0.6),
    ]
    metrics = compute_metrics(records)
    buckets = metrics["calibration"]["buckets"]
    assert buckets[">=0.8"]["n"] == 2
    assert buckets[">=0.8"]["accuracy"] == 1.0
    assert buckets["<0.5"]["n"] == 1
    assert buckets["<0.5"]["accuracy"] == 0.0
    assert metrics["calibration"]["mean_confidence_correct"] == 0.783
    assert metrics["calibration"]["mean_confidence_wrong"] == 0.4


def test_metrics_empty():
    metrics = compute_metrics([])
    assert metrics["accuracy"] == 0.0
    assert metrics["n"] == 0
