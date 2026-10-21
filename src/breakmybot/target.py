from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .models import DiscoveryResult, EndpointCandidate
from .redaction import redact_text

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
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return url


def _resolve_ref(spec: dict, value: dict, seen: frozenset[str] = frozenset()) -> dict:
    ref = value.get("$ref", "")
    if not ref.startswith("#/") or ref in seen:
        return value
    node = spec
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        node = node.get(part, {}) if isinstance(node, dict) else {}
    if not isinstance(node, dict):
        return value
    return {**node, **{key: child for key, child in value.items() if key != "$ref"}}


_PROMPT = "Hello. Please reply briefly."
_CONVERSATION_FIELDS = ("messages", "chat_history", "message", "input", "prompt", "query")


def _sample_value(spec: dict, schema: dict, *, name: str = "", depth: int = 0,
                  seen: frozenset[str] = frozenset()):
    """Build a bounded example honoring common OpenAPI constraints and references."""
    if depth > 8 or not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    resolved = _resolve_ref(spec, schema, seen)
    if ref and resolved is schema:
        return None
    next_seen = seen | ({ref} if ref else set())
    schema = resolved

    for union in ("oneOf", "anyOf"):
        options = schema.get(union)
        if isinstance(options, list) and options:
            return _sample_value(spec, options[0], name=name, depth=depth + 1,
                                 seen=frozenset(next_seen))
    if schema.get("allOf"):
        merged_properties: dict = dict(schema.get("properties", {}))
        merged_required: list[str] = list(schema.get("required", []))
        for part in schema["allOf"]:
            resolved_part = _resolve_ref(spec, part, frozenset(next_seen))
            merged_properties.update(resolved_part.get("properties", {}))
            merged_required.extend(resolved_part.get("required", []))
        schema = {**schema, "properties": merged_properties,
                  "required": list(dict.fromkeys(merged_required))}

    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if "example" in schema:
        return schema["example"]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    lowered = name.casefold()
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return {}
        required = schema.get("required", [])
        names = list(dict.fromkeys([item for item in required if item in props]))
        conversation_field = next((field for field in _CONVERSATION_FIELDS if field in props), None)
        if conversation_field and not any(field in names for field in _CONVERSATION_FIELDS):
            names.append(conversation_field)
        value = {}
        for field in names:
            field_schema = props[field]
            if field in {"messages", "chat_history"} and isinstance(field_schema, dict) \
                    and field_schema.get("type") == "array":
                item_schema = _resolve_ref(spec, field_schema.get("items", {}), frozenset(next_seen))
                if item_schema.get("type") == "object":
                    item_props = item_schema.get("properties", {})
                    if item_props:
                        item = {}
                        for item_field in item_schema.get("required", list(item_props)):
                            field_schema = item_props[item_field]
                            if item_field in {"content", "message", "text"}:
                                candidate = field_schema.get("enum", [_PROMPT])
                                item[item_field] = _PROMPT if _PROMPT in candidate else candidate[0]
                            elif item_field == "role" and "user" in field_schema.get("enum", ["user"]):
                                item[item_field] = "user"
                            else:
                                item[item_field] = _sample_value(spec, field_schema, name=item_field,
                                    depth=depth + 2, seen=frozenset(next_seen))
                        if not any(key in item for key in ("content", "message", "text")):
                            item["content"] = _PROMPT
                        value[field] = [item]
                    else:
                        value[field] = [{"role": "user", "content": _PROMPT}]
                    continue
            sampled = _sample_value(spec, field_schema, name=field, depth=depth + 1,
                                    seen=frozenset(next_seen))
            if sampled is not None:
                value[field] = sampled
        return value
    if kind == "array":
        items = schema.get("items", {})
        minimum = max(1 if lowered in {"messages", "chat_history"} else 0,
                      int(schema.get("minItems", 0) or 0))
        maximum = schema.get("maxItems")
        count = min(minimum, int(maximum)) if maximum is not None else min(minimum, 3)
        count = min(count, 3)
        return [_sample_value(spec, items, name=name, depth=depth + 1,
                              seen=frozenset(next_seen)) for _ in range(count)]
    if kind == "string" or kind is None:
        if lowered in _CONVERSATION_FIELDS:
            value = _PROMPT
            minimum = int(schema.get("minLength", 0) or 0)
            if minimum > 256:
                return None
            value = value if minimum <= len(value) else value + " " * (minimum - len(value))
            maximum = schema.get("maxLength")
            if maximum is not None:
                if int(maximum) < minimum:
                    return None
                value = value[:int(maximum)]
            return value
        fmt = schema.get("format")
        if fmt == "email":
            return "tester@example.test"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        minimum = int(schema.get("minLength", 0) or 0)
        maximum = int(schema.get("maxLength", 256) or 0)
        if maximum < minimum or minimum > 256:
            return None
        return "x" * min(max(minimum, 1), maximum, 256)
    if kind in {"integer", "number"}:
        numeric = int if kind == "integer" else float
        minimum = schema.get("minimum", 1)
        if "exclusiveMinimum" in schema:
            exclusive = numeric(schema["exclusiveMinimum"])
            minimum = max(numeric(minimum), exclusive + (1 if kind == "integer" else 0.1))
        maximum = schema.get("maximum")
        value = max(numeric(minimum), numeric(1))
        if maximum is not None:
            value = min(value, numeric(maximum))
        if "exclusiveMaximum" in schema:
            exclusive = numeric(schema["exclusiveMaximum"])
            value = min(value, exclusive - (1 if kind == "integer" else 0.1))
        return value
    if kind == "boolean":
        return True
    return None


def payload_from_operation(spec: dict, operation: dict) -> dict:
    request_body = operation.get("requestBody", {})
    content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
    if not isinstance(content, dict):
        content = {}
    media = content.get("application/json") or next(iter(content.values()), {})
    if not isinstance(media, dict):
        media = {}
    schema = media.get("schema", {})
    explicit = media.get("example")
    if explicit is None and isinstance(media.get("examples"), dict) and media["examples"]:
        first = next(iter(media["examples"].values()))
        explicit = first.get("value") if isinstance(first, dict) else None
    if explicit is not None:
        return explicit if isinstance(explicit, dict) else {"value": explicit}
    sample = _sample_value(spec, schema)
    return sample if isinstance(sample, dict) else {"message": _PROMPT}


def identify_endpoints(spec: dict) -> list[EndpointCandidate]:
    candidates: list[EndpointCandidate] = []
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return candidates
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(method, str):
                continue
            method = method.upper()
            if method not in {"POST", "PUT", "PATCH", "GET"} or not isinstance(operation, dict):
                continue
            text = f"{path} {operation.get('summary', '')} {operation.get('description', '')}".lower()
            score = sum(3 for hint in HINTS if hint in text)
            if "requestbody" in operation:
                score += 2
            if score:
                request_body = operation.get("requestBody", {})
                schema = request_body.get("content", {}) if isinstance(request_body, dict) else {}
                media = schema.get("application/json", {}) if isinstance(schema, dict) else {}
                json_schema = media.get("schema", {}) if isinstance(media, dict) else {}
                props = _resolve_ref(spec, json_schema).get("properties", {}) if isinstance(json_schema, dict) else {}
                if any(name in props for name in ("messages", "message", "input", "prompt", "query")):
                    score += 3
                if method == "GET":
                    score -= 1
                response_schemas: dict[str, dict[str, dict]] = {}
                streaming = False
                responses = operation.get("responses", {})
                for status, response in (responses.items() if isinstance(responses, dict) else []):
                    if not isinstance(response, dict):
                        continue
                    response_content_map = response.get("content", {})
                    for content_type, response_content in (response_content_map.items()
                            if isinstance(response_content_map, dict) else []):
                        if not isinstance(response_content, dict):
                            continue
                        if content_type.casefold().split(";")[0].strip() == "text/event-stream":
                            streaming = True
                        response_schema = response_content.get("schema")
                        if isinstance(response_schema, dict):
                            response_schemas.setdefault(str(status), {})[content_type] = response_schema
                candidates.append(EndpointCandidate(method=method, path=path,
                    description=operation.get("summary", operation.get("description", "")),
                    score=score, payload_template=payload_from_operation(spec, operation),
                    request_schema=(json_schema if isinstance(json_schema, dict) else {}),
                    response_schemas=response_schemas, streaming=streaming))
    return sorted(candidates, key=lambda item: (-item.score, item.path))


def identify_health_endpoints(spec: dict) -> list[str]:
    health: list[str] = []
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return health
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() != "get" or not isinstance(operation, dict):
                continue
            tags = operation.get("tags", [])
            labels = " ".join([str(path), str(operation.get("summary", "")),
                str(operation.get("description", "")), str(operation.get("operationId", "")),
                " ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""]).casefold()
            if any(term in labels for term in ("health", "readiness", "liveness", "livez", "readyz")):
                health.append(f"GET {path}")
    return sorted(set(health))


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
        health_endpoints: list[str] = []
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
                        health_endpoints = identify_health_endpoints(spec)
                        diagnostics.append(f"OpenAPI specification found at {path}")
                        if health_endpoints:
                            diagnostics.append(f"Health endpoints found: {', '.join(health_endpoints)}")
                        break
            except (httpx.HTTPError, ValueError):
                continue
        if not found_spec:
            diagnostics.append("No OpenAPI description was found; provide an endpoint and JSON payload explicitly")
        return DiscoveryResult(target_url=display_url, reachable=reachable, openapi_found=found_spec,
            application_name=app_name, endpoints=endpoints, health_endpoints=health_endpoints,
            diagnostics=diagnostics)


async def preflight(url: str, path: str, method: str, payload: dict, *, docker_mode: bool = False,
                    headers: dict[str, str] | None = None) -> dict:
    """Invoke exactly one user-authorized baseline from the runner's network namespace."""
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("Endpoint path must be an absolute path without query or fragment")
    if method not in {"GET", "POST", "PUT", "PATCH"}:
        raise ValueError("Unsupported endpoint method")
    status, body, duration, content_type = await invoke(url, path, method, payload, 15,
        docker_mode=docker_mode, headers=headers)
    return {"reachable": True, "status_code": status, "successful": 200 <= status < 300,
        "duration_ms": round(duration, 2), "content_type": content_type,
        "response_excerpt": redact_text(body, payload,
            tuple(value for value in (headers or {}).values() if value))[:500]}


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
