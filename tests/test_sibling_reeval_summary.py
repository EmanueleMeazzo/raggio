"""Guard scientific conclusions against incomplete runs and A/A noise."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
from sibling_reeval_summary import analyze


def recording(mode, change=0):
    key = "sofia-summary/c8"
    arms = ("a", "b") if mode == "aa" else ("a", "c")
    base = {"p50_ms": 10, "server_cpu_ms": 4, "qps": 100,
            "requests": 100, "host_busy_fraction": .15}
    comparison = {**base, "p50_ms": 10 * (1 + change / 100),
                  "server_cpu_ms": 4 * (1 + change / 100), "qps": 100 * (1 - change / 100)}
    summaries = {m: {"change_pct": change if m != "qps" else -change,
                    "paired_median_95_ci_pct": [change, change] if m != "qps" else [-change, -change]}
                 for m in ("p50_ms", "server_cpu_ms", "qps")}
    return {"response_parity": True, "pairs": 7,
            "runs": {key: [{arms[0]: deepcopy(base), arms[1]: deepcopy(comparison)} for _ in range(7)]},
            "summary": {key: summaries}}


def test_incomplete_or_mismatched_runs_cannot_produce_a_verdict():
    aa, ab = recording("aa"), recording("ab")
    ab["response_parity"] = False
    with pytest.raises(ValueError, match="parity"):
        analyze(aa, ab)
    ab["response_parity"] = True
    ab["runs"]["sofia-summary/c8"].pop()
    with pytest.raises(ValueError, match="missing paired"):
        analyze(aa, ab)


def test_consistent_control_loss_above_tolerance_fails_all_relevant_gates():
    verdict = analyze(recording("aa"), recording("ab", 5))["gates"]["sofia-summary/c8"]
    assert all(v["regression_gate_failure"] for v in verdict.values())
    assert not any(v["gain_beyond_noise"] for v in verdict.values())


def test_an_interval_excluding_zero_does_not_override_larger_aa_noise():
    verdict = analyze(recording("aa", 10), recording("ab", -5))["gates"]["sofia-summary/c8"]
    assert not any(v["gain_beyond_noise"] for v in verdict.values())


def test_gain_requires_both_noise_and_interval_gates():
    ab = recording("ab", -5)
    verdict = analyze(recording("aa"), ab)["gates"]["sofia-summary/c8"]
    assert all(v["gain_beyond_noise"] for v in verdict.values())
    ab["summary"]["sofia-summary/c8"]["p50_ms"]["paired_median_95_ci_pct"] = [-6, 1]
    assert not analyze(recording("aa"), ab)["gates"]["sofia-summary/c8"]["p50_ms"]["gain_beyond_noise"]
