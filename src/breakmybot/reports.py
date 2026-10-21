from __future__ import annotations

import html
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import RunRecord


def generate(run: RunRecord, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    html_path = output_dir / "report.html"
    pdf_path = output_dir / "report.pdf"
    payload = run.model_dump(mode="json")
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
        f"<article><h3>{html.escape(s.name)} · {html.escape(s.severity)}</h3><p>Category: {html.escape(s.category)}. Scenario: {html.escape(s.scenario_id)}.</p><p>Observed status: {html.escape(str(s.http_status or 'No HTTP response'))}. Duration: {html.escape(str(s.duration_ms or 'N/A'))} ms.</p><p>Evidence: {html.escape(json.dumps(s.evidence, ensure_ascii=False))}</p><p>Reproduction request: {html.escape(run.method)} {html.escape(run.target_url + run.endpoint_path)} with payload <code>{html.escape(json.dumps(run.request_payload, ensure_ascii=False))}</code>. Credential-like fields are redacted in stored evidence. Repeat only in an isolated, resettable test environment.</p></article>"
        for s in run.scenarios if s.status == "VERIFIED_FAILURE"
    ) or "<p>No verified failures were observed in the executed scenarios.</p>"
    plan_rows = "".join(
        f"<tr><td>{html.escape(item.get('title', 'Scenario proposal'))}</td><td>{html.escape(item.get('hypothesis', ''))}</td><td>{'Eligible for current runner' if item.get('eligible_for_current_runner') else 'Requires unavailable capability'}</td><td>Not executed</td></tr>"
        for item in run.planning.get("scenarios", [])
    ) or "<tr><td colspan='4'>No scenario plan was requested.</td></tr>"
    score = run.scoring.get("overall_score")
    score_text = f"{score}/100" if score is not None else "Not established"
    observed_scopes = ", ".join(run.scoring.get("observed_scopes", [])) or "none"
    html_doc = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>BreakMyBot report {html.escape(run.id)}</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 24px;color:#18212b}}h1{{font-size:34px}}.score{{font-size:32px;color:#a43129}}table{{border-collapse:collapse;width:100%;margin-top:20px}}th,td{{border:1px solid #ccd2d8;padding:10px;text-align:left}}th{{background:#edf1f5}}small{{color:#59636e}}</style>
<body><h1>BREAKMYBOT</h1><p>AI RELIABILITY ASSESSMENT</p><h2>{html.escape(run.application_name or 'AI application')}</h2><p>Assessment date: {html.escape(run.created_at.isoformat())}</p><p>{html.escape(run.target_url)}{html.escape(run.endpoint_path)}</p>
<p class="score">{score_text}</p><p>Coverage: {run.scoring.get('coverage_percent', 0)}%</p><p>{html.escape(run.scoring.get('warning', ''))}</p>
<h2>Executive summary</h2><p>Assessment of {html.escape(run.application_name or 'the connected target')} via {html.escape(run.method)} {html.escape(run.endpoint_path)}. Status: {html.escape(run.status)}. Observed scopes: {html.escape(observed_scopes)}.</p>
<h2>Category scorecard</h2><table><thead><tr><th>Category</th><th>Score</th><th>Coverage</th><th>Conclusive</th></tr></thead><tbody>{category_rows}</tbody></table>
<h2>Findings</h2>{finding_rows}
<h2>Scenario planning hypotheses</h2><table><thead><tr><th>Proposal</th><th>Hypothesis</th><th>Capability gate</th><th>Execution</th></tr></thead><tbody>{plan_rows}</tbody></table>
<h2>Scenario inventory</h2><table><thead><tr><th>ID</th><th>Scenario</th><th>Category</th><th>Result</th><th>HTTP</th><th>Limitations</th></tr></thead><tbody>{scenario_rows}</tbody></table>
<h2>Method and limitations</h2><p>Methodology: {html.escape(run.scoring.get('methodology', ''))}. The score describes only executed checks and is not a universal safety certification.</p>
<p>Unsupported capabilities: {html.escape(', '.join(run.scoring.get('untested_capabilities', [])))}</p><small>Run {html.escape(run.id)} · {html.escape(run.created_at.isoformat())}</small></body></html>"""
    html_path.write_text(html_doc, encoding="utf-8")

    styles = getSampleStyleSheet()
    story = [Paragraph("BREAKMYBOT — AI RELIABILITY ASSESSMENT", styles["Title"]), Spacer(1, 12),
             Paragraph(html.escape(run.application_name or "AI application"), styles["Heading2"]),
             Paragraph(html.escape(run.target_url + run.endpoint_path), styles["BodyText"]),
             Paragraph(f"Score: {score_text} · Coverage: {run.scoring.get('coverage_percent', 0)}%", styles["Heading2"]),
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
    failed = [s for s in run.scenarios if s.status == "VERIFIED_FAILURE"]
    if failed:
        story.append(Paragraph("Verified findings", styles["Heading2"]))
        for item in failed:
            story.extend([Paragraph(html.escape(f"{item.name} — {item.severity}"), styles["Heading3"]),
                          Paragraph(html.escape(json.dumps(item.evidence, ensure_ascii=False)[:1500]), styles["BodyText"])])
    SimpleDocTemplate(str(pdf_path), pagesize=letter, title=f"BreakMyBot {run.id}").build(story)
    return {"json": str(json_path), "html": str(html_path), "pdf": str(pdf_path)}
