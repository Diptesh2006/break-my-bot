"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Scenario = { scenario_id: string; name: string; category: string; status: string; severity: string; evidence: Record<string, unknown>; limitation?: string | null };
type Run = { id: string; project_id?: string | null; status: string; target_url: string; endpoint_path: string; application_name?: string | null; created_at: string; scoring: { overall_score: number | null; coverage_percent: number; observed_weight_percent: number; critical_findings: number; scenario_counts: Record<string, number>; category_scores: Record<string, { score: number | null; coverage_percent: number; conclusive: number }> }; scenarios: Scenario[]; report_paths?: Record<string, string> };
type Project = { id: string; name: string; target_url: string; created_at: string };
type Discovery = { reachable: boolean; openapi_found: boolean; application_name?: string; endpoints: Array<{ method: string; path: string; payload_template: Record<string, unknown> }>; diagnostics: string[] };
type LiveEvent = { type: string; scenario_id?: string; name?: string; category?: string; status?: string; phase?: string; completed_scenarios?: number; evidence?: Record<string, unknown> };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: "no-store", ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `API request failed (${response.status})`);
  return body as T;
}

const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED", "PARTIAL"]);

export default function Home() {
  const [url, setUrl] = useState("http://localhost:8000");
  const [method, setMethod] = useState("POST");
  const [endpoint, setEndpoint] = useState("/api/chat");
  const [payload, setPayload] = useState('{\n  "message": "Hello. Please reply briefly."\n}');
  const [profile, setProfile] = useState("standard");
  const [expected, setExpected] = useState("");
  const [authEnv, setAuthEnv] = useState("");
  const [authHeader, setAuthHeader] = useState("Authorization");
  const [authScheme, setAuthScheme] = useState("Bearer");
  const [projectName, setProjectName] = useState("");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [phase, setPhase] = useState("");
  const [activeScenario, setActiveScenario] = useState("");
  const [liveScenarios, setLiveScenarios] = useState<LiveEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState("Connecting to API…");

  const refresh = useCallback(async () => {
    try {
      const query = selectedProjectId ? `&project_id=${encodeURIComponent(selectedProjectId)}` : "";
      const [healthResult, recent, savedProjects] = await Promise.all([api<{ status: string }>("/api/health"), api<Run[]>(`/api/runs?limit=12${query}`), api<Project[]>("/api/projects")]);
      setHealth(healthResult.status === "ok" ? "API online" : "API unavailable");
      setRuns(recent);
      setProjects(savedProjects);
    } catch (e) {
      setHealth("API unavailable");
      setError(e instanceof Error ? e.message : "Could not connect to BreakMyBot API");
    }
  }, [selectedProjectId]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function discover() {
    setError(""); setDiscovery(null);
    try {
      const result = await api<Discovery>("/api/targets/discover", { method: "POST", body: JSON.stringify({ url }) });
      setDiscovery(result);
      if (result.endpoints.length) {
        setMethod(result.endpoints[0].method);
        setEndpoint(result.endpoints[0].path);
        setPayload(JSON.stringify(result.endpoints[0].payload_template, null, 2));
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Discovery failed"); }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault(); setError("");
    try {
      const created = await api<Project>("/api/projects", { method: "POST", body: JSON.stringify({ name: projectName, target_url: url }) });
      setProjects((current) => [created, ...current]);
      setProjectName(""); setCompareIds([]); setSelectedProjectId(created.id); setUrl(created.target_url);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save project"); }
  }

  function toggleCompare(id: string) {
    setCompareIds((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      if (current.length >= 4) { setError("Compare up to four assessments at a time."); return current; }
      return [...current, id];
    });
  }

  async function start(event: FormEvent) {
    event.preventDefault(); setError("");
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(payload); } catch { setError("Request JSON is invalid."); return; }
    if (method !== "GET" && !window.confirm("Confirm this test invocation is safe and will not cause unwanted side effects.")) return;
    setBusy(true); setRun(null); setEvents([]); setLiveScenarios([]); setPhase("QUEUED"); setActiveScenario("Waiting for worker");
    try {
      const created = await api<{ run_id: string }>("/api/runs", { method: "POST", body: JSON.stringify({ target_url: url, project_id: selectedProjectId || null, endpoint_path: endpoint, method, payload: parsed, profile, expected_substring: expected || null, allow_repeated_post: false, auth_env: authEnv || null, auth_header: authHeader, auth_scheme: authScheme }) });
      setRun(await api<Run>(`/api/runs/${encodeURIComponent(created.run_id)}`));
      const stream = new EventSource(`/api/runs/${encodeURIComponent(created.run_id)}/events`);
      const receive = (type: string) => (message: Event) => {
        try {
          const data = JSON.parse((message as MessageEvent).data) as LiveEvent;
          const item = { ...data, type };
          setEvents((current) => [...current.slice(-79), item]);
          if (type === "run.phase") { setPhase(data.phase || "RUNNING"); setActiveScenario(""); }
          if (type === "scenario.started") { setActiveScenario(data.name || data.scenario_id || "Scenario"); }
          if (type === "scenario.completed") {
            setLiveScenarios((current) => [...current.filter((scenario) => scenario.scenario_id !== data.scenario_id), item]);
            setActiveScenario("");
          }
          if (type === "run.completed" || type === "run.cancelled" || type === "run.failed") { setPhase(data.phase || data.status || "FINISHED"); setActiveScenario(""); }
        } catch { /* Ignore malformed or non-JSON event frames. */ }
      };
      ["run.started", "run.phase", "planning.completed", "scenario.started", "scenario.completed", "run.progress", "run.completed", "run.cancelled", "run.failed"].forEach((type) => stream.addEventListener(type, receive(type)));
      let result: Run;
      do {
        await new Promise((resolve) => window.setTimeout(resolve, 650));
        result = await api<Run>(`/api/runs/${encodeURIComponent(created.run_id)}`);
        setRun(result);
      } while (!terminal.has(result.status));
      stream.close(); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Assessment failed"); }
    finally { setBusy(false); }
  }

  async function cancel() {
    if (!run || terminal.has(run.status)) return;
    try { await api(`/api/runs/${encodeURIComponent(run.id)}/cancel`, { method: "POST" }); }
    catch (e) { setError(e instanceof Error ? e.message : "Cancellation failed"); }
  }

  async function openRun(id: string) {
    try { setRun(await api<Run>(`/api/runs/${encodeURIComponent(id)}`)); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not open assessment"); }
  }

  const counts = run?.scoring.scenario_counts;
  const compareRuns = runs.filter((item) => compareIds.includes(item.id));
  const compareKey = (item: Run) => item.project_id || new URL(item.target_url).origin;
  const comparisonCompatible = compareRuns.length < 2 || compareRuns.every((item) => compareKey(item) === compareKey(compareRuns[0]));
  return (
    <main className="shell">
      <header className="topbar"><a className="brand" href="#top"><span className="brand-mark">B</span><span>BREAKMYBOT<small>AI RELIABILITY LAB</small></span></a><div className="top-meta"><span className={health === "API online" ? "health-dot online" : "health-dot"} />{health}<span className="local-tag">LOCAL WORKSPACE</span></div></header>
      <section className="hero" id="top"><div className="eyebrow"><span className="eyebrow-line" />EVIDENCE BEFORE ASSUMPTIONS</div><h1>Find the failure<br /><em>before your users do.</em></h1><p>Run controlled reliability checks against your AI application. Every result is tied to a real response, explicit invariant, and observed fault provenance.</p></section>
      {error && <div className="notice error" role="alert">{error}<button className="close" onClick={() => setError("")} aria-label="Dismiss error">×</button></div>}
      <div className="workspace-grid">
        <section className="panel connect-panel"><div className="panel-head"><div><div className="eyebrow">01 / TARGET</div><h2>Connect an application</h2></div><span className="panel-index">PREFLIGHT</span></div>
          <form onSubmit={start}>
            <label htmlFor="target-url">Application base URL</label><div className="input-action"><input id="target-url" type="url" value={url} onChange={(e) => setUrl(e.target.value)} required /><button type="button" className="button subtle" onClick={discover}>Discover API</button></div>
            {discovery && <div className={discovery.reachable ? "preflight success" : "preflight failure"}><span>{discovery.reachable ? "✓ Reachable from execution service" : "× Not reachable from execution service"}</span><span>{discovery.openapi_found ? "OpenAPI detected" : "OpenAPI unavailable"}</span>{discovery.diagnostics.map((item) => <small key={item}>{item}</small>)}</div>}
            <div className="two-col"><div><label htmlFor="method">Method</label><select id="method" value={method} onChange={(e) => setMethod(e.target.value)}>{["POST", "GET", "PUT", "PATCH"].map((item) => <option key={item}>{item}</option>)}</select></div><div><label htmlFor="endpoint">Invocation path</label><input id="endpoint" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} required /></div></div>
            <label htmlFor="payload">Request JSON</label><textarea id="payload" value={payload} onChange={(e) => setPayload(e.target.value)} spellCheck={false} rows={7} />
            <details className="auth-config"><summary>Authentication <span className="optional">OPTIONAL · SERVER ENVIRONMENT ONLY</span></summary><p>Store the token in BreakMyBot’s <code>.env</code>; enter its variable name here. The secret is never sent through the browser.</p><label htmlFor="auth-env">Credential environment variable</label><input id="auth-env" value={authEnv} onChange={(e) => setAuthEnv(e.target.value)} placeholder="TARGET_API_TOKEN" autoComplete="off" /><div className="two-col"><div><label htmlFor="auth-header">Header name</label><input id="auth-header" value={authHeader} onChange={(e) => setAuthHeader(e.target.value)} /></div><div><label htmlFor="auth-scheme">Scheme</label><input id="auth-scheme" value={authScheme} onChange={(e) => setAuthScheme(e.target.value)} placeholder="Bearer" /></div></div></details>
            <div className="two-col"><div><label htmlFor="profile">Assessment profile</label><select id="profile" value={profile} onChange={(e) => setProfile(e.target.value)}><option value="quick">Quick scan</option><option value="standard">Standard</option><option value="deep">Deep</option></select></div><div><label htmlFor="expected">Expected response text <span className="optional">OPTIONAL</span></label><input id="expected" value={expected} onChange={(e) => setExpected(e.target.value)} placeholder="e.g. contact support" /></div></div>
            <div className="safety-note"><span>!</span><p>Only assess systems you are authorized to test. POST calls are sent once by default; repeated calls require separate CLI authorization.</p></div>
            <button className="button primary start-button" type="submit" disabled={busy}>{busy ? <><span className="spinner" /> Assessment running…</> : "Start assessment  →"}</button>
            {busy && run && <button className="button danger" type="button" onClick={cancel}>Cancel run</button>}
          </form>
        </section>
        <aside className="side-column"><section className="panel scope-panel"><div className="panel-head"><div><div className="eyebrow">CURRENT SCOPE</div><h2>What gets measured</h2></div></div><ul className="scope-list"><li><span className="scope-check">✓</span><div><b>Black-box HTTP</b><small>Availability, response validity, latency</small></div><span className="scope-state">ACTIVE</span></li><li><span className="scope-check">◇</span><div><b>Tool fault injection</b><small>Requires opt-in target instrumentation</small></div><span className="scope-state muted-state">OPT-IN</span></li><li><span className="scope-check">◇</span><div><b>Memory evaluation</b><small>MemoryScope bridge and judge consent</small></div><span className="scope-state muted-state">OPT-IN</span></li></ul><p className="scope-foot">Unsupported capabilities remain visible; they are never counted as passes.</p></section>
          <section className="panel project-panel"><div className="panel-head"><div><div className="eyebrow">WORKSPACE</div><h2>Projects</h2></div></div><label htmlFor="project-select">History and assessment project</label><select id="project-select" value={selectedProjectId} onChange={(e) => { setSelectedProjectId(e.target.value); setCompareIds([]); const chosen = projects.find((project) => project.id === e.target.value); if (chosen) setUrl(chosen.target_url); }}><option value="">All targets · unassigned</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select><form className="project-create" onSubmit={createProject}><input aria-label="New project name" value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="New project name" required maxLength={120} /><small>Uses the current target URL: {url}</small><button className="button subtle" type="submit">Save project</button></form><p className="scope-foot">Projects and assessments are stored locally in this BreakMyBot workspace.</p></section>
          <section className="panel recent-panel"><div className="panel-head"><div><div className="eyebrow">HISTORY</div><h2>Recent assessments</h2></div><button className="icon-button" onClick={refresh} aria-label="Refresh assessments">↻</button></div>{runs.length ? <div className="recent-list">{runs.map((item) => <div className="recent-entry" key={item.id}><button className="recent-item" onClick={() => openRun(item.id)}><span className="recent-status" data-status={item.status} /><span className="recent-copy"><b>{item.application_name || item.target_url}</b><small>{item.id} · {item.endpoint_path} · {new Date(item.created_at).toLocaleString()}</small></span><span className="recent-score">{item.scoring?.overall_score == null ? "—" : `${item.scoring.overall_score}`}</span></button><label className="compare-choice"><input type="checkbox" checked={compareIds.includes(item.id)} onChange={() => toggleCompare(item.id)} />Compare</label></div>)}</div> : <p className="empty">No saved assessments yet.</p>}{compareRuns.length > 0 && <div className="comparison-wrap"><h3 className="subhead">Run comparison <small>Persisted results only</small></h3>{!comparisonCompatible ? <p className="empty">Choose runs from the same project or target to compare.</p> : compareRuns.length < 2 ? <p className="empty">Select one more run from this project or target.</p> : <div className="comparison-scroll"><table className="comparison-table"><thead><tr><th>Metric</th>{compareRuns.map((item) => <th key={item.id}><button type="button" onClick={() => openRun(item.id)}>{item.id}</button></th>)}</tr></thead><tbody><tr><th>Status</th>{compareRuns.map((item) => <td key={item.id}>{item.status}</td>)}</tr><tr><th>Score</th>{compareRuns.map((item) => <td key={item.id}>{item.scoring.overall_score == null ? "Not established" : `${item.scoring.overall_score}/100`}</td>)}</tr><tr><th>Coverage</th>{compareRuns.map((item) => <td key={item.id}>{item.scoring.coverage_percent}%</td>)}</tr><tr><th>Rubric observed</th>{compareRuns.map((item) => <td key={item.id}>{item.scoring.observed_weight_percent}%</td>)}</tr><tr><th>Critical findings</th>{compareRuns.map((item) => <td key={item.id}>{item.scoring.critical_findings}</td>)}</tr><tr><th>Target path</th>{compareRuns.map((item) => <td key={item.id}>{item.endpoint_path}</td>)}</tr></tbody></table></div>}</div>}</section>
        </aside>
      </div>
      {run && <section className="panel result-panel"><div className="panel-head"><div><div className="eyebrow">02 / EVIDENCE · {run.id}</div><h2>Assessment {run.status.toLowerCase()}</h2></div><div className="report-links">{["html", "pdf", "json"].map((format) => <a key={format} href={`/api/runs/${encodeURIComponent(run.id)}/report.${format}`} target="_blank" rel="noreferrer">{format.toUpperCase()} ↗</a>)}</div></div>
        <div className="score-strip"><div className="score-main"><small>OBSERVED RELIABILITY</small><strong>{run.scoring.overall_score == null ? "Not established" : `${run.scoring.overall_score}/100`}</strong></div><div className="score-metric"><small>SCENARIO COVERAGE</small><strong>{run.scoring.coverage_percent}%</strong></div><div className="score-metric"><small>RUBRIC WEIGHT OBSERVED</small><strong>{run.scoring.observed_weight_percent}%</strong></div><div className="score-metric"><small>CRITICAL FINDINGS</small><strong>{run.scoring.critical_findings}</strong></div></div>
        <div className="count-strip"><span><i className="count-pass" />{counts?.PASSED ?? 0} passed</span><span><i className="count-fail" />{counts?.VERIFIED_FAILURE ?? 0} verified failures</span><span><i className="count-other" />{counts?.UNSUPPORTED ?? 0} unsupported</span></div>
        <div className="category-grid">{Object.entries(run.scoring.category_scores).map(([name, item]) => <div className="category-card" key={name}><div><b>{name}</b><span>{item.coverage_percent}% covered</span></div><strong>{item.score == null ? "—" : `${item.score}`}</strong></div>)}</div>
        <h3 className="subhead">Scenario evidence</h3><div className="scenario-list">{run.scenarios.map((scenario) => <details className="scenario-row" key={scenario.scenario_id}><summary><span className="scenario-state" data-status={scenario.status}>{scenario.status.replaceAll("_", " ")}</span><b>{scenario.name}</b><span className="scenario-category">{scenario.category}</span></summary><div className="evidence"><p>{scenario.limitation || "Observed from target response and execution evidence."}</p><pre>{JSON.stringify(scenario.evidence, null, 2)}</pre></div></details>)}</div>
        <section className="live-progress" aria-live="polite"><div className="live-progress-head"><h3 className="subhead">Live execution</h3><span>{phase || run.status}</span></div>{activeScenario && <p className="active-scenario"><span className="spinner" />{activeScenario}</p>}<div className="live-scenario-list">{liveScenarios.map((item) => <div className="live-scenario" key={item.scenario_id}><span className="scenario-state" data-status={item.status}>{item.status?.replaceAll("_", " ")}</span><b>{item.name}</b>{item.evidence?.fault_injected === true && <small>Fault confirmed{item.evidence.cleanup_succeeded === true ? " · cleanup confirmed" : " · cleanup not confirmed"}</small>}</div>)}</div></section>
        <details className="live-events"><summary>Execution events <span>{events.length || "open for persisted history"}</span></summary><pre>{events.length ? events.map((item) => JSON.stringify(item)).join("\n") : "Run events are persisted and available via the API event stream."}</pre></details>
      </section>}
      <footer><span>BREAKMYBOT · SCORE METHODOLOGY v3</span><span>Local-first · evidence-backed · no fabricated observations</span></footer>
    </main>
  );
}
