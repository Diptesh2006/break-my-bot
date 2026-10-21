"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Scenario = { scenario_id: string; name: string; category: string; status: string; severity: string; evidence: Record<string, unknown>; limitation?: string | null };
type Run = { id: string; status: string; target_url: string; endpoint_path: string; created_at: string; scoring: { overall_score: number | null; coverage_percent: number; observed_weight_percent: number; critical_findings: number; scenario_counts: Record<string, number>; category_scores: Record<string, { score: number | null; coverage_percent: number; conclusive: number }> }; scenarios: Scenario[]; report_paths?: Record<string, string> };
type Discovery = { reachable: boolean; openapi_found: boolean; application_name?: string; endpoints: Array<{ method: string; path: string; payload_template: Record<string, unknown> }>; diagnostics: string[] };

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
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState("Connecting to API…");

  const refresh = useCallback(async () => {
    try {
      const [healthResult, recent] = await Promise.all([api<{ status: string }>("/api/health"), api<Run[]>("/api/runs?limit=12")]);
      setHealth(healthResult.status === "ok" ? "API online" : "API unavailable");
      setRuns(recent);
    } catch (e) {
      setHealth("API unavailable");
      setError(e instanceof Error ? e.message : "Could not connect to BreakMyBot API");
    }
  }, []);

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

  async function start(event: FormEvent) {
    event.preventDefault(); setError("");
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(payload); } catch { setError("Request JSON is invalid."); return; }
    if (method !== "GET" && !window.confirm("Confirm this test invocation is safe and will not cause unwanted side effects.")) return;
    setBusy(true); setRun(null); setEvents([]);
    try {
      const created = await api<{ run_id: string }>("/api/runs", { method: "POST", body: JSON.stringify({ target_url: url, endpoint_path: endpoint, method, payload: parsed, profile, expected_substring: expected || null, allow_repeated_post: false }) });
      const stream = new EventSource(`/api/runs/${encodeURIComponent(created.run_id)}/events`);
      stream.onmessage = (message) => setEvents((current) => [...current.slice(-80), message.data]);
      stream.addEventListener("scenario.completed", (message) => setEvents((current) => [...current.slice(-80), (message as MessageEvent).data]));
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
            <div className="two-col"><div><label htmlFor="profile">Assessment profile</label><select id="profile" value={profile} onChange={(e) => setProfile(e.target.value)}><option value="quick">Quick scan</option><option value="standard">Standard</option><option value="deep">Deep</option></select></div><div><label htmlFor="expected">Expected response text <span className="optional">OPTIONAL</span></label><input id="expected" value={expected} onChange={(e) => setExpected(e.target.value)} placeholder="e.g. contact support" /></div></div>
            <div className="safety-note"><span>!</span><p>Only assess systems you are authorized to test. POST calls are sent once by default; repeated calls require separate CLI authorization.</p></div>
            <button className="button primary start-button" type="submit" disabled={busy}>{busy ? <><span className="spinner" /> Assessment running…</> : "Start assessment  →"}</button>
            {busy && run && <button className="button danger" type="button" onClick={cancel}>Cancel run</button>}
          </form>
        </section>
        <aside className="side-column"><section className="panel scope-panel"><div className="panel-head"><div><div className="eyebrow">CURRENT SCOPE</div><h2>What gets measured</h2></div></div><ul className="scope-list"><li><span className="scope-check">✓</span><div><b>Black-box HTTP</b><small>Availability, response validity, latency</small></div><span className="scope-state">ACTIVE</span></li><li><span className="scope-check">◇</span><div><b>Tool fault injection</b><small>Requires opt-in target instrumentation</small></div><span className="scope-state muted-state">OPT-IN</span></li><li><span className="scope-check">◇</span><div><b>Memory evaluation</b><small>MemoryScope bridge and judge consent</small></div><span className="scope-state muted-state">OPT-IN</span></li></ul><p className="scope-foot">Unsupported capabilities remain visible; they are never counted as passes.</p></section>
          <section className="panel recent-panel"><div className="panel-head"><div><div className="eyebrow">HISTORY</div><h2>Recent assessments</h2></div><button className="icon-button" onClick={refresh} aria-label="Refresh assessments">↻</button></div>{runs.length ? <div className="recent-list">{runs.map((item) => <button className="recent-item" key={item.id} onClick={() => openRun(item.id)}><span className="recent-status" data-status={item.status} /><span className="recent-copy"><b>{item.id}</b><small>{item.endpoint_path} · {new Date(item.created_at).toLocaleString()}</small></span><span className="recent-score">{item.scoring?.overall_score == null ? "—" : `${item.scoring.overall_score}`}</span></button>)}</div> : <p className="empty">No saved assessments yet.</p>}</section>
        </aside>
      </div>
      {run && <section className="panel result-panel"><div className="panel-head"><div><div className="eyebrow">02 / EVIDENCE · {run.id}</div><h2>Assessment {run.status.toLowerCase()}</h2></div><div className="report-links">{["html", "pdf", "json"].map((format) => <a key={format} href={`/api/runs/${encodeURIComponent(run.id)}/report.${format}`} target="_blank" rel="noreferrer">{format.toUpperCase()} ↗</a>)}</div></div>
        <div className="score-strip"><div className="score-main"><small>OBSERVED RELIABILITY</small><strong>{run.scoring.overall_score == null ? "Not established" : `${run.scoring.overall_score}/100`}</strong></div><div className="score-metric"><small>SCENARIO COVERAGE</small><strong>{run.scoring.coverage_percent}%</strong></div><div className="score-metric"><small>RUBRIC WEIGHT OBSERVED</small><strong>{run.scoring.observed_weight_percent}%</strong></div><div className="score-metric"><small>CRITICAL FINDINGS</small><strong>{run.scoring.critical_findings}</strong></div></div>
        <div className="count-strip"><span><i className="count-pass" />{counts?.PASSED ?? 0} passed</span><span><i className="count-fail" />{counts?.VERIFIED_FAILURE ?? 0} verified failures</span><span><i className="count-other" />{counts?.UNSUPPORTED ?? 0} unsupported</span></div>
        <div className="category-grid">{Object.entries(run.scoring.category_scores).map(([name, item]) => <div className="category-card" key={name}><div><b>{name}</b><span>{item.coverage_percent}% covered</span></div><strong>{item.score == null ? "—" : `${item.score}`}</strong></div>)}</div>
        <h3 className="subhead">Scenario evidence</h3><div className="scenario-list">{run.scenarios.map((scenario) => <details className="scenario-row" key={scenario.scenario_id}><summary><span className="scenario-state" data-status={scenario.status}>{scenario.status.replaceAll("_", " ")}</span><b>{scenario.name}</b><span className="scenario-category">{scenario.category}</span></summary><div className="evidence"><p>{scenario.limitation || "Observed from target response and execution evidence."}</p><pre>{JSON.stringify(scenario.evidence, null, 2)}</pre></div></details>)}</div>
        <details className="live-events"><summary>Execution events <span>{events.length || "open for persisted history"}</span></summary><pre>{events.length ? events.join("\n") : "Run events are persisted and available via the API event stream."}</pre></details>
      </section>}
      <footer><span>BREAKMYBOT · SCORE METHODOLOGY v3</span><span>Local-first · evidence-backed · no fabricated observations</span></footer>
    </main>
  );
}
