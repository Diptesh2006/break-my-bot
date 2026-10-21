from benchmarks.verify_demo import _metrics


def _attempt(status: str, run_status: str = "COMPLETED", duration_ms: float | None = 12.0) -> dict:
    return {"scenario_status": status, "run_status": run_status, "duration_ms": duration_ms}


def test_benchmark_metrics_do_not_count_setup_or_inconclusive_control_as_false_positive():
    cases = [
        {"expected_scenario_status": "PASSED", "attempts": [
            _attempt("PASSED"), _attempt("INCONCLUSIVE"),
            _attempt("SETUP_ERROR", run_status="FAILED"), _attempt("VERIFIED_FAILURE"),
        ]},
        {"expected_scenario_status": "VERIFIED_FAILURE", "outcome_match": True,
         "reproduced_on_both_attempts": True, "attempts": [
             _attempt("VERIFIED_FAILURE"), _attempt("VERIFIED_FAILURE"),
             _attempt("SETUP_ERROR", run_status="FAILED", duration_ms=None),
         ]},
    ]

    metrics = _metrics(cases)

    assert metrics["false_positive_rate_on_fixed_control"] == 0.25
    assert metrics["scenario_validity_rate"] == 4 / 7
    assert metrics["verified_failure_rate_on_known_bug_attempts"] == 2 / 3
    assert metrics["run_execution_success_rate"] == 5 / 7
    assert metrics["known_bug_conditions_detected"] == 1
    assert metrics["known_bug_attempts_verified"] == 2


def test_benchmark_metrics_report_missing_denominators_without_inventing_rates():
    metrics = _metrics([])

    assert metrics["known_bug_conditions_detected"] == 0
    assert metrics["scenario_validity_rate"] == 0
    assert metrics["run_execution_success_rate"] == 0
    assert metrics["false_positive_rate_on_fixed_control"] is None
    assert metrics["verified_failure_rate_on_known_bug_attempts"] is None
    assert metrics["mean_target_time_per_verified_finding_ms"] is None
