export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: Request, context: Context) {
  const { path } = await context.params;
  const origin = (process.env.BREAKMYBOT_API_ORIGIN || "http://127.0.0.1:8000").replace(/\/$/, "");
  const incoming = new URL(request.url);
  const target = `${origin}/api/${path.map(encodeURIComponent).join("/")}${incoming.search}`;
  const headers = new Headers();
  for (const name of ["accept", "content-type", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
    signal: request.signal,
  });
  const responseHeaders = new Headers();
  for (const name of ["content-type", "content-disposition", "cache-control", "last-modified", "etag"]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("x-accel-buffering", "no");
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
