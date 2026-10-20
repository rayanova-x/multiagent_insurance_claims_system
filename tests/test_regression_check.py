"""Unit tests for evaluation/regression_check.py's baseline-finding and
history logic - pure functions, no LLM calls, runs in the normal CI gate.
The actual live regression run (real batch evaluation) is a separate,
manually-triggered step - see .github/workflows/llm-eval.yml.
"""

from evaluation.regression_check import find_baseline


def _entry(model, config_sha, score=0.9, ts="t"):
    return {"model": model, "config_sha256": config_sha, "avg_eval_score": score, "timestamp": ts}


def test_no_history_means_no_baseline():
    assert find_baseline([], {"model": "m1", "config_sha256": "c1"}) is None


def test_finds_most_recent_entry_with_a_different_identity():
    history = [
        _entry("m1", "c1", ts="t1"),
        _entry("m1", "c1", ts="t2"),
        _entry("m2", "c1", ts="t3"),
    ]
    baseline = find_baseline(history, {"model": "m2", "config_sha256": "c1"})
    assert baseline["timestamp"] == "t2"


def test_same_identity_history_has_no_baseline():
    """Every entry shares the current run's exact model+config - there is
    no real swap to compare against, only same-config noise."""
    history = [_entry("m1", "c1", ts="t1"), _entry("m1", "c1", ts="t2")]
    assert find_baseline(history, {"model": "m1", "config_sha256": "c1"}) is None


def test_a_config_only_change_counts_as_a_different_identity():
    """Same model, different config fingerprint (e.g. a threshold edit in
    base.yaml) is still a real swap worth comparing against - config_sha256
    is part of the identity key, not just the model name."""
    history = [_entry("m1", "c1", ts="t1")]
    baseline = find_baseline(history, {"model": "m1", "config_sha256": "c2"})
    assert baseline is not None
    assert baseline["timestamp"] == "t1"


def test_most_recent_different_identity_wins_over_an_older_one():
    history = [
        _entry("m0", "c0", ts="t0"),
        _entry("m1", "c1", ts="t1"),
        _entry("m2", "c2", ts="t2"),
    ]
    baseline = find_baseline(history, {"model": "m3", "config_sha256": "c3"})
    assert baseline["timestamp"] == "t2"
