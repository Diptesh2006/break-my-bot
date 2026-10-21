from __future__ import annotations

from typing import Any

SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential")


def redact_payload(value: Any, key: str = "") -> Any:
    """Return JSON-safe request evidence while hiding fields named like credentials."""
    if any(part in key.casefold() for part in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): redact_payload(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
