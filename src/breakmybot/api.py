from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from dotenv import load_dotenv

from . import runner, scoring, store
from .models import DiscoveryRequest, ProjectCreate, ProjectRecord, RunAccepted, RunRecord, RunRequest
from .target import discover, preflight
from .redaction import redact_payload

load_dotenv(Path.cwd() / ".env")

@asynccontextmanager
async def lifespan(_app):
    store.expire_stale_jobs()
    yield


app = FastAPI(title="BreakMyBot API", version="0.1.0", description="Evidence-based AI application reliability assessments", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def root():
    return {"product": "BreakMyBot", "dashboard": "http://127.0.0.1:3000", "api_docs": "/docs"}


@app.get("/legacy-dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>BreakMyBot</title>
<style>
:root{color-scheme:dark;--bg:#101419;--panel:#191f26;--line:#303943;--ink:#eef2f5;--muted:#9aa7b3;--accent:#fb684f;--green:#7ad5a0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:32px 22px 60px}header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}.brand{font-size:21px;font-weight:800;letter-spacing:.04em}.brand span{color:var(--accent)}.tag{color:var(--muted)}h1{font-size:38px;line-height:1.12;margin:0 0 8px}h2{font-size:19px;margin:0 0 14px}.lede{color:var(--muted);max-width:680px;margin:0 0 26px}.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}.field{display:grid;gap:6px;margin:12px 0}label{font-size:13px;color:var(--muted)}input,select,textarea{width:100%;background:#0e1216;color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:10px;font:inherit}textarea{min-height:112px;resize:vertical;font-family:ui-monospace,monospace}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}button{background:var(--accent);color:#21120f;border:0;border-radius:7px;padding:10px 14px;font-weight:750;cursor:pointer}button.secondary{background:#2b343d;color:var(--ink);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:15px}.hint,.muted{color:var(--muted);font-size:13px}.pill{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:3px 9px;color:var(--muted);font-size:12px}.score{font-size:32px;font-weight:800;color:var(--green)}.list{display:grid;gap:9px}.item{padding:11px;border:1px solid var(--line);border-radius:8px}.item strong{display:block}.item a{color:#8bc8ff;margin-right:12px}.scenario{display:flex;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding:9px 0}.status{font-weight:700}.PASS{color:var(--green)}.FAIL{color:#ff9284}pre{white-space:pre-wrap;word-break:break-word;background:#0d1115;padding:12px;border-radius:7px;max-height:300px;overflow:auto}.warning{border-left:3px solid var(--accent);padding:8px 12px;color:var(--muted);font-size:13px}footer{margin-top:22px;color:var(--muted);font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr}h1{font-size:31px}}
</style></head><body><main>
<header><div class="brand"><span>💥</span> BREAKMYBOT</div><div class="tag">AI reliability assessments</div></header>
<h1>Find the failure before your users do.</h1><p class="lede">Connect a local AI application, run bounded checks against its real HTTP endpoint, and review evidence from the target response.</p>
<div class="grid"><section>
<div class="card"><h2>Connect an application</h2>
<div class="field"><label for="url">Application base URL</label><input id="url" value="http://localhost:8000" autocomplete="url"></div>
<div class="actions"><button id="discover" class="secondary">Discover API</button><span id="connect-status" class="hint"></span></div>
<div class="row"><div class="field"><label for="method">Method</label><select id="method"><option>POST</option><option>GET</option><option>PUT</option><option>PATCH</option></select></div><div class="field"><label for="endpoint">Invocation path</label><input id="endpoint" value="/api/chat"></div></div>
<div class="field"><label for="payload">Request JSON</label><textarea id="payload">{"message":"Hello. Please reply briefly."}</textarea></div>
<div class="row"><div class="field"><label for="profile">Profile</label><select id="profile"><option value="quick">Quick</option><option value="standard" selected>Standard</option><option value="deep">Deep</option></select></div><div class="field"><label for="expected">Optional expected text</label><input id="expected" placeholder="Add a literal check"></div></div>
<p class="warning">Repeated POST calls may repeat side effects. This dashboard sends one POST invocation per run. Only assess systems you are authorized to test.</p>
<div class="actions"><button id="start">Start assessment</button><span id="run-status" class="hint"></span></div></div>
<div id="result" class="card" hidden><h2>Assessment result</h2><div id="score" class="score">—</div><div id="coverage" class="muted"></div><div id="scenarios"></div><div id="reports" class="actions"></div></div>
</section><aside><div class="card"><h2>Recent assessments</h2><div id="recent" class="list"><span class="muted">Loading saved runs…</span></div></div><div class="card"><h2>Live run events</h2><pre id="events">Waiting for an assessment.</pre></div><div class="card"><h2>Assessment scope</h2><p class="muted">The current score covers observed HTTP and latency checks only. Memory lifecycle, internal tool faults, and security boundaries require a compatible agent adapter and are reported as unsupported.</p><p class="muted">A high score with limited coverage is not a general safety guarantee.</p></div></aside></div>
<footer>Runs and reports are stored locally. This local dashboard is unauthenticated and must remain bound to loopback.</footer></main>
<script>
const el=id=>document.getElementById(id);let source=null;
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const body=await r.json().catch(()=>({detail:r.statusText}));if(!r.ok)throw Error(body.detail||`HTTP ${r.status}`);return body}
function addText(parent,text,tag='span',cls=''){const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;parent.append(node);return node}
async function refresh(){try{const runs=await api('/api/runs?limit=12');const box=el('recent');box.replaceChildren();if(!runs.length){addText(box,'No assessments yet.','span','muted');return}for(const run of runs){const item=document.createElement('div');item.className='item';const score=run.scoring?.overall_score;addText(item,run.application_name||run.target_url,'strong');addText(item,`${run.status} · ${score==null?'Score not established':score+'/100'} · ${run.scoring?.coverage_percent||0}% coverage`,'span','muted');const link=document.createElement('a');link.href=`/api/runs/${encodeURIComponent(run.id)}/report.html`;link.textContent='HTML report';item.append(document.createElement('br'),link);box.append(item)}}catch(e){el('recent').textContent=e.message}}
el('discover').onclick=async()=>{el('connect-status').textContent='Checking from the BreakMyBot service…';try{const d=await api('/api/targets/discover',{method:'POST',body:JSON.stringify({url:el('url').value})});el('connect-status').textContent=d.reachable?`Reachable · ${d.openapi_found?'OpenAPI found':'OpenAPI not found'}`:'Not reachable from runner';if(d.endpoints?.length){const c=d.endpoints[0];el('method').value=c.method;el('endpoint').value=c.path;el('payload').value=JSON.stringify(c.payload_template,null,2)}for(const x of d.diagnostics||[])addText(el('connect-status'),` · ${x}`)}}catch(e){el('connect-status').textContent=e.message}};
el('start').onclick=async()=>{let payload;try{payload=JSON.parse(el('payload').value)}catch{el('run-status').textContent='Request JSON is invalid';return}if(el('method').value!=='GET'&&!confirm('Confirm this test invocation is safe and will not trigger unwanted side effects. Continue?'))return;el('start').disabled=true;el('run-status').textContent='Creating assessment…';el('result').hidden=true;el('events').textContent='';try{const created=await api('/api/runs',{method:'POST',body:JSON.stringify({target_url:el('url').value,endpoint_path:el('endpoint').value,method:el('method').value,payload,profile:el('profile').value,expected_substring:el('expected').value||null,allow_repeated_post:false})});if(source)source.close();source=new EventSource(`/api/runs/${encodeURIComponent(created.run_id)}/events`);source.onmessage=e=>{el('events').textContent+=e.data+'\n'};source.addEventListener('scenario.completed',e=>{el('events').textContent+=e.data+'\n'});el('run-status').textContent=`Running ${created.run_id}`;let run;do{await new Promise(r=>setTimeout(r,650));run=await api(`/api/runs/${encodeURIComponent(created.run_id)}`)}while(['CREATED','RUNNING'].includes(run.status));if(source)source.close();el('result').hidden=false;el('score').textContent=run.scoring.overall_score==null?'Not established':`${run.scoring.overall_score}/100`;el('coverage').textContent=`Coverage ${run.scoring.coverage_percent}% · ${run.scoring.warning}`;const list=el('scenarios');list.replaceChildren();for(const s of run.scenarios){const row=document.createElement('div');row.className='scenario';addText(row,s.name);addText(row,s.status,s.status==='PASSED'?'span':'span',s.status==='PASSED'?'status PASS':s.status==='VERIFIED_FAILURE'?'status FAIL':'status');list.append(row)}const reports=el('reports');reports.replaceChildren();for(const format of ['html','pdf','json']){const a=document.createElement('a');a.href=`/api/runs/${encodeURIComponent(run.id)}/report.${format}`;a.textContent=`${format.toUpperCase()} report`;a.className='pill';reports.append(a)}el('run-status').textContent=run.status;refresh()}catch(e){el('run-status').textContent=e.message}finally{el('start').disabled=false}};
refresh();
</script></body></html>""")


@app.get("/api/health")
def health():
    return {"status": "ok", "product": "BreakMyBot", "storage": "sqlite", "execution": "sqlite-worker-queue"}


@app.post("/api/targets/discover")
async def target_discover(request: DiscoveryRequest):
    try:
        return await discover(str(request.url), docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/targets/validate")
async def target_validate(request: DiscoveryRequest):
    return await target_discover(request)


@app.post("/api/targets/preflight")
async def target_preflight(request: RunRequest):
    try:
        headers = None
        if request.auth_env:
            token = os.getenv(request.auth_env)
            if not token:
                raise ValueError("Configured target credential environment variable is missing")
            headers = {request.auth_header: f"{request.auth_scheme} {token}".strip()}
        result = await preflight(str(request.target_url), request.endpoint_path, request.method,
            request.payload, docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true",
            headers=headers)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects", response_model=ProjectRecord, status_code=201)
def create_project(request: ProjectCreate):
    name = request.name.strip()
    if not name:
        raise HTTPException(422, "Project name cannot be blank")
    project = ProjectRecord(name=name, target_url=str(request.target_url).rstrip("/"))
    store.save_project(project)
    return project


@app.get("/api/projects", response_model=list[ProjectRecord])
def list_projects():
    return store.list_projects()


@app.post("/api/runs", response_model=RunAccepted, status_code=202)
async def create_run(request: RunRequest):
    if request.project_id:
        project = store.get_project(request.project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        project_origin = urlsplit(project.target_url)
        target_origin = urlsplit(str(request.target_url))
        if (project_origin.scheme, project_origin.netloc) != (target_origin.scheme, target_origin.netloc):
            raise HTTPException(422, "Assessment target origin must match the selected project's target")
    record = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, request_payload=redact_payload(request.payload),
        application_name=request.application_name, profile=request.profile, project_id=request.project_id)
    store.save(record)
    store.append_event(record.id, {"type": "run.created", "phase": "CREATED"})
    store.enqueue(record.id, request.model_dump(mode="json"))
    return RunAccepted(run_id=record.id, status="CREATED", status_url=f"/api/runs/{record.id}")


@app.get("/api/runs")
def list_runs(limit: int = Query(default=30, ge=1, le=100), project_id: str | None = None):
    if project_id and not store.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return [_current_run(run) for run in store.latest(limit, project_id)]


def _current_run(run: RunRecord) -> dict:
    data = run.model_dump(mode="json")
    data["scoring"] = scoring.calculate(run)
    return data


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return _current_run(run)


@app.get("/api/runs/{run_id}/scenarios")
def list_run_scenarios(run_id: str, offset: int = Query(default=0, ge=0),
                       limit: int = Query(default=100, ge=1, le=500)):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {"run_id": run_id, "total": len(run.scenarios),
        "items": [scenario.model_dump(mode="json") for scenario in run.scenarios[offset:offset + limit]]}


@app.get("/api/runs/{run_id}/findings")
def list_run_findings(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    findings = [scenario.model_dump(mode="json") for scenario in run.scenarios
        if scenario.status == "VERIFIED_FAILURE"]
    return {"run_id": run_id, "total": len(findings), "items": findings}


@app.get("/api/runs/{run_id}/score")
def get_run_score(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {"run_id": run_id, **scoring.calculate(run)}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in {"CREATED", "RUNNING"}:
        raise HTTPException(409, "Run is already terminal")
    return {"run_id": run_id, "cancellation_requested": store.cancel_job(run_id)}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0):
    if not store.get(run_id):
        raise HTTPException(404, "Run not found")
    async def stream():
        cursor = after
        while True:
            for sequence, event in store.events_after(run_id, cursor):
                cursor = sequence
                yield f"id: {sequence}\nevent: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
            record = store.get(run_id)
            if record and record.status in {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL"} and not store.events_after(run_id, cursor):
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.75)
    return StreamingResponse(stream(), media_type="text/event-stream")


def _report(run_id: str, format: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    run.scoring = scoring.calculate(run)
    if run.status in {"COMPLETED", "PARTIAL", "CANCELLED"}:
        run.report_paths = runner.reports.generate(run, store.data_dir() / "reports" / run.id)
        store.save(run)
    if format not in {"html", "pdf", "json"}:
        raise HTTPException(404, "Unsupported report format")
    path = run.report_paths.get(format)
    if not path or not Path(path).exists():
        raise HTTPException(409, "Report is not ready")
    media = {"html": "text/html", "pdf": "application/pdf", "json": "application/json"}[format]
    return FileResponse(path, media_type=media, filename=f"report.{format}")


@app.get("/api/runs/{run_id}/report")
def report_json(run_id: str):
    return _report(run_id, "json")


@app.get("/api/runs/{run_id}/report.{format}")
def download_report(run_id: str, format: str):
    return _report(run_id, format)


@app.get("/api/scoring/methodology")
def scoring_methodology():
    return {"version": scoring.METHODOLOGY_VERSION, "category_weights": scoring.CATEGORY_WEIGHTS,
        "score_scope": "evidence-backed executed scenarios, weighted within observed categories",
        "coverage_is_separate": True, "unsupported_tests_are_not_passes": True,
        "minimum_conclusive_scenarios": 3}
