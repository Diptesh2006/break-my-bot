from __future__ import annotations

from collections import Counter, defaultdict

from .models import RunRecord

METHODOLOGY_VERSION = "breakmybot-score-v3"
CATEGORY_WEIGHTS = {
    "HTTP reliability": 0.20,
    "Response validity": 0.10,
    "Latency": 0.10,
    "Tool reliability": 0.20,
    "Memory integrity": 0.15,
    "Security": 0.20,
    "Output correctness": 0.05,
}


def calculate(run: RunRecord) -> dict:
    states = Counter(s.status for s in run.scenarios)
    conclusive = [s for s in run.scenarios if s.status in {"PASSED", "VERIFIED_FAILURE"}]
    supported = len(conclusive)
    eligible = len(run.scenarios)
    coverage = round(supported / eligible * 100) if eligible else 0
    category_groups: dict[str, list] = defaultdict(list)
    category_totals: Counter[str] = Counter()
    for scenario in run.scenarios:
        category_totals[scenario.category] += 1
        if scenario.status in {"PASSED", "VERIFIED_FAILURE"}:
            category_groups[scenario.category].append(scenario)
    category_scores = {}
    for category, total in category_totals.items():
        rows = category_groups.get(category, [])
        category_scores[category] = {
            "score": round(sum(row.status == "PASSED" for row in rows) / len(rows) * 100) if rows else None,
            "conclusive": len(rows),
            "coverage_percent": round(len(rows) / total * 100) if total else 0,
            "weight": CATEGORY_WEIGHTS.get(category),
        }
    for category, weight in CATEGORY_WEIGHTS.items():
        category_scores.setdefault(category, {"score": None, "conclusive": 0,
            "coverage_percent": 0, "weight": weight})

    observed_scopes = (["black-box HTTP"] if any(s.category in
        {"HTTP reliability", "Response validity", "Latency"} and s.status in {"PASSED", "VERIFIED_FAILURE"}
        for s in run.scenarios) else [])
    if any(s.category == "Tool reliability" and s.status in {"PASSED", "VERIFIED_FAILURE"} for s in run.scenarios):
        observed_scopes.append("instrumented tool faults")
    if any(s.scenario_id.startswith("memoryscope-") and s.status in {"PASSED", "VERIFIED_FAILURE"}
           for s in run.scenarios):
        observed_scopes.append("MemoryScope-grounded answer checks")

    weighted_categories = [(CATEGORY_WEIGHTS.get(category), item["score"])
        for category, item in category_scores.items()
        if CATEGORY_WEIGHTS.get(category) is not None and item["score"] is not None]
    observed_weight = sum(weight for weight, _ in weighted_categories)
    score = (round(sum(weight * value for weight, value in weighted_categories) / observed_weight)
             if supported >= 3 and observed_weight >= 0.5 else None)
    untested = ["persistent memory lifecycle and access boundaries", "security policy behavior"]
    if "instrumented tool faults" not in observed_scopes:
        untested.insert(0, "internal tool fault injection")
    return {
        "methodology": METHODOLOGY_VERSION,
        "category_weights": CATEGORY_WEIGHTS,
        "overall_score": score,
        "score_label": "Observed reliability score" if score is not None else "Not established",
        "score_eligibility_threshold_observed_weight_percent": 50,
        "observed_weight_percent": round(observed_weight * 100),
        "observed_scopes": observed_scopes,
        "category_scores": category_scores,
        "coverage_percent": coverage,
        "conclusive_scenarios": supported,
        "scenario_counts": {state: states.get(state, 0) for state in
            ("PASSED", "VERIFIED_FAILURE", "INCONCLUSIVE", "SETUP_ERROR", "UNSUPPORTED", "SKIPPED")},
        "critical_findings": sum(s.status == "VERIFIED_FAILURE" and s.severity == "critical" for s in run.scenarios),
        "warning": "This weighted score renormalizes over observed weighted categories only; it summarizes listed evidence, not a safety probability or universal certification. See coverage and untested capabilities.",
        "untested_capabilities": untested,
    }
