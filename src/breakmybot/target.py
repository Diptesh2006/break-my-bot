from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .models import DiscoveryResult, EndpointCandidate

HINTS = ("chat", "invoke", "agent", "ask", "generate", "completion", "query")


def _allowed_hostnames() -> set[str]:
    configured = os.getenv("BREAKMYBOT_ALLOWED_HOSTS", "")
    hosts = {item.strip().lower() for item in configured.split(",") if item.strip()}
    hosts.add("host.docker.internal")
    return hosts


def validate_target(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Target URL cannot contain credentials, query parameters, or a fragment; use auth environment variables")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "ip6-localhost"}:
        return
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback:
            return
        cidrs = [item.strip() for item in os.getenv("BREAKMYBOT_ALLOWED_CIDRS", "").split(",") if item.strip()]
        if any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs):
            return
        raise ValueError("Only loopback targets are allowed unless a test-network CIDR is explicitly configured")
    except ValueError as exc:
        if "Only loopback targets are allowed" in str(exc):
            raise
    try:
        answers = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
    except OSError as exc:
        raise ValueError(f"Target hostname could not be resolved: {hostname}") from exc
    if hostname in _allowed_hostnames():
        cidrs = [item.strip() for item in os.getenv("BREAKMYBOT_ALLOWED_CIDRS", "").split(",") if item.strip()]
        networks = [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]
        if answers and all(ip.is_loopback or ip.is_private or ip.is_link_local or any(ip in net for net in networks) for ip in answers):
            return
    if not answers or any(not ip.is_loopback for ip in answers):
        raise ValueError("Target must resolve only to loopback addresses or be explicitly allowlisted")


def docker_resolve_url(url: str) -> str:
    """Translate a host CLI's loopback URL for a container-side target request."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        netloc = "host.docker.internal"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    return url


def _resolve_ref(spec: dict, value: dict) -> dict:
    ref = value.get("$ref", "")
    if not ref.startswith("#/components/schemas/"):
        return value
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else value


def payload_from_operation(spec: dict, operation: dict) -> dict:
    content = operation.get("requestBody", {}).get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    schema = _resolve_ref(spec, media.get("schema", {}))
    props = schema.get("properties", {})
    if not props:
        return {"message": "Hello. Please reply briefly."}
    payload: dict = {}
    for name in schema.get("required", list(props)):
        prop = _resolve_ref(spec, props.get(name, {}))
        kind = prop.get("type")
        if name in {"messages", "chat_history"} and kind == "array":
            item = _resolve_ref(spec, prop.get("items", {}))
            if item.get("type") == "object":
                payload[name] = [{"role": "user", "content": "Hello. Please reply briefly."}]
            else:
                payload[name] = ["Hello. Please reply briefly."]
        elif kind == "string" or not kind:
            payload[name] = "Hello. Please reply briefly."
        elif kind == "array":
            payload[name] = []
        elif kind == "object":
            payload[name] = {}
        elif kind == "boolean":
            payload[name] = False
        elif kind in {"integer", "number"}:
            payload[name] = 1
    return payload


def identify_endpoints(spec: dict) -> list[EndpointCandidate]:
    candidates: list[EndpointCandidate] = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method = method.upper()
            if method not in {"POST", "PUT", "PATCH", "GET"} or not isinstance(operation, dict):
                continue
            text = f"{path} {operation.get('summary', '')} {operation.get('description', '')}".lower()
            score = sum(3 for hint in HINTS if hint in text)
            if "requestbody" in operation:
                score += 2
            if score:
                candidates.append(EndpointCandidate(method=method, path=path,
                    description=operation.get("summary", operation.get("description", "")),
                    score=score, payload_template=payload_from_operation(spec, operation)))
    return sorted(candidates, key=lambda item: (-item.score, item.path))


async def discover(url: str, *, docker_mode: bool = False) -> DiscoveryResult:
    validate_target(url)
    display_url = url
    request_url = docker_resolve_url(url) if docker_mode else url
    diagnostics: list[str] = []
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        try:
            response = await client.get(request_url)
            reachable = response.status_code < 500
            diagnostics.append(f"Root endpoint returned HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            return DiscoveryResult(target_url=display_url, reachable=False,
                diagnostics=[f"Target is unreachable from the BreakMyBot service: {type(exc).__name__}"])
        parsed = urlsplit(request_url)
        found_spec = False
        app_name = None
        endpoints: list[EndpointCandidate] = []
        spec_paths = ["/openapi.json", "/swagger.json"]
        if parsed.path.rstrip("/") not in {"", "/"}:
            spec_paths.insert(0, parsed.path.rstrip("/") + "/openapi.json")
        for path in spec_paths:
            spec_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
            try:
                spec_response = await client.get(spec_url)
                if spec_response.status_code == 200 and "json" in spec_response.headers.get("content-type", ""):
                    spec = spec_response.json()
                    if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
                        found_spec = True
                        app_name = spec.get("info", {}).get("title")
                        endpoints = identify_endpoints(spec)
                        diagnostics.append(f"OpenAPI specification found at {path}")
                        break
            except (httpx.HTTPError, ValueError):
                continue
        if not found_spec:
            diagnostics.append("No OpenAPI description was found; provide an endpoint and JSON payload explicitly")
        return DiscoveryResult(target_url=display_url, reachable=reachable, openapi_found=found_spec,
            application_name=app_name, endpoints=endpoints, diagnostics=diagnostics)


async def invoke(url: str, path: str, method: str, payload: dict, timeout: float, *, docker_mode: bool = False,
                 headers: dict[str, str] | None = None) -> tuple[int, str, float, str]:
    base = docker_resolve_url(url) if docker_mode else url
    target = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    validate_target(target)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        import time
        started = time.perf_counter()
        async with client.stream(method, target, json=payload if method != "GET" else None, headers=headers) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                remaining = max(0, 1_000_001 - len(chunks))
                chunks.extend(chunk[:remaining])
                if len(chunks) > 1_000_000:
                    break
            body = bytes(chunks).decode("utf-8", errors="replace")
            if len(chunks) > 1_000_000:
                body = body[:4000] + "\n[response body truncated after 1 MB]"
        duration = (time.perf_counter() - started) * 1000
        return response.status_code, body[:4100], duration, response.headers.get("content-type", "")
