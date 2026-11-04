"""Structural guard for install-trivy/action.yml's own security properties (aquasecurity/setup-trivy#42).

Nothing else pins these: the step's `with:`/`env:` contract is checked by
test_workflow_pins.py and test_security_scan_steps.py, but neither reads the
`run:` script itself. Without this guard, deleting the SHA-256 check, making
the comparison vacuous, widening the tar extraction, or dropping
`--proto '=https'` from the curl call would all keep every other test, zizmor,
actionlint and shellcheck green while silently installing an unverified
binary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = REPO_ROOT / ".github" / "actions" / "install-trivy" / "action.yml"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CURL_LINE_RE = re.compile(r"^\s*curl\b.*$", re.MULTILINE)
TAR_LINE_RE = re.compile(r"^\s*tar\b.*$", re.MULTILINE)
SHA_COMPARE_AND_EXIT_RE = re.compile(
    r'if \[ "\$\{ACTUAL_SHA256\}" != "\$\{TRIVY_SHA256\}" \]; then\n'
    r"(?:.*\n)*?"
    r"\s*exit [1-9]\d*\s*\n"
    r"\s*fi\b"
)
SUDO_INSTALL_LINE_RE = re.compile(r"^\s*sudo install\b.*$", re.MULTILINE)
ACTUAL_SHA_FROM_TARBALL_RE = re.compile(r'ACTUAL_SHA256="\$\(sha256sum\s+"\$\{TARBALL\}"')
SHA256_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+|declare\s+(?:-\w+\s+)*|readonly\s+)?(TRIVY_SHA256|ACTUAL_SHA256)=",
    re.MULTILINE,
)


def _install_step(action_text: str) -> dict:
    doc = yaml.safe_load(action_text) or {}
    steps = ((doc.get("runs") or {}).get("steps")) or []
    assert len(steps) == 1, f"expected exactly one install-trivy step, found {len(steps)}"
    return steps[0]


def check_install_trivy_security(action_text: str) -> list[str]:
    """Return every guard-assertion violation found in the installer. Empty = clean."""
    errors: list[str] = []
    step = _install_step(action_text)
    env = step.get("env") or {}
    script = step.get("run") or ""

    sha = str(env.get("TRIVY_SHA256", ""))
    if not SHA256_RE.match(sha):
        errors.append(f"TRIVY_SHA256 {sha!r} does not match ^[0-9a-f]{{64}}$")

    curl_lines = CURL_LINE_RE.findall(script)
    if len(curl_lines) != 1:
        errors.append(f"expected exactly one curl call, found {len(curl_lines)}")
    else:
        if "--fail" not in curl_lines[0]:
            errors.append("curl call is missing --fail")
        if "--proto '=https'" not in curl_lines[0]:
            errors.append("curl call is missing --proto '=https'")

    compare_match = SHA_COMPARE_AND_EXIT_RE.search(script)
    if not compare_match:
        errors.append(
            'no \'if [ "${ACTUAL_SHA256}" != "${TRIVY_SHA256}" ]\' branch that exits non-zero'
        )
    else:
        install_match = SUDO_INSTALL_LINE_RE.search(script)
        if install_match is None:
            errors.append("no 'sudo install' line to order the SHA-256 check against")
        elif compare_match.start() > install_match.start():
            errors.append(
                "the SHA-256 compare-and-exit happens after 'sudo install', not before it -- "
                "the unverified binary would already be on disk by the time the check could fail"
            )

    if not ACTUAL_SHA_FROM_TARBALL_RE.search(script):
        errors.append(
            "ACTUAL_SHA256 is not computed by hashing the downloaded ${TARBALL}, so the "
            "comparison can't catch a tampered or truncated download"
        )

    sha_assignments = SHA256_ASSIGNMENT_RE.findall(script)
    if sha_assignments.count("TRIVY_SHA256") or sha_assignments.count("ACTUAL_SHA256") != 1:
        errors.append(
            f"expected run: to assign ACTUAL_SHA256 exactly once and never reassign "
            f"TRIVY_SHA256, found assignments {sha_assignments}"
        )

    tar_lines = TAR_LINE_RE.findall(script)
    if len(tar_lines) != 1:
        errors.append(f"expected exactly one tar call, found {len(tar_lines)}")
    elif not re.search(r'"\$\{TARBALL\}"\s+trivy\s*$', tar_lines[0].strip()):
        errors.append(
            f"tar call does not extract exactly the single member 'trivy': {tar_lines[0]!r}"
        )

    if "/usr/local/bin/trivy" not in script:
        errors.append("script does not install to /usr/local/bin/trivy")

    return errors


def test_install_trivy_action_passes_its_own_security_checks():
    assert check_install_trivy_security(ACTION_PATH.read_text(encoding="utf-8")) == []


def _mutate_delete_sha_check(text: str) -> str:
    """Delete the SHA-256 check -- nothing refuses a mismatched tarball."""
    block = (
        'if [ "${ACTUAL_SHA256}" != "${TRIVY_SHA256}" ]; then\n'
        '          echo "::error::install-trivy: SHA-256 mismatch for ${TARBALL} '
        '(expected ${TRIVY_SHA256}, got ${ACTUAL_SHA256}). Refusing to install."\n'
        "          exit 1\n"
        "        fi\n\n"
    )
    assert block in text, "fixture text drifted from the mutant's expected block"
    return text.replace(block, "")


def _mutate_comparison_always_passes(text: str) -> str:
    """The comparison can never be true, so a mismatch is never caught."""
    needle = 'if [ "${ACTUAL_SHA256}" != "${TRIVY_SHA256}" ]; then'
    replacement = 'if [ "${ACTUAL_SHA256}" != "${ACTUAL_SHA256}" ]; then'
    assert needle in text, "fixture text drifted from the mutant's expected comparison"
    return text.replace(needle, replacement, 1)


def _mutate_tar_extracts_everything(text: str) -> str:
    """tar extracts the whole tarball instead of only the `trivy` member."""
    needle = 'tar -xzf "${TARBALL}" trivy\n'
    replacement = 'tar -xzf "${TARBALL}"\n'
    assert needle in text, "fixture text drifted from the mutant's expected tar call"
    return text.replace(needle, replacement, 1)


def _mutate_drop_https_proto(text: str) -> str:
    """The download no longer pins the protocol to HTTPS."""
    needle = "--proto '=https' "
    assert needle in text, "fixture text drifted from the mutant's expected curl flags"
    return text.replace(needle, "", 1)


_HASH_AND_COMPARE_BLOCK = (
    '        ACTUAL_SHA256="$(sha256sum "${TARBALL}" | awk \'{print $1}\')"\n'
    '        if [ "${ACTUAL_SHA256}" != "${TRIVY_SHA256}" ]; then\n'
    '          echo "::error::install-trivy: SHA-256 mismatch for ${TARBALL} '
    '(expected ${TRIVY_SHA256}, got ${ACTUAL_SHA256}). Refusing to install."\n'
    "          exit 1\n"
    "        fi\n"
)
_EXTRACT_AND_INSTALL_BLOCK = (
    '        tar -xzf "${TARBALL}" trivy\n'
    "        chmod +x trivy\n"
    "        sudo install -m 0755 trivy /usr/local/bin/trivy\n"
)


def _mutate_sha_check_moved_after_install(text: str) -> str:
    """The SHA-256 check moves below `sudo install`, so the unverified binary is already
    on disk by the time the check could fail."""
    original_order = f"{_HASH_AND_COMPARE_BLOCK}\n{_EXTRACT_AND_INSTALL_BLOCK}"
    mutated_order = f"{_EXTRACT_AND_INSTALL_BLOCK}\n{_HASH_AND_COMPARE_BLOCK}"
    assert original_order in text, "fixture text drifted from the mutant's expected block order"
    return text.replace(original_order, mutated_order, 1)


def _mutate_actual_sha_copied_from_pinned_value(text: str) -> str:
    """ACTUAL_SHA256 is copied from the pinned TRIVY_SHA256 instead of hashing the
    download, so the comparison can never fail."""
    needle = 'ACTUAL_SHA256="$(sha256sum "${TARBALL}" | awk \'{print $1}\')"'
    replacement = 'ACTUAL_SHA256="${TRIVY_SHA256}"'
    assert needle in text, "fixture text drifted from the mutant's expected hash assignment"
    return text.replace(needle, replacement, 1)


_EXTRACT_AND_MV_BLOCK = (
    '        tar -xzf "${TARBALL}" trivy\n'
    "        chmod +x trivy\n"
    "        sudo mv trivy /usr/local/bin/trivy\n"
)


def _mutate_install_via_mv_has_no_order_anchor(text: str) -> str:
    """The install step uses `sudo mv` instead of `sudo install`, and the SHA-256 check
    moves below it. With no 'sudo install' line to order against, the order check would
    be a silent no-op, and the unverified binary is already on disk by the time the
    check runs."""
    original_order = f"{_HASH_AND_COMPARE_BLOCK}\n{_EXTRACT_AND_INSTALL_BLOCK}"
    mutated_order = f"{_EXTRACT_AND_MV_BLOCK}\n{_HASH_AND_COMPARE_BLOCK}"
    assert original_order in text, "fixture text drifted from the mutant's expected block order"
    return text.replace(original_order, mutated_order, 1)


def _mutate_actual_sha_overridden_after_hash(text: str) -> str:
    """ACTUAL_SHA256 is hashed correctly, then immediately overwritten by an `export`
    assignment copied from the pinned value, so the comparison can never fail."""
    needle = '        ACTUAL_SHA256="$(sha256sum "${TARBALL}" | awk \'{print $1}\')"\n'
    replacement = needle + '        export ACTUAL_SHA256="${TRIVY_SHA256}"\n'
    assert needle in text, "fixture text drifted from the mutant's expected hash assignment"
    return text.replace(needle, replacement, 1)


MUTATIONS = {
    "delete_sha_check": _mutate_delete_sha_check,
    "comparison_always_passes": _mutate_comparison_always_passes,
    "tar_extracts_everything": _mutate_tar_extracts_everything,
    "drop_https_proto": _mutate_drop_https_proto,
    "sha_check_moved_after_install": _mutate_sha_check_moved_after_install,
    "actual_sha_copied_from_pinned_value": _mutate_actual_sha_copied_from_pinned_value,
    "install_via_mv_has_no_order_anchor": _mutate_install_via_mv_has_no_order_anchor,
    "actual_sha_overridden_after_hash": _mutate_actual_sha_overridden_after_hash,
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_guard_catches_each_mutation(name):
    """A guard that cannot fail is worse than no guard: it reports success either way."""
    original = ACTION_PATH.read_text(encoding="utf-8")
    mutated = MUTATIONS[name](original)
    assert mutated != original, f"mutation {name!r} did not change the action"
    assert check_install_trivy_security(mutated) != [], f"mutation {name!r} was not caught"
