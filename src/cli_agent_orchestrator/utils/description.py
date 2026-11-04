"""Shared one-line normalization for operator-facing terminal descriptions."""

import re

DESCRIPTION_MAX_LENGTH = 64


def normalize_description(value: str | None, *, truncate: bool = False) -> str | None:
    """Replace line breaks, remove C0/C1 controls, then trim and count code points."""
    if value is None:
        return None
    # Preserve word boundaries before filtering controls (including interior tabs).
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", " ".join(value.splitlines())).strip()
    if truncate:
        text = text[:DESCRIPTION_MAX_LENGTH].rstrip()
    elif len(text) > DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"Description must be at most {DESCRIPTION_MAX_LENGTH} characters")
    return text or None
