"""Credential pattern gate for memory writes and archive export.

Pure module — no I/O, no logging, no state. ``scan_for_secrets`` matches
the supplied content against a fixed, ordered list of named regexes and
returns the NAME of the first matching pattern (or ``None`` if clean).
``redact_secrets`` replaces every match of every pattern with a
``[REDACTED:<name>]`` marker for the export ``--redact`` path (#345, D5).

``scan_for_secrets`` is used ONLY to reject credentials on
``scope="federated"`` writes — the machine-wide shared tier. This is a
heuristic deny-list, not entropy scoring; it errs toward catching common
credential shapes.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Pattern, Tuple

# Ordered (name, compiled_regex) pairs. First match wins, so ordering is
# stable and reproducible across calls. No entropy scoring. Vendor-specific
# shapes come before the generic assignment patterns so the returned name is
# the most specific one that applies.
_SECRET_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    # AWS access key IDs — long-lived (AKIA) and temporary/STS (ASIA).
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # AWS secret access keys: 40 base64 chars with an ``aws ... secret|access``
    # context nearby (the gitleaks shape) or a ``SecretAccessKey`` key as in
    # STS/IAM JSON responses. A bare 40-char match would also hit every 40-hex
    # git SHA, so context is required, and the run must mix upper and lower
    # case so a 40-digit id or a lowercase hex digest near the word "aws" does
    # not fire.
    (
        "aws_secret_access_key",
        re.compile(
            r"(?i)(?:aws.{0,20}(?:secret|access).{0,20}['\"=:\s]"
            r"|secret_?access_?key['\"]?\s*[:=]\s*['\"]?)"
            # (?-i:...) turns case-folding back off: the mixed-case lookaheads
            # are meaningless under the leading (?i).
            r"(?-i:(?=[A-Za-z0-9/+]{0,39}[a-z])(?=[A-Za-z0-9/+]{0,39}[A-Z])"
            r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+]))"
        ),
    ),
    # PEM-encoded private keys (RSA / EC / OPENSSH / generic).
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    ),
    # Anthropic API keys.
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI keys: project / service-account / admin prefixes, and the legacy
    # form whose middle carries the fixed ``T3BlbkFJ`` marker.
    (
        "openai_api_key",
        re.compile(
            r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
            r"|sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
        ),
    ),
    # GitHub fine-grained personal access tokens (fixed 22 + 59 segment shape).
    ("github_fine_grained_pat", re.compile(r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}")),
    # GitHub classic PATs (ghp_), OAuth (gho_), user-to-server (ghu_),
    # server-to-server (ghs_) and refresh (ghr_) tokens.
    ("github_pat", re.compile(r"gh[posur]_[A-Za-z0-9]{36,}")),
    # GitLab personal access tokens.
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    # Slack bot / user / refresh / session tokens (``xox?-``) and app-level
    # tokens (``xapp-``, which represent the app across installations). Every
    # issued form has a numeric segment right after the prefix (a bot token
    # is ``xoxb-`` then digits then ``-``; an app-level token is ``xapp-``
    # then a digit then ``-A0`` and the app id), which keeps documentation
    # paths such as ``/xoxb-example-token`` out.
    ("slack_token", re.compile(r"(?:xox[abeprs]|xapp)-[0-9]+-[A-Za-z0-9-]{10,}")),
    # JSON Web Tokens: header and payload are base64url JSON objects, so both
    # begin with ``eyJ``; the three-part dotted shape keeps prose out.
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    # Bearer / api-key / token assignments with a long value. The separator
    # may be ':'/'=' OR whitespace, so the canonical HTTP header form
    # 'Authorization: Bearer <token>' (Bearer followed by a space) is caught.
    (
        "bearer_token",
        re.compile(r"(?i)(?:bearer|api[_-]?key|token)[\s:=]+\S{16,}"),
    ),
    # Generic secret/password assignments.
    (
        "secret_assignment",
        re.compile(r"(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}"),
    ),
]


# Characters that render as nothing. A credential with one of them inside its
# prefix (``AK\u200bIA...``) reads as a key to a human and to the consumer that
# eventually uses it, while defeating every pattern above. The set is the
# whole Unicode ``Cf`` (format) category rather than a hand-picked few: the
# zero-width space/joiners, word joiner and BOM, but also the bidi marks
# (U+200E/F, U+202A-E, U+2066-9), the soft hyphen (U+00AD), the invisible
# operators (U+2061-4) and the tag characters, any one of which splits a prefix
# just as well. Added on top: the variation selectors (U+FE00-F,
# U+E0100-E01EF) and the combining grapheme joiner (U+034F), which are marks
# by category but invisible in practice. Other combining marks (accents) are
# NOT included: they are visible and belong to the text they modify.
# ``scan_for_secrets`` judges the content with these removed. ``redact_secrets``
# removes them only from inside spans that match once they are gone, so a
# U+200D joining an emoji sequence elsewhere in the same string survives.
# The Cf category as of Unicode 16.0.0, frozen. ``unicodedata`` reports the
# category of the interpreter's own tables, and those lag: Python 3.10 ships
# Unicode 13 and 3.11 ships 14, so on them U+0890/U+0891 (Arabic pound and
# piastre marks, 14.0) and U+13439-U+1343F (Egyptian hieroglyph format controls,
# 15.0) are "unassigned" and would pass through, while any newer interpreter and
# every terminal already treats them as format characters. Which code point an
# attacker can hide behind must not depend on which Python the server runs, so
# this table is the floor and the live category is unioned on top of it.
_FORMAT_CONTROL_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)


def _invisible_character_class() -> str:
    # Start from the frozen table above, then add whatever the running
    # interpreter's tables call Cf, so a newer Unicode release is picked up
    # on a newer Python without waiting for this table to be updated. Every Cf
    # code point lives in planes 0 and 1 (BMP and SMP: bidi and zero-width
    # controls, Kaithi and Egyptian hieroglyph format controls, musical and
    # shorthand format controls) or in the plane-14 tag block, so planes 0-1 are
    # enumerated at import (~13 ms) and plane 14 is covered by the table, since
    # iterating the whole code space costs ~120 ms. A test compares the class
    # against the live category over the full range so a Unicode update that
    # adds a format character elsewhere fails loudly.
    code_points = [cp for lo, hi in _FORMAT_CONTROL_RANGES for cp in range(lo, hi + 1)]
    code_points += [cp for cp in range(0x20000) if unicodedata.category(chr(cp)) == "Cf"]
    # Invisible by rendering rather than by category: the combining grapheme
    # joiner and the variation selectors (Mn).
    code_points += [0x034F]
    code_points += list(range(0xFE00, 0xFE10))
    code_points += list(range(0xE0100, 0xE01F0))
    return "".join(f"\\U{cp:08x}" for cp in sorted(set(code_points)))


_INVISIBLE = re.compile(f"[{_invisible_character_class()}]")


def _strip_invisible(content: str) -> str:
    return _INVISIBLE.sub("", content)


def _strip_invisible_inside_matches(content: str) -> str:
    """Drop invisible characters that sit inside a credential, and only those.

    Patterns are run over a copy with the invisible characters removed; every
    match span is mapped back to the original string and the invisible
    characters within that span are removed. Invisible characters outside every
    span are kept.
    """
    kept: List[str] = []
    index_map: List[int] = []
    for i, ch in enumerate(content):
        if not _INVISIBLE.match(ch):
            kept.append(ch)
            index_map.append(i)
    stripped = "".join(kept)
    drop: set[int] = set()
    for _name, pattern in _SECRET_PATTERNS:
        for m in pattern.finditer(stripped):
            if m.end() <= m.start():
                continue
            lo, hi = index_map[m.start()], index_map[m.end() - 1]
            drop.update(j for j in range(lo, hi + 1) if _INVISIBLE.match(content[j]))
    if not drop:
        return content
    return "".join(ch for j, ch in enumerate(content) if j not in drop)


# A parsed document carries context in its STRUCTURE that the text patterns
# cannot see once a key and its value are scanned separately. An STS or IAM
# response puts a bare 40-character secret under ``SecretAccessKey``; a
# ``{"password": "correct horse battery"}`` pair is what the text pattern
# ``password: <value>`` would catch in prose; an environment entry puts the
# key name under ``name`` and the secret under a sibling ``value``. All three
# are recognised here by key. The AWS shape is matched explicitly, keeping the
# text pattern's deliberate refusal to match a context-free 40-character run
# (every 40-hex git SHA would fire); every other key is judged by rendering
# the pair as ``key: value`` and asking whether the text patterns fire on the
# pair when they fire on neither half alone.
_AWS_SECRET_KEY_NAMES = re.compile(r"(?i)^(?:aws_?)?secret_?access_?key$")
_AWS_SECRET_VALUE = re.compile(
    r"(?=[A-Za-z0-9/+]{0,39}[a-z])(?=[A-Za-z0-9/+]{0,39}[A-Z])[A-Za-z0-9/+]{40}\Z"
)
_ENTRY_NAME_KEYS = frozenset({"name", "Name", "key", "Key"})
_ENTRY_VALUE_KEYS = frozenset({"value", "Value"})


def _keyed_secret_name(key: Any, value: Any) -> Optional[str]:
    """Pattern name when ``value`` is a credential that only its ``key`` gives away.

    ``None`` when the value is clean on its own terms (nothing to add), when
    the value already matches by itself (the leaf scan handles it), or when
    the key is not a credential name. Fail-closed: a ``{"pwd": "/some/path"}``
    pair reads as a password assignment here just as ``pwd: /some/path`` does
    in prose, and is redacted.
    """
    if not (isinstance(key, str) and isinstance(value, str)):
        return None
    if _AWS_SECRET_KEY_NAMES.match(_strip_invisible(key)) and _AWS_SECRET_VALUE.match(
        _strip_invisible(value)
    ):
        return "aws_secret_access_key"
    if scan_for_secrets(value) is not None or scan_for_secrets(key) is not None:
        return None
    return scan_for_secrets(f"{key}: {value}")


def _entry_secret_name(node: Dict[Any, Any]) -> Optional[str]:
    """Pattern name when ``node`` is a ``{name: <secret key name>, value: <secret>}`` entry."""
    names = [node[k] for k in _ENTRY_NAME_KEYS if k in node]
    values = [node[k] for k in _ENTRY_VALUE_KEYS if k in node]
    for name in names:
        for value in values:
            hit = _keyed_secret_name(name, value)
            if hit:
                return hit
    return None


def scan_for_secrets(content: str) -> Optional[str]:
    """Return the NAME of the first credential pattern that matches.

    Returns ``None`` when no pattern matches. The caller must not echo the
    matched bytes — only the returned pattern name is safe to log.

    The content is judged with every invisible character removed, so a
    credential split by one is still a credential. The cost is a rare false
    positive: two innocent runs that a bidi mark or soft hyphen kept apart can
    read as one key once joined (``AKIA<RLM>ABCDEF1234567890``). Every caller
    of this gate refuses a write or an export on a hit (federated memory, the
    vault, archive import/export, graph export) and names the pattern, never
    the bytes, so the trade is taken in the fail-closed direction.
    """
    if not content:
        return None
    content = _strip_invisible(content)
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return name
    return None


def scan_json_for_secrets(node: Any) -> Optional[str]:
    """:func:`scan_for_secrets` over a parsed JSON document, structure included.

    Every string (keys and values) is scanned as text, and the key context a
    serialised scan loses is applied on the way down: a 40-character value
    under a ``SecretAccessKey`` key, or the ``value`` of a
    ``{name: AWS_SECRET_ACCESS_KEY, value: ...}`` entry, is a hit. Callers that
    have a parsed document should scan it here rather than ``json.dumps`` it
    first: ``json.dumps`` defaults to ``ensure_ascii=True``, which turns an
    invisible character into the six ASCII bytes ``\\u200b`` before the gate
    can strip it, and escapes quotes that the context patterns rely on.
    """
    if isinstance(node, str):
        return scan_for_secrets(node)
    if isinstance(node, dict):
        hit = _entry_secret_name(node)
        if hit:
            return hit
        for key, value in node.items():
            hit = _keyed_secret_name(key, value) or scan_json_for_secrets(key)
            if hit:
                return hit
            hit = scan_json_for_secrets(value)
            if hit:
                return hit
        return None
    if isinstance(node, list):
        for value in node:
            hit = scan_json_for_secrets(value)
            if hit:
                return hit
    return None


def redact_secrets(content: str) -> Tuple[str, List[str]]:
    """Replace every match of every pattern with ``[REDACTED:<name>]``.

    Returns ``(redacted_text, fired)`` where ``fired`` is the list of
    pattern names that matched at least once, in ``_SECRET_PATTERNS``
    order, deduplicated. The caller must not echo the original matched
    bytes — only the redacted text and pattern names are safe to emit.

    Redaction cascades: patterns run in sequence over the already-redacted
    text, so a later pattern may re-match an earlier ``[REDACTED:<name>]``
    marker and ``fired`` can include a pattern that only matched a marker.
    This is fail-safe — it never leaks original bytes.

    Invisible characters inside a credential are removed so the pattern can
    match the whole token; invisible characters anywhere else are kept.
    """
    if not content:
        return content, []
    if _INVISIBLE.search(content):
        # An invisible character may be hiding a credential from the patterns;
        # remove the ones inside any such credential so the loop below sees,
        # and redacts, the whole token. Invisible characters elsewhere stay.
        content = _strip_invisible_inside_matches(content)
    fired: List[str] = []
    for name, pattern in _SECRET_PATTERNS:
        content, count = pattern.subn(f"[REDACTED:{name}]", content)
        if count:
            fired.append(name)
    return content, fired


def redact_json_leaves(node: Any) -> Any:
    """Recursively :func:`redact_secrets` every string inside a parsed JSON document.

    Dict KEYS are redacted alongside values. A credential is as capable of landing in
    a key as in a value, and no unredacted credential may be persisted; the accepted
    cost is that two keys differing only inside a redacted span collapse into one,
    which loses a member but cannot produce an invalid document. Non-string scalars
    pass through untouched — there is nothing in an ``int`` for a pattern to match.

    PROMOTED HERE from ``script_runner._redact_json_leaves`` by issue #583 Bolt 2, unit
    ``manifest-envelope``, so that BOTH the step-output path and the execution-manifest
    envelope share ONE definition. Two copies of this function would drift, and the
    drift would be silent and security-relevant in the worst direction: one path would
    keep persisting a credential class the other had already learned to catch.

    THIS MODULE IS THE RIGHT HOME because it is a LEAF — it imports only ``re`` and
    ``typing``. ``services/execution_manifest.py`` can therefore depend on it and remain
    a leaf itself, which importing from ``script_runner`` (a heavyweight module) would
    have prevented by inverting the layering that ``step_result.py`` and
    ``step_fingerprint.py`` established.

    OPERATE ON THE PARSED TREE, NEVER ON THE SERIALISED STRING. Redacting a JSON string
    would replace text inside string literals and could span a quote or an escape
    sequence, producing an unparseable document — written successfully and failing on
    every read. Walking parsed values and re-serialising afterwards makes output
    validity hold by construction.

    The structure is context. Scanning a key and its value separately loses what the
    text pattern needs for an AWS secret access key, so a 40-character value under a
    ``SecretAccessKey``-style key, or the ``value`` of a ``{name, value}`` entry whose
    name is one, is redacted whole as ``aws_secret_access_key`` (see
    ``_keyed_secret_name``). A bare 40-character run under any other key is still left
    alone, as in text.
    """
    if isinstance(node, str):
        redacted, _fired = redact_secrets(node)
        return redacted
    if isinstance(node, dict):
        entry_hit = _entry_secret_name(node)
        out: Dict[Any, Any] = {}
        for key, value in node.items():
            hit = _keyed_secret_name(key, value)
            if hit is None and entry_hit is not None and key in _ENTRY_VALUE_KEYS:
                hit = entry_hit
            out[redact_json_leaves(key)] = f"[REDACTED:{hit}]" if hit else redact_json_leaves(value)
        return out
    if isinstance(node, list):
        return [redact_json_leaves(v) for v in node]
    return node
