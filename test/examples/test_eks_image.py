"""The EKS image's default build stays clear of the findings it shipped with.

Amazon Inspector, which scans the image in ECR, reported two critical and five
high findings on the Debian-based build (curl, expat, zlib, gnupg2), and Debian
13 had a fixed build of none of them. The default base is now Wolfi, pinned by
digest. These checks read the Dockerfile, so a later edit cannot quietly undo
that; building and scanning the image is the job of the EKS guide.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "examples/cao-clusters/kubernetes/eks/Dockerfile"


def _run_step(marker: str) -> str:
    """The RUN instruction that contains ``marker``, its lines joined.

    As Docker parses one: a backslash continues it, and a comment line inside
    it is skipped without ending it.
    """
    lines = DOCKERFILE.read_text().splitlines()
    at = next(i for i, line in enumerate(lines) if marker in line)
    start = max(i for i in range(at + 1) if lines[i].startswith("RUN "))
    step = []
    for line in lines[start:]:
        if line.strip().startswith("#"):
            continue
        step.append(line)
        if not line.rstrip().endswith("\\"):
            return "\n".join(step)
    raise AssertionError(f"the RUN step with {marker!r} never ends")


def test_the_default_base_is_wolfi_pinned_by_digest():
    match = re.search(r"^ARG BASE_PROVIDER_IMAGE=(\S+)$", DOCKERFILE.read_text(), re.M)
    assert match, "no default BASE_PROVIDER_IMAGE"
    assert re.fullmatch(r"cgr\.dev/chainguard/wolfi-base@sha256:[0-9a-f]{64}", match.group(1))


def test_the_default_build_upgrades_the_bases_packages_before_adding_any():
    # The base is pinned, its packages are not: each build takes Wolfi's fixes.
    step = _run_step("apk upgrade --no-cache")
    assert step.index("apk upgrade --no-cache") < step.index("apk add --no-cache")


def test_nodesource_is_used_only_on_a_debian_base():
    # NodeSource is a third-party apt repository, set up with gnupg, whose
    # findings had no fix either. On Wolfi, Node comes from the distribution.
    step = _run_step("deb.nodesource.com")
    apk = step.index("command -v apk")
    apk_branch = step[apk : step.index("else", apk)]
    assert "apk add --no-cache nodejs-24 npm" in apk_branch
    assert "nodesource" not in apk_branch


def test_claude_codes_install_script_may_fetch_its_native_binary():
    # npm 12 runs no install script unless allowed by name; without this one,
    # `claude` fails with "native binary not installed".
    step = _run_step("@anthropic-ai/claude-code@")
    assert "--allow-scripts=@anthropic-ai/claude-code" in step
