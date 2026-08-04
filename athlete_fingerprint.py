"""
Fingerprint (hash) comparison — Step 3 of the pipeline.

Avoids paying for a Gemini call when the raw source content hasn't actually
changed since the last successful check.
"""

import hashlib
import json


def compute_fingerprint(raw_data: dict | str) -> str:
    """Deterministic SHA-256 hash of the raw fetched payload."""
    if isinstance(raw_data, dict):
        payload = json.dumps(raw_data, sort_keys=True, default=str)
    else:
        payload = raw_data
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_changed(new_fingerprint: str, stored_fingerprint: str | None) -> bool:
    """New athletes (stored_fingerprint is None) always count as changed."""
    if stored_fingerprint is None:
        return True
    return new_fingerprint != stored_fingerprint