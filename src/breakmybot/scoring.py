from __future__ import annotations

from collections import Counter, defaultdict

from .models import RunRecord

METHODOLOGY_VERSION = "breakmybot-score-v1"


def calculate(run: RunRecord) -> dict:
    states = Counter(s.status for s in run.scenarios)
    conclusive = [s for s in run.scenarios if s.status in {"PASSED", "VERIFIED_FAILURE"}]
    supported = len(conclusive)
    eligible = len(run.scenarios)
    coverage = round(supported / eligible * 100) if eligible else 0
    score = round(sum(s.status == "PASSED" for s in conclusive) / supported * 100) if supported >= 3 else None
    category_groups: dict[str, list] = defaultdict(list)
    category_totals: Counter[str] = Counter()
    for scenario in run.scenarios:
        category_totals[scenario.category] += 1
        if scenario.status in {"PASSED", "VERIFIED_FAILURE"}:
            category_groups[scenario.category].append(scenario)
    category_scores = {
        category: {
            "score": round(sum(row.status == "PASSED" for row in rows) / len(rows) * 100),
            "conclusive": len(rows),
            "coverage_percent": round(len(rows) / category_totals[category] * 100),
        }
        for category, rows in category_groups.items()
    }
    return {
        "methodology": METHODOLOGY_VERSION,
        "overall_score": score,
        "score_label": "HTTP reliability score" if score is not None else "Not established",
        "category_scores": category_scores,
        "coverage_percent": coverage,
        "conclusive_scenarios": supported,
        "scenario_counts": {state: states.get(state, 0) for state in
            ("PASSED", "VERIFIED_FAILURE", "INCONCLUSIVE", "SETUP_ERROR", "UNSUPPORTED", "SKIPPED")},
        "critical_findings": sum(s.status == "VERIFIED_FAILURE" and s.severity == "critical" for s in run.scenarios),
        "warning": "This score summarizes only observed HTTP checks; it is not a safety probability or universal certification.",
        "untested_capabilities": ["internal tool fault injection", "memory lifecycle and access boundaries", "security policy behavior"],
    }
