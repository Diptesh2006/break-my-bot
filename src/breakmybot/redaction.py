from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_PARTS = ("password", "secret", "api_key", "apikey", "authorization", "credential")
TOKEN_USAGE_KEYS = {"total_tokens", "prompt_tokens", "completion_tokens", "input_tokens",
    "output_tokens", "reasoning_tokens", "cached_tokens", "accepted_tokens", "rejected_tokens"}


def _sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered in TOKEN_USAGE_KEYS:
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS) or "token" in lowered


def redact_payload(value: Any, key: str = "") -> Any:
    """Return JSON-safe request evidence while hiding fields named like credentials."""
    if _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): redact_payload(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_text(value: str, payload: Any, extra_secrets: tuple[str, ...] = ()) -> str:
    """Redact credential-shaped response fragments and known submitted secrets."""
    result = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    secrets: set[str] = {secret for secret in extra_secrets if len(secret) >= 4}

    def collect(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child_value in item.items():
                collect(child_value, str(child_key))
        elif isinstance(item, list):
            for child in item:
                collect(child, key)
        elif _sensitive_key(key) and isinstance(item, str) and len(item) >= 4:
            secrets.add(item)

    collect(payload)
    for secret in sorted(secrets, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def redact_evidence(value: Any, extra_secrets: tuple[str, ...] = (), key: str = "") -> Any:
    """Sanitize untrusted nested evidence before persisting or publishing it."""
    if _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): redact_evidence(child_value, extra_secrets, str(child_key))
                for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_evidence(item, extra_secrets, key) for item in value]
    if isinstance(value, str):
        return redact_text(value, {}, extra_secrets)
    return value
