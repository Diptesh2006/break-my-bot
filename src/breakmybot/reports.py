from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import urljoin

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import RunRecord

REPORT_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_URI = "https://raw.githubusercontent.com/Diptesh2006/break-my-bot/main/docs/report-schema.json"


def _finding_expectation(scenario) -> str:
    evidence = scenario.evidence
    if evidence.get("expected_substring_passed") is False:
        return "The response should contain the configured expected substring."
    if evidence.get("matched") is False:
        expected = evidence.get("expected_substring")
        return f"The response should satisfy the configured literal invariant: {expected}." if expected else "The configured response invariant should hold."
    if evidence.get("fault_injected"):
        return "The agent should preserve its declared safe behavior under the confirmed tool fault."
    if evidence.get("assertion"):
        return str(evidence["assertion"])
    return "No explicit expected-behavior invariant was recorded; interpret this observation within the stated scenario scope."


def _finding_recommendation(scenario) -> str:
    return {
        "HTTP reliability": "Inspect target availability, request handling, and safe timeout/retry behavior; add a regression test for this observed response.",
        "Response validity": "Return a response matching the declared content type and response contract, and cover the malformed case with a regression test.",
        "Latency": "Profile the affected path and define a latency budget consistent with user-facing requirements.",
        "Output correctness": "Correct the response against the supplied invariant or reference, then add the case to the target application's regression suite.",
        "Tool reliability": "Handle the confirmed dependency fault with a safe fallback, bounded retry policy, and idempotency protection where actions may repeat.",
        "Memory integrity": "Review the observed memory state and retrieval/update policy, then add a resettable regression fixture.",
        "Security": "Review the violated boundary and enforce it at the application authorization layer; add a negative authorization regression test.",
    }.get(scenario.category, "Review the recorded observation and add a focused regression test for the affected behavior.")


def _finding_reproduction(run: RunRecord, endpoint: str, scenario) -> str:
    steps = [f"1. Use the isolated target endpoint: {run.method} {endpoint}.",
        f"2. Send the recorded redacted payload: {json.dumps(run.request_payload, ensure_ascii=False)}."]
    faults = scenario.evidence.get("faults")
    if faults:
        steps.append(f"3. Ordered fault sequence: configure the target's opt-in BreakMyBot adapter with {json.dumps(faults, ensure_ascii=False)}.")
    else:
        steps.append("3. Repeat only under the same isolated target conditions; no internal fault injection was recorded for this scenario.")
    steps.append(f"4. Compare the observed response with the invariant: {_finding_expectation(scenario)}")
    return " ".join(steps)


def generate(run: RunRecord, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    html_path = output_dir / "report.html"
    pdf_path = output_dir / "report.pdf"
    target_endpoint = urljoin(run.target_url.rstrip("/") + "/", run.endpoint_path.lstrip("/"))
    payload = run.model_dump(mode="json")
    payload["$schema"] = REPORT_SCHEMA_URI
    payload["report_schema_version"] = REPORT_SCHEMA_VERSION
    payload["report_paths"] = {kind: f"report.{kind}" for kind in ("html", "pdf", "json")}
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    scenario_rows = "".join(
        f"<tr><td>{html.escape(s.scenario_id)}</td><td>{html.escape(s.name)}</td><td>{html.escape(s.category)}</td><td>{html.escape(s.status)}</td><td>{html.escape(str(s.http_status or '—'))}</td><td>{html.escape(s.limitation or '')}</td></tr>"
        for s in run.scenarios
    )
    category_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(item['score']) + '/100' if item['score'] is not None else 'Not established')}</td><td>{item['coverage_percent']}%</td><td>{item['conclusive']}</td></tr>"
        for name, item in run.scoring.get("category_scores", {}).items()
    ) or "<tr><td colspan='4'>No category has enough conclusive observations yet.</td></tr>"
    finding_rows = "".join(
        f"<article><h3>{html.escape(s.name)} · {html.escape(s.severity)}</h3><p>Category: {html.escape(s.category)}. Scenario: {html.escape(s.scenario_id)}.</p><p>Expected behavior: {html.escape(_finding_expectation(s))}</p><p>Actual observation: HTTP {html.escape(str(s.http_status or 'not available'))}; {html.escape(str(s.duration_ms or 'N/A'))} ms; response evidence: <code>{html.escape(str(s.evidence.get('response_excerpt', 'No response excerpt recorded.')))}</code>.</p><p>Evidence: {html.escape(json.dumps(s.evidence, ensure_ascii=False))}</p><p>Reproduction: {html.escape(_finding_reproduction(run, target_endpoint, s))}</p><p>Recommended remediation: {html.escape(_finding_recommendation(s))}</p><p>Repeat only in an isolated, resettable test environment. Credential-like fields are redacted.</p></article>"
        for s in run.scenarios if s.status == "VERIFIED_FAILURE"
    ) or "<p>No verified failures were observed in the executed scenarios.</p>"
    plan_rows = "".join(
        f"<tr><td>{html.escape(item.get('title', 'Scenario proposal'))}</td><td>{html.escape(item.get('hypothesis', ''))}</td><td>{'Eligible for current runner' if item.get('eligible_for_current_runner') else 'Requires unavailable capability'}</td><td>Not executed</td></tr>"
        for item in run.planning.get("scenarios", [])
    ) or "<tr><td colspan='4'>No scenario plan was requested.</td></tr>"
    investigation = run.planning.get("investigation", {})
    investigation_titles = {
        "observed_facts": "Observed baseline facts",
        "inferred_dependencies": "Inferred dependencies · unverified",
        "failure_sensitive_operations": "Failure-sensitive operations",
        "candidate_invariants": "Candidate invariants · hypotheses",
        "coverage_gaps": "Coverage gaps",
    }
    investigation_sections = "".join(
        f"<h3>{html.escape(title)}</h3><ul>{''.join(f'<li>{html.escape(str(item))}</li>' for item in (investigation.get(key) or ['None reported.']))}</ul>"
        for key, title in investigation_titles.items()
    ) if investigation else "<p>No LLM investigation was performed.</p>"
    semantic_cases = [item for item in run.scenarios if item.scenario_id.startswith("judge-")]
    semantic_rows = "".join(
        f"<tr><td>{html.escape(item.name)}</td><td>{html.escape(item.status)}</td>"
        f"<td>{html.escape(str(item.evidence.get('judge', {}).get('confidence', '—')))}</td>"
        f"<td>{html.escape(str(item.evidence.get('judge', {}).get('explanation', item.limitation or '')))}</td>"
        f"<td>{html.escape(str(item.evidence.get('judge', {}).get('answer_quote', '')))}</td>"
        f"<td>{html.escape(str(item.evidence.get('judge', {}).get('reference_quote', '')))}</td></tr>"
        for item in semantic_cases
    ) or "<tr><td colspan='6'>No built-in semantic judge cases were run.</td></tr>"
    score = run.scoring.get("overall_score")
    score_text = f"{score}/100" if score is not None else "Not established"
    observed_scopes = ", ".join(run.scoring.get("observed_scopes", [])) or "none"
    html_doc = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>BreakMyBot report {html.escape(run.id)}</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 24px;color:#18212b}}h1{{font-size:34px}}.score{{font-size:32px;color:#a43129}}table{{border-collapse:collapse;width:100%;margin-top:20px}}th,td{{border:1px solid #ccd2d8;padding:10px;text-align:left}}th{{background:#edf1f5}}small{{color:#59636e}}</style>
<body><h1>BREAKMYBOT</h1><p>AI RELIABILITY ASSESSMENT</p><h2>{html.escape(run.application_name or 'AI application')}</h2><p>Assessment date: {html.escape(run.created_at.isoformat())}</p><p>{html.escape(target_endpoint)}</p>
<p class="score">{score_text}</p><p>Coverage: {run.scoring.get('coverage_percent', 0)}% · Rubric weight observed: {run.scoring.get('observed_weight_percent', 0)}%</p><p>{html.escape(run.scoring.get('warning', ''))}</p>
<h2>Executive summary</h2><p>Assessment of {html.escape(run.application_name or 'the connected target')} via {html.escape(run.method)} {html.escape(run.endpoint_path)}. Status: {html.escape(run.status)}. Observed scopes: {html.escape(observed_scopes)}.</p>
<h2>Category scorecard</h2><table><thead><tr><th>Category</th><th>Score</th><th>Coverage</th><th>Conclusive</th></tr></thead><tbody>{category_rows}</tbody></table>
<h2>Findings</h2>{finding_rows}
<h2>Scenario planning hypotheses</h2><table><thead><tr><th>Proposal</th><th>Hypothesis</th><th>Capability gate</th><th>Execution</th></tr></thead><tbody>{plan_rows}</tbody></table>
<h2>LLM investigation · hypotheses, not scored facts</h2><p>Provider: {html.escape(str(run.planning.get('provider', 'not recorded')))} · Model: {html.escape(str(run.planning.get('actual_model', run.planning.get('requested_model', 'not recorded'))))}</p>{investigation_sections}
<h2>Reference-grounded semantic judgments · fallible model evidence</h2><p>Status: {html.escape(str(run.semantic_judging.get('status', 'not requested')))} · Provider: {html.escape(str(run.semantic_judging.get('provider', 'not recorded')))} · Model: {html.escape(str(run.semantic_judging.get('actual_model', run.semantic_judging.get('requested_model', 'not recorded'))))} · Usage: {html.escape(str(run.semantic_judging.get('usage', 'not reported')))} · Cost: {html.escape(str(run.semantic_judging.get('provider_cost', 'not reported')))}</p><table><thead><tr><th>Case</th><th>Result</th><th>Confidence</th><th>Rationale</th><th>Answer quote</th><th>Reference quote</th></tr></thead><tbody>{semantic_rows}</tbody></table><p>A single model judgment is fallible; review its grounded quotes. The overall score is calculated by the fixed rubric from persisted scenario outcomes.</p>
<h2>Scenario inventory</h2><table><thead><tr><th>ID</th><th>Scenario</th><th>Category</th><th>Result</th><th>HTTP</th><th>Limitations</th></tr></thead><tbody>{scenario_rows}</tbody></table>
<h2>Method and limitations</h2><p>Methodology: {html.escape(run.scoring.get('methodology', ''))}. The score describes only executed checks and is not a universal safety certification.</p>
<p>Unsupported capabilities: {html.escape(', '.join(run.scoring.get('untested_capabilities', [])))}</p><small>Run {html.escape(run.id)} · {html.escape(run.created_at.isoformat())}</small></body></html>"""
    html_path.write_text(html_doc, encoding="utf-8")

    styles = getSampleStyleSheet()
    story = [Paragraph("BREAKMYBOT — AI RELIABILITY ASSESSMENT", styles["Title"]), Spacer(1, 12),
             Paragraph(html.escape(run.application_name or "AI application"), styles["Heading2"]),
             Paragraph(html.escape(target_endpoint), styles["BodyText"]),
             Paragraph(f"Score: {score_text} · Coverage: {run.scoring.get('coverage_percent', 0)}% · Rubric weight observed: {run.scoring.get('observed_weight_percent', 0)}%", styles["Heading2"]),
             Paragraph("Observed scopes: " + html.escape(observed_scopes), styles["BodyText"]),
             Paragraph(html.escape(run.scoring.get("warning", "")), styles["BodyText"]), Spacer(1, 12)]
    category_data = [["Category", "Score", "Coverage", "Conclusive"]]
    category_data.extend([
        [name, f"{item['score']}/100" if item["score"] is not None else "Not established",
         f"{item['coverage_percent']}%", str(item["conclusive"])]
        for name, item in run.scoring.get("category_scores", {}).items()
    ])
    if len(category_data) > 1:
        category_table = Table(category_data, repeatRows=1, colWidths=[200, 95, 90, 75])
        category_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#263746")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        story.extend([Paragraph("Category scorecard", styles["Heading2"]), category_table, Spacer(1, 16)])
    data = [["Scenario", "Category", "Result", "HTTP"]] + [[s.name[:40], s.category[:28], s.status, str(s.http_status or "—")] for s in run.scenarios]
    table = Table(data, repeatRows=1, colWidths=[190, 140, 100, 55])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#263746")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    story.extend([table, Spacer(1, 16), Paragraph("Unsupported: " + html.escape(", ".join(run.scoring.get("untested_capabilities", []))), styles["BodyText"])])
    story.append(Paragraph("LLM investigation · hypotheses, not scored facts", styles["Heading2"]))
    story.append(Paragraph("Provider: " + html.escape(str(run.planning.get("provider", "not recorded")))
        + " · Model: " + html.escape(str(run.planning.get("actual_model", run.planning.get("requested_model", "not recorded")))), styles["BodyText"]))
    if investigation:
        for key, title in investigation_titles.items():
            story.append(Paragraph(html.escape(title), styles["Heading3"]))
            story.extend(Paragraph("• " + html.escape(str(item)), styles["BodyText"])
                for item in (investigation.get(key) or ["None reported."]))
    else:
        story.append(Paragraph("No LLM investigation was performed.", styles["BodyText"]))
    story.append(Paragraph("Reference-grounded semantic judgments · fallible model evidence", styles["Heading2"]))
    story.append(Paragraph("Status: " + html.escape(str(run.semantic_judging.get("status", "not requested")))
        + " · Provider: " + html.escape(str(run.semantic_judging.get("provider", "not recorded")))
        + " · Model: " + html.escape(str(run.semantic_judging.get("actual_model", run.semantic_judging.get("requested_model", "not recorded"))))
        + " · Usage: " + html.escape(str(run.semantic_judging.get("usage", "not reported")))
        + " · Cost: " + html.escape(str(run.semantic_judging.get("provider_cost", "not reported"))), styles["BodyText"]))
    if semantic_cases:
        judge_data = [["Case", "Result", "Confidence", "Rationale"]]
        judge_data.extend([[item.name[:45], item.status,
            str(item.evidence.get("judge", {}).get("confidence", "—")),
            str(item.evidence.get("judge", {}).get("explanation", item.limitation or ""))[:500]]
            for item in semantic_cases])
        judge_table = Table(judge_data, repeatRows=1, colWidths=[110, 80, 65, 285])
        judge_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#263746")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        story.append(judge_table)
        for item in semantic_cases:
            judged = item.evidence.get("judge", {})
            story.append(Paragraph(html.escape(f"{item.name}: answer quote: {judged.get('answer_quote', '—')} · reference quote: {judged.get('reference_quote', '—')}"), styles["BodyText"]))
    else:
        story.append(Paragraph("No built-in semantic judge cases were run.", styles["BodyText"]))
    story.append(Paragraph("A single model judgment is fallible; review grounded quotes. The aggregate score uses the fixed rubric, not a model-generated score.", styles["BodyText"]))
    failed = [s for s in run.scenarios if s.status == "VERIFIED_FAILURE"]
    if failed:
        story.append(Paragraph("Verified findings", styles["Heading2"]))
        for item in failed:
            story.extend([Paragraph(html.escape(f"{item.name} — {item.severity}"), styles["Heading3"]),
                          Paragraph("Expected behavior: " + html.escape(_finding_expectation(item)), styles["BodyText"]),
                          Paragraph("Actual observation: " + html.escape(str(item.evidence.get("response_excerpt", item.limitation or "No response excerpt recorded.")))[:1500], styles["BodyText"]),
                          Paragraph("Reproduction: " + html.escape(_finding_reproduction(run, target_endpoint, item)), styles["BodyText"]),
                          Paragraph("Recommended remediation: " + html.escape(_finding_recommendation(item)), styles["BodyText"]),
                          Paragraph("Recorded evidence: " + html.escape(json.dumps(item.evidence, ensure_ascii=False)[:1500]), styles["BodyText"])])
    SimpleDocTemplate(str(pdf_path), pagesize=letter, title=f"BreakMyBot {run.id}").build(story)
    return {"json": str(json_path), "html": str(html_path), "pdf": str(pdf_path)}
