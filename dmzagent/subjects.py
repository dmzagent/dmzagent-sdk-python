"""Canonical subject identity helpers.

Subject ids follow the form:

    subject:<division_id>:<slug>               (3-part)
    subject:<division_id>:<type>:<slug>         (4-part, with subject type)

The optional type segment encodes the behavioral pattern (chat, sensor,
lead, ticket, journey, etc.) and determines trace grouping strategy
and deviation rules on the server side.
"""
from __future__ import annotations

import re

from .errors import ValidationError

SUBJECT_PREFIX = "subject"
DEFAULT_MAX_SLUG_LENGTH = 48
VALID_SUBJECT_TYPES = frozenset({
    "chat", "sensor", "lead", "ticket", "journey", "custom", "other",
})

_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_subject(value: str, max_length: int | None = None) -> str:
    """Convert a display name into a URL-safe slug."""
    if max_length is None:
        max_length = DEFAULT_MAX_SLUG_LENGTH
    if not isinstance(max_length, int) or max_length <= 0:
        raise ValidationError("max_length must be a positive integer")
    slug = _NON_SLUG_RE.sub("-", value.strip().lower()).strip("-")[:max_length]
    return slug or "subject"


def is_canonical_subject_id(subject_id: str | None) -> bool:
    """Return True for a valid canonical subject id (3 or 4 parts)."""
    if not subject_id:
        return False
    parts = subject_id.split(":")
    n = len(parts)
    if n < 3 or parts[0] != SUBJECT_PREFIX:
        return False
    if n == 3:
        return bool(parts[1]) and bool(parts[2])
    if n == 4:
        return bool(parts[1]) and bool(parts[2]) and bool(parts[3])
    return False


def subject_type_from_subject_id(subject_id: str | None) -> str | None:
    """Extract the type segment from a 4-part subject id.

    Returns None for 3-part or unrecognised forms.
    """
    if not subject_id:
        return None
    parts = subject_id.split(":")
    if len(parts) >= 4 and parts[0] == SUBJECT_PREFIX and parts[2]:
        return parts[2]
    return None


def subject_id_for_division(
    division_id: str,
    display_name_or_slug: str,
    *,
    max_length: int | None = None,
    subject_type: str | None = None,
) -> str:
    """Build a canonical subject id.

    ``subject_type`` is optional — omitted → 3-part form, included →
    4-part form ``subject:<div>:<type>:<slug>``.
    """
    division = division_id.strip()
    if not division:
        raise ValidationError("division_id is required")
    if ":" in division:
        raise ValidationError("division_id must not contain ':'")
    if not isinstance(display_name_or_slug, str) or not display_name_or_slug.strip():
        raise ValidationError("display_name_or_slug is required")
    slug = slugify_subject(display_name_or_slug, max_length=max_length)
    if subject_type:
        return f"{SUBJECT_PREFIX}:{division}:{subject_type}:{slug}"
    return f"{SUBJECT_PREFIX}:{division}:{slug}"


def subject_for_division(
    division_id: str,
    display_name_or_slug: str,
    *,
    max_length: int | None = None,
    subject_type: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Like ``subject_id_for_division`` but returns a full Subject dict."""
    return {
        "subject_id": subject_id_for_division(
            division_id, display_name_or_slug,
            max_length=max_length, subject_type=subject_type,
        ),
        "role": role or "other",
        "kind": kind or "other",
        **(metadata if metadata else {}),
    }
