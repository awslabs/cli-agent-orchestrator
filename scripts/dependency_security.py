#!/usr/bin/env python3
"""Scan every tracked npm, Bun, uv and Cargo lockfile, and gate only what CAO ships (#1398).

INTERFACE (step 1 of #1398; the stubs below raise NotImplementedError until step 2).

Policy
------
Every tracked lockfile is still scanned in full (dev dependencies and unfixed
advisories included). Each finding is then classified:

* ``shipped``   - the (package, version) is in the shipped set measured from the build.
  A HIGH/CRITICAL shipped finding blocks, exactly as #884 does, unless a valid,
  unexpired ignore-file entry covers it.
* ``toolchain`` - everything else (devDependencies, build tooling, ``examples/``).
  Listed in the step summary, ``report.json`` and ``toolchain.sarif``; never blocks.

CLI
---
``python3 scripts/dependency_security.py [--output-dir DIR] [--ignore-file FILE]
[--shipped-set FILE] [--today YYYY-MM-DD]``

* ``--output-dir``   default ``dependency-security-results``; receives ``report.json``,
  ``summary.md``, ``shipped-set.json`` and ``toolchain.sarif``.
* ``--ignore-file``  default ``<repo>/.github/dependency-security/trivyignore.yaml``; a missing
  file means no entries. Only this script reads it: Trivy runs with its ignore file disabled
  (``--ignorefile /dev/null``) and the file's name is not one Trivy auto-loads.
* ``--shipped-set``  read this ``shipped-set.json`` instead of running the producers
  (tests and local proof). It is validated exactly like producer output.
* ``--today``        override the date used for expiry (tests only).

Exit codes: ``0`` passed (toolchain findings and warnings allowed); ``1`` blocked (a
shipped HIGH/CRITICAL not covered by a valid unexpired ignore entry); ``2`` error, which
fails closed: a producer failed or returned an empty set, a tracked lockfile has no
producer (except ``examples/``), the scanner report is malformed, or the ignore file is
invalid. ``report.status`` is ``passed`` / ``blocked`` / ``error``.

shipped-set.json (written by ``collect_shipped_sets``; one entry per tracked lockfile
that ships code; ``examples/**`` lockfiles are deliberately absent)::

    {"schema": 1,
     "sets": {
       "uv.lock":                    {"producer": "uv export --locked --no-dev --all-extras",
                                      "packages": [{"name": "click", "version": "8.1.7"}]},
       "web/package-lock.json":      {"producer": "bundled-packages.json", "packages": [...]},
       "cao_mcp_apps/package-lock.json": {"producer": "bundled-packages.json", "packages": [...]},
       "docusaurus/package-lock.json":   {"producer": "bundled-packages.json", "packages": [...]},
       "tui/Cargo.lock":             {"producer": "cargo tree -e normal", "packages": [...]}
     }}

``packages`` must be non-empty for every entry, names are compared case-insensitively
with ``_`` and ``-`` equal for uv, exactly for npm and cargo.

bundled-packages.json (written by the Vite/Rollup plugin, one file per build config, to
``<project>/.shipped/bundled-packages.<config>.json``; ``.shipped/`` is git-ignored and
never inside a shipped output directory)::

    {"schema": 1, "project": "web", "config": "vite.config.ts",
     "packages": [{"name": "react", "version": "18.3.1"}]}

``load_bundled_packages`` unions every ``bundled-packages*.json`` in the directory and
fails if there are none or the union is empty.

Docusaurus writes the same file format (``config`` is ``client``) to
``docusaurus/.shipped/bundled-packages.client.json`` from a webpack plugin that reads the
modules of the CLIENT bundle only (the server bundle used for static rendering is
excluded), so ``shipped_docusaurus_set`` is ``load_bundled_packages(docusaurus/)``.

Finding classification output (each element of ``report["findings"]``)::

    {"path": "web/package-lock.json", "advisory": "CVE-2026-112366", "severity": "HIGH",
     "package": "picomatch", "version": "4.0.7", "fixed_version": "",
     "classification": "toolchain",            # or "shipped"
     "reason": "not in the bundled set",       # why it was classified so
     "dependency_paths": [["web", "vite", "tinyglobby", "picomatch"]],
     "ignore": null}                           # or {"id", "state": "valid"|"expired", "expired_at"}

``report`` also carries ``blocking`` (shipped, uncovered HIGH/CRITICAL), ``toolchain``
(count of toolchain findings of any severity), ``warnings`` (list of strings, e.g. a
HIGH that exists only in a devDependency) and ``ignore_entries``.

trivyignore.yaml (a ``vulnerabilities`` list shaped like Trivy's, plus the fields CAO requires)::

    vulnerabilities:
      - id: CVE-2026-112366            # required, advisory ID
        url: https://github.com/...    # required, https URL of the advisory
        statement: "Not reachable: ..."# required, non-empty reachability statement
        added: 2026-11-17              # required, ISO date the entry was added
        expired_at: 2026-11-24         # required, ISO date, added < expired_at <= added + 14d
        paths: [web/package-lock.json] # optional; omitted means every lockfile

An entry with a missing field, a bad date, an expiry more than 14 days after ``added``,
or a non-https url makes the whole file invalid (exit 2). An entry whose ``expired_at``
is on or before today is *expired*: it covers nothing and the finding blocks again.

#1400 review round (interface; implemented below)
-------------------------------------------------
New CLI options: ``--policy FILE`` (default ``<repo>/.github/dependency-security/policy.json``;
when passed explicitly a missing file is exit 2, when only the default is missing the checks
that need it are skipped with a warning), ``--base-rev REV`` (default env
``CAO_DEPSEC_BASE_REV``; the base commit whose lockfiles and ignore-file history are compared;
with ``GITHUB_EVENT_NAME=pull_request`` and no base the run is exit 2), ``--baseline FILE``
(default ``.github/dependency-security/shipped-baseline.json``; must be inside the repository
root, because the base revision's copy is read from git at the same relative path),
``--write-baseline`` (write the
measured shipped names to it and exit 0). All failures stay exit 2 (fail closed).

policy.json (checked in; schema 1)::

    {"schema": 1,
     "expected_outputs": [{"project": "web", "config": "web", "lockfile": "web/package-lock.json",
                           "output_dir": "src/cli_agent_orchestrator/web_ui"}, ...],
     "sentinels": {"web/package-lock.json": ["react"], "uv.lock": ["fastapi"], ...},
     "html_writers": {"docusaurus/package-lock.json": ["remark-*", "rehype-*", "@mdx-js/*", ...]}}

* ``expected_outputs`` lists every build that must have recorded
  ``<project>/.shipped/bundled-packages.<config>.json``: web, the 4 MCP App views, docusaurus
  ``client`` and ``server``. A missing or empty record is exit 2 (``verify_build_outputs``).
* Records now also carry ``"output_dir"`` (repo-relative posix path or null) and ``"outputs"``
  (file names relative to it). ``verify_build_outputs`` also walks each ``output_dir`` that
  appears in the policy: every ``.js``, ``.mjs``, ``.css`` and ``.html`` file must be in the
  ``outputs`` of some record for that directory, else exit 2 (unmapped chunk).
* ``sentinels``: Trivy's package inventory for that lockfile must contain these names
  (``verify_sentinels``), and every lockfile must have a non-empty inventory, else exit 2.
* ``html_writers``: glob patterns of docusaurus build-time packages (remark/rehype/MDX/Prism)
  that write shipped HTML; every locked package whose name matches counts as shipped.

Classification (``classify_findings``): a finding is shipped when its canonical NAME is in the
lockfile's shipped set, at ANY version (over-blocking is the safe direction). Each finding gets
``tier``: ``shipped`` | ``malware`` | ``toolchain-fixable-new`` | ``toolchain``, and
``malware`` (bool: advisory id ``MAL-*``, or the scanner title starts with ``Malware in`` /
``Malicious code in``). Blocking (exit 1): shipped HIGH/CRITICAL not validly ignored; ANY
malware finding (no ignore applies); a toolchain HIGH/CRITICAL WITH a fixed version whose
``(name, version)`` is not in the base lockfile (``toolchain-fixable-new``). Toolchain findings
without a fix (picomatch today) stay report-only. ``classify_findings`` gains the keyword
``base_packages: dict[str, set[tuple[str, str]]] | None`` (lockfile path to the names and
versions locked at the base; None disables the new-with-fix tier). Base lockfiles come from
``base_lockfile_packages(root, base_rev, inputs)``, which raises ``BaseUnavailableError``
(a ValueError, exit 2) when ``git show <base_rev>:<path>`` fails for a lockfile that exists at
head; a lockfile that does not exist at the base has an empty set.

Ignore file: ``paths`` (non-empty list of lockfiles) or ``purl`` (``pkg:<type>/<name>[@version]``)
is REQUIRED; neither is IgnoreFileError. After classification an entry that matches no current
shipped finding is an error (``stale_ignore_entries``). The authoritative ``added`` date comes
from git (``entry_added_dates``): the commit date of the first commit in ``<base_rev>`` history
whose diff to the ignore file introduces the entry id; an entry not in the base is dated ``today`` (``--today``, default the real date).
If the repository is shallow and the entry id is found, dating is impossible: exit 2. The YAML
``added`` stays required and checked (not in the future, expiry within 14 days of it), and the
git date is checked the same way.

Summary section "Left the shipped set": ``left_shipped_set(shipped, baseline, lock_names)``
returns ``{"lockfile", "name"}`` records for baseline packages that are still locked but are
not in the measured set.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote

# These formats contain resolved graphs; colocated manifests are unnecessary.
# In particular, Cargo.toml makes Trivy drop locked development dependencies.
LOCKFILES = {
    "package-lock.json": "npm",
    "bun.lock": "bun",
    "uv.lock": "uv",
    "Cargo.lock": "cargo",
}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
BLOCKING = {"HIGH", "CRITICAL"}
MALWARE_TITLES = ("Malware in", "Malicious code in")
TIER_BLOCKING_NON_SHIPPED = ("malware", "toolchain-fixable-new")


def tracked_inputs(root: Path) -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    paths = [
        Path(os.fsdecode(name))
        for name in listing.split(b"\0")
        if name and Path(os.fsdecode(name)).name in LOCKFILES
    ]
    if not any(path.name in LOCKFILES for path in paths):
        raise ValueError("No tracked dependency lockfiles found; coverage cannot be verified")
    for path in paths:
        source = root / path
        if (
            path.is_absolute()
            or ".." in path.parts
            or source.is_symlink()
            or not source.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError(f"Dependency input must be a repository file: {path}")
    return sorted(paths)


def required_text(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Scanner report has a missing or invalid {key}")
    return value


def normalize_report(report: dict, expected: dict[str, str]) -> tuple[list[dict], list[dict]]:
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise ValueError("Unsupported or missing Trivy report schema")
    results = report.get("Results")
    if not isinstance(results, list):
        raise ValueError("Scanner report has no Results array")
    inventory = []
    findings = []
    seen = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Invalid scanner result")
        target = required_text(result, "Target")
        if target not in expected or target in seen or result.get("Type") != expected[target]:
            raise ValueError(
                f"Unexpected, duplicate or incorrectly typed dependency graph: {target}"
            )
        packages = result.get("Packages")
        if not isinstance(packages, list) or not packages:
            raise ValueError(f"No package inventory for {target}; coverage cannot be verified")
        for package in packages:
            if not isinstance(package, dict):
                raise ValueError(f"Invalid package inventory for {target}")
            required_text(package, "Name")
            required_text(package, "Version")
        seen.add(target)
        inventory.append(
            {
                "path": target,
                "ecosystem": expected[target],
                "packages": len(packages),
                "dependencies": [
                    {"name": package["Name"], "version": package["Version"]} for package in packages
                ],
                # In-memory only (dependency paths); scan() removes it before publishing.
                "_graph": [
                    {
                        "ID": package.get("ID"),
                        "Name": package["Name"],
                        "Version": package["Version"],
                        "DependsOn": [
                            item for item in package.get("DependsOn") or [] if isinstance(item, str)
                        ],
                    }
                    for package in packages
                ],
            }
        )
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ValueError(f"Invalid vulnerability list for {target}")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ValueError(f"Invalid vulnerability in {target}")
            severity = required_text(vulnerability, "Severity")
            if severity not in SEVERITIES:
                raise ValueError(f"Unknown scanner severity: {severity}")
            fixed = vulnerability.get("FixedVersion", "")
            if not isinstance(fixed, str):
                raise ValueError("Invalid FixedVersion in scanner report")
            advisory_id = required_text(vulnerability, "VulnerabilityID")
            title = vulnerability.get("Title")
            # Malware advisories (OSV MAL-*, GHSA "Malware in ..."): a flag only; the title
            # text itself is never kept or published.
            malware = advisory_id.startswith("MAL-") or (
                isinstance(title, str) and title.startswith(MALWARE_TITLES)
            )
            findings.append(
                {
                    "path": target,
                    "advisory": required_text(vulnerability, "VulnerabilityID"),
                    "severity": severity,
                    "package": required_text(vulnerability, "PkgName"),
                    "version": required_text(vulnerability, "InstalledVersion"),
                    "fixed_version": fixed,
                    "malware": malware,
                }
            )
    if missing := set(expected) - seen:
        raise ValueError(f"Unscanned lockfiles: {', '.join(sorted(missing))}")
    return sorted(inventory, key=lambda item: item["path"]), findings


def cell(value: object) -> str:
    text = html.escape(str(value)).replace("\r", " ").replace("\n", " ")
    return text.translate({ord(char): f"&#{ord(char)};" for char in "|[]()`\\*"})


def _finding_row(finding: dict, extra: str) -> str:
    values = [
        finding["severity"],
        finding["advisory"],
        finding["package"],
        finding["version"],
        finding["fixed_version"] or "Not published",
        finding["path"],
        extra,
    ]
    return "| " + " | ".join(cell(value) for value in values) + " |"


def _path_text(finding: dict) -> str:
    chains = finding.get("dependency_paths") or []
    return "; ".join(" > ".join(chain) for chain in chains) or "unavailable"


def render_summary(report: dict) -> str:
    lines = [
        "## Dependency Security",
        "",
        f"**Status: {report['status']}**",
        "",
    ]
    if report.get("scanned_at"):
        lines += [f"Scanned: {cell(report['scanned_at'])}", ""]
    if report.get("revision"):
        lines += [f"Revision: {cell(report['revision'])}", ""]
    if report["status"] == "error":
        lines += [
            cell(report["error"]),
            "",
            "No clean security verdict is available.",
            "Package inventory, findings, and blocking count are unavailable.",
            f"Known input hashes retained: **{len(report['input_sha256'])}** (may be partial).",
        ]
        return "\n".join(lines) + "\n"
    findings = report["findings"]
    shipped = [f for f in findings if f.get("classification", "shipped") == "shipped"]
    toolchain = [f for f in findings if f.get("classification") == "toolchain"]
    lines += [
        "Every tracked lockfile is scanned in full, including development and unfixed",
        "dependencies. Findings in the shipped set (code that reaches users) are gated at full",
        "strength: every HIGH/CRITICAL blocks unless a valid, unexpired entry in",
        "`.github/dependency-security/trivyignore.yaml` covers it. Toolchain-only findings are listed below and never block.",
        "",
        "| Lockfile | Ecosystem | Packages |",
        "| --- | --- | ---: |",
    ]
    for graph in report["inventory"]:
        lines.append(
            "| " + " | ".join(cell(graph[key]) for key in ("path", "ecosystem", "packages")) + " |"
        )
    lines += [
        "",
        f"Blocking findings: **{report['blocking']}**. "
        f"Shipped findings: **{len(shipped)}**. Toolchain findings: **{len(toolchain)}**.",
        "Distinct advisories: " f"**{len({finding['advisory'] for finding in findings})}**.",
    ]
    for warning in report.get("warnings") or []:
        lines += ["", f"> Warning: {cell(warning)}"]
    if shipped:
        lines += [
            "",
            "### Shipped",
            "",
            "| Severity | Advisory | Package | Installed | Fixed version | Lockfile | Ignore |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for finding in shipped:
            ignore = finding.get("ignore")
            lines.append(
                _finding_row(
                    finding,
                    f"{ignore['state']} until {ignore['expired_at']}" if ignore else "none",
                )
            )
    blocking_toolchain = [f for f in toolchain if f.get("tier") in TIER_BLOCKING_NON_SHIPPED]
    if blocking_toolchain:
        lines += [
            "",
            "### Blocking, outside the shipped set",
            "",
            "Malware advisories block in any scope. A toolchain HIGH/CRITICAL with a fixed "
            "version blocks when this change adds or changes the package.",
            "",
            "| Severity | Advisory | Package | Installed | Fixed version | Lockfile | Tier |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [_finding_row(f, f["tier"]) for f in blocking_toolchain]
    if report.get("left_shipped"):
        lines += ["", "### Left the shipped set (still locked, no longer bundled)", ""]
        lines += [
            f"- `{cell(i['name'])}` in `{cell(i['lockfile'])}`" for i in report["left_shipped"]
        ]
    toolchain = [f for f in toolchain if f.get("tier") not in TIER_BLOCKING_NON_SHIPPED]
    if toolchain:
        lines += [
            "",
            "### Toolchain only (not blocking)",
            "",
            "| Severity | Advisory | Package | Installed | Fixed version | Lockfile | Dependency path |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for finding in toolchain:
            lines.append(_finding_row(finding, _path_text(finding)))
    return "\n".join(lines) + "\n"


def scan(
    root: Path,
    shipped: dict[str, dict] | None = None,
    ignore_file: Path | None = None,
    today: date | None = None,
    *,
    policy: dict | None = None,
    base_rev: str | None = None,
    baseline_file: Path | None = None,
    require_base: bool = False,
) -> dict:
    """Scan every tracked lockfile and gate only the shipped set.

    ``shipped`` is a ready ``sets`` mapping (tests, ``--shipped-set``); when omitted the
    producers run against the real build output and any failure is an ``error``.
    ``policy`` enables the completeness and sentinel checks, ``base_rev`` the
    new-with-a-fix tier, the git dating of ignore entries and the baseline comparison;
    ``require_base`` makes a missing ``base_rev`` an error (pull-request runs).
    """
    hashes = {}
    today = today or datetime.now(timezone.utc).date()
    try:
        inputs = tracked_inputs(root)
        expected = {
            path.as_posix(): LOCKFILES[path.name] for path in inputs if path.name in LOCKFILES
        }
        with tempfile.TemporaryDirectory(prefix="cao-dependency-scan-") as temporary:
            work = Path(temporary)
            source = work / "source"
            for path in inputs:
                destination = source / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / path, destination)
                hashes[path.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
            output = work / "trivy.json"
            config = work / "trivy.yaml"
            config.write_text("{}\n", encoding="utf-8")
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("TRIVY_") or key == "TRIVY_CACHE_DIR"
            }
            subprocess.run(
                [
                    "trivy",
                    "--config",
                    str(config),
                    "fs",
                    "--scanners",
                    "vuln",
                    "--include-dev-deps",
                    "--ignore-unfixed=false",
                    "--ignore-status",
                    "",
                    "--ignorefile",
                    os.devnull,
                    "--severity",
                    ",".join(sorted(SEVERITIES)),
                    "--skip-db-update=false",
                    "--list-all-pkgs",
                    "--format",
                    "json",
                    "--output",
                    str(output),
                    "--exit-code",
                    "0",
                    "--timeout",
                    "5m",
                    str(source),
                ],
                check=True,
                timeout=330,
                env=environment,
            )
            inventory, findings = normalize_report(
                json.loads(output.read_text(encoding="utf-8")), expected
            )
        # After the scan, so an error here still reports the input hashes. Any failure is
        # an error (exit 2): the gate never defaults to "nothing shipped, so pass".
        if policy is not None:
            verify_sentinels(inventory, policy)
        if require_base and not base_rev:
            raise BaseUnavailableError(
                "a pull-request run needs the base revision (--base-rev or CAO_DEPSEC_BASE_REV)"
            )
        ignore_path = ignore_file or root / ".github" / "dependency-security" / "trivyignore.yaml"
        ignores = load_ignore_file(ignore_path, today)
        git_dates = entry_added_dates(
            root, ignore_path, base_rev, [entry["id"] for entry in ignores], today
        )
        for entry in ignores:
            added = min(git_dates.get(entry["id"], today), today)
            entry["added_git"] = added.isoformat()
            expires = date.fromisoformat(entry["expired_at"])
            if expires > today and (expires - added).days > IGNORE_MAX_DAYS:
                raise IgnoreFileError(
                    f"{entry['id']}: expired_at {entry['expired_at']} is more than "
                    f"{IGNORE_MAX_DAYS} days after the entry was first added to the base "
                    f"({added.isoformat()})"
                )
        base_packages = base_lockfile_packages(root, base_rev, inputs) if base_rev else None
        if shipped is None:
            # Two-argument call without a policy keeps the producer seam the tests patch.
            shipped = (
                collect_shipped_sets(root, inputs, policy)
                if policy is not None
                else collect_shipped_sets(root, inputs)
            )
        else:
            shipped = validate_shipped_sets({"schema": 1, "sets": shipped}, inputs)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "input_sha256": hashes,
            "inventory": None,
            "findings": None,
            "blocking": None,
        }
    if policy is not None:
        add_html_writers(shipped, inventory, policy)
    classified = classify_findings(
        findings, inventory, shipped, ignores, base_packages=base_packages
    )
    stale = stale_ignore_entries(ignores, classified, inventory)
    left = []
    baseline, baseline_source = _read_baseline(root, baseline_file or DEFAULT_BASELINE, base_rev)
    if baseline is not None:
        lock_names = {item["path"]: {d["name"] for d in item["dependencies"]} for item in inventory}
        left = left_shipped_set(shipped, baseline, lock_names)
    for item in inventory:
        item.pop("_graph", None)
    if stale:
        return {
            "status": "error",
            "error": "IgnoreFileError: ignore entries match no current shipped finding: "
            + ", ".join(sorted({e["id"] for e in stale})),
            "input_sha256": hashes,
            "inventory": None,
            "findings": None,
            "blocking": None,
        }
    warnings = []
    if policy is None:
        warnings.append(
            "No policy file was loaded: build completeness and Trivy sentinel checks were skipped."
        )
    if base_rev is None:
        warnings.append(
            "No base revision: the new-with-a-fix tier and ignore-entry dating were not applied."
        )
    for name in left:
        warnings.append(
            f"{name['name']} left the shipped set of {name['lockfile']} (it is still locked); "
            f"compared with {baseline_source}."
        )
    for finding in classified:
        where = f"{finding['package']}@{finding['version']} in {finding['path']}"
        if finding["classification"] == "toolchain" and finding["severity"] in BLOCKING:
            warnings.append(
                f"{finding['advisory']} ({finding['severity']}) affects {where}, which is "
                f"toolchain-only ({finding['reason']}); not blocking."
            )
        if finding["ignore"] and finding["ignore"]["state"] == "expired":
            warnings.append(
                f"The ignore-file entry for {finding['advisory']} expired on "
                f"{finding['ignore']['expired_at']}; {where} blocks again."
            )
    blocking = sum(is_blocking(finding) for finding in classified)
    return {
        "status": "blocked" if blocking else "passed",
        "blocking": blocking,
        "left_shipped": left,
        "toolchain": sum(f["classification"] == "toolchain" for f in classified),
        "toolchain_high": sum(
            f["classification"] == "toolchain" and f["severity"] in BLOCKING for f in classified
        ),
        "malware": sum(f["tier"] == "malware" for f in classified),
        "toolchain_new_with_fix": sum(f["tier"] == "toolchain-fixable-new" for f in classified),
        "warnings": warnings,
        "input_sha256": hashes,
        "inventory": inventory,
        "findings": classified,
        "ignore_entries": ignores,
        "shipped_sets": {
            path: {"producer": entry["producer"], "packages": len(entry["packages"])}
            for path, entry in sorted(shipped.items())
        },
        "_shipped": shipped,
    }


# --- #1398: shipped-set gate ----------------------------------------------------------

#: Lockfiles under these prefixes ship nothing and are always ``toolchain``.
NO_SHIP_PREFIXES = ("examples/",)
#: Named exceptions to NO_SHIP_PREFIXES: example lockfiles whose code is deployed, so their
#: runtime (non-dev) packages are shipped. Owner ruling on #1400: the decision-model-host
#: CDK app is a deployable stack, so what it installs at deploy time runs in an account.
SHIPPED_EXAMPLES = {
    "examples/decision-model-host/aws/cdk/package-lock.json": (
        "deployable CDK app: its runtime dependencies run in the deployed stack"
    ),
}


def ships_nothing(lockfile: str) -> bool:
    """A lockfile under NO_SHIP_PREFIXES that is not a named SHIPPED_EXAMPLES exception."""
    return lockfile.startswith(NO_SHIP_PREFIXES) and lockfile not in SHIPPED_EXAMPLES


def npm_runtime_packages(root: Path, lockfile: str) -> set[tuple[str, str]]:
    """Non-dev packages of an npm lockfile (v2/v3 ``packages`` map, ``"dev": true`` skipped)."""
    try:
        packages = json.loads((root / lockfile).read_text(encoding="utf-8")).get("packages")
    except (OSError, ValueError) as exc:
        raise ShippedSetError(f"{lockfile}: unreadable: {exc}") from exc
    if not isinstance(packages, dict):
        raise ShippedSetError(f"{lockfile}: no lockfile v2/v3 'packages' map")
    pairs = set()
    for key, entry in packages.items():
        if not key or not isinstance(entry, dict) or entry.get("dev") or entry.get("link"):
            continue
        if entry.get("version"):
            folder = key.rsplit("node_modules/", 1)[-1]
            pairs.add((folder, entry["version"]))
            # An npm alias ("helper": "npm:@scope/real-helper@1.0.0") sits in the folder
            # "helper" but Trivy reports the real name, so record both.
            real = entry.get("name")
            if isinstance(real, str) and real and real != folder:
                pairs.add((real, entry["version"]))
    return pairs


IGNORE_MAX_DAYS = 14
EXIT_PASSED, EXIT_BLOCKED, EXIT_ERROR = 0, 1, 2
PRODUCER_TIMEOUT = 300
_UV_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_CARGO_LINE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*) v(\d[^\s]*)")


class ShippedSetError(ValueError):
    """A producer failed, or returned an empty or malformed set. Always fails closed."""


class IgnoreFileError(ValueError):
    """The ignore file is missing a field, has a bad date, or exceeds 14 days."""


class PolicyError(ValueError):
    """policy.json is missing, unreadable or malformed."""


class BaseUnavailableError(ValueError):
    """The base revision (or a lockfile at it) cannot be read; fail closed."""


DEFAULT_POLICY = Path(".github") / "dependency-security" / "policy.json"
DEFAULT_BASELINE = Path(".github") / "dependency-security" / "shipped-baseline.json"
ARTIFACT_SUFFIXES = (".js", ".mjs", ".css", ".html")


def load_policy(path: Path) -> dict:
    """Read and validate policy.json (schema 1)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{path.name}: unreadable: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise PolicyError(f"{path.name}: unsupported or missing schema")
    outputs = document.get("expected_outputs")
    if not isinstance(outputs, list) or not outputs:
        raise PolicyError(f"{path.name}: expected_outputs must be a non-empty list")
    for item in outputs:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key]
            for key in ("project", "config", "lockfile")
        ):
            raise PolicyError(f"{path.name}: invalid expected_outputs entry {item!r}")
        if item.get("output_dir") is not None and not isinstance(item["output_dir"], str):
            raise PolicyError(f"{path.name}: output_dir must be a string or null")
    for key in ("sentinels", "html_writers"):
        table = document.get(key, {})
        if not isinstance(table, dict) or not all(
            isinstance(v, list) and v and all(isinstance(i, str) for i in v) for v in table.values()
        ):
            raise PolicyError(f"{path.name}: {key} must map lockfiles to non-empty string lists")
    return document


def verify_build_outputs(root: Path, policy: dict) -> None:
    """Every expected build recorded a non-empty manifest, and every shipped file is mapped.

    Raises ShippedSetError (exit 2): a missing or empty record, a record that is not
    schema 1, or a ``.js``/``.mjs``/``.css``/``.html`` file under an ``output_dir`` that no
    record lists in ``outputs``.
    """
    records: dict[str, list[tuple[str, set[str]]]] = {}
    for item in policy["expected_outputs"]:
        file = root / item["project"] / ".shipped" / f"bundled-packages.{item['config']}.json"
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShippedSetError(
                f"{item['project']} ({item['config']}): no build record at {file.name}; the "
                f"build did not run, or its plugin is missing ({exc.__class__.__name__})"
            ) from exc
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise ShippedSetError(f"{file.name}: unsupported or missing schema")
        if not _pairs(record.get("packages"), file.name):
            raise ShippedSetError(f"{file.name}: bundled no packages (empty shipped set)")
        output_dir = item.get("output_dir")
        if output_dir:
            outputs = record.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise ShippedSetError(f"{file.name}: lists no outputs for {output_dir}")
            records.setdefault(output_dir, []).append((file.name, {str(o) for o in outputs}))
    for output_dir, mapped in records.items():
        directory = root / output_dir
        if not directory.is_dir():
            raise ShippedSetError(f"{output_dir}: expected build output directory is missing")
        known = set().union(*(names for _, names in mapped))
        unmapped = sorted(
            file.relative_to(directory).as_posix()
            for file in directory.rglob("*")
            if file.is_file()
            and file.suffix in ARTIFACT_SUFFIXES
            and file.relative_to(directory).as_posix() not in known
        )
        if unmapped:
            raise ShippedSetError(
                f"{output_dir}: shipped file(s) not attributed to any build record: "
                f"{', '.join(unmapped[:5])}"
            )


def verify_sentinels(inventory: list[dict], policy: dict) -> None:
    """Trivy parsed every lockfile and saw the packages that must be there."""
    for graph in inventory:
        if not graph["packages"]:
            raise ValueError(f"Trivy parsed no packages from {graph['path']}")
    names = {
        graph["path"]: {_canonical(graph["ecosystem"], d["name"]) for d in graph["dependencies"]}
        for graph in inventory
    }
    for lockfile, wanted in (policy.get("sentinels") or {}).items():
        if lockfile not in names:
            raise ValueError(f"policy sentinel lockfile {lockfile} was not scanned")
        ecosystem = next(g["ecosystem"] for g in inventory if g["path"] == lockfile)
        missing = [n for n in wanted if _canonical(ecosystem, n) not in names[lockfile]]
        if missing:
            raise ValueError(
                f"Trivy did not report {', '.join(missing)} for {lockfile}; its parse of this "
                "lockfile cannot be trusted"
            )


def add_html_writers(shipped: dict[str, dict], inventory: list[dict], policy: dict) -> None:
    """Count build-time packages that write shipped HTML (remark, rehype, MDX ...) as shipped.

    Matched against Trivy's inventory of the lockfile, every version, so the result does not
    depend on how the shipped set was obtained.
    """
    for lockfile, patterns in (policy.get("html_writers") or {}).items():
        if lockfile not in shipped:
            continue
        graph = next((g for g in inventory if g["path"] == lockfile), None)
        if graph is None:
            continue
        entry = shipped[lockfile]
        have = {(p["name"], p["version"]) for p in entry["packages"]}
        added = {
            (d["name"], d["version"])
            for d in graph["dependencies"]
            if any(fnmatch.fnmatchcase(d["name"], pattern) for pattern in patterns)
        } - have
        if added:
            entry["packages"] = entry["packages"] + [
                {"name": n, "version": v} for n, v in sorted(added)
            ]
            entry["producer"] += " + policy html_writers"


def _lock_pairs(kind: str, text: str) -> set[tuple[str, str]]:
    """``(canonical name, version)`` pairs of a lockfile's text."""
    pairs: set[tuple[str, str]] = set()
    if kind == "npm":
        for key, entry in (json.loads(text).get("packages") or {}).items():
            if key and isinstance(entry, dict) and entry.get("version") and not entry.get("link"):
                pairs.add((key.rsplit("node_modules/", 1)[-1], entry["version"]))
    elif kind == "bun":
        document = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
        for value in (document.get("packages") or {}).values():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], str)
                and "@" in value[0][1:]
            ):
                name, version = value[0].rsplit("@", 1)
                pairs.add((name, version))
    else:  # uv, cargo
        import tomllib

        for package in tomllib.loads(text).get("package", []):
            if package.get("name") and package.get("version"):
                pairs.add((_canonical(kind, package["name"]), package["version"]))
    return pairs


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=120, check=False
    )


def base_lockfile_packages(
    root: Path, base_rev: str, inputs: list[Path]
) -> dict[str, set[tuple[str, str]]]:
    """Packages locked at ``base_rev`` per lockfile; raises BaseUnavailableError on any failure.

    A pull-request checkout is the merge of the PR into its base, so diffing it against the
    base commit is exactly the PR's change. A lockfile absent at the base has an empty set.
    """
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base_rev}^{{commit}}").returncode:
        raise BaseUnavailableError(
            f"base revision {base_rev!r} is not available in this checkout (fetch it, or the "
            "new-with-a-fix tier cannot be evaluated)"
        )
    result: dict[str, set[tuple[str, str]]] = {}
    for path in inputs:
        key = path.as_posix()
        if _git(root, "cat-file", "-e", f"{base_rev}:{key}").returncode:
            result[key] = set()
            continue
        shown = _git(root, "show", f"{base_rev}:{key}")
        if shown.returncode:
            raise BaseUnavailableError(f"cannot read {key} at {base_rev}: {shown.stderr.strip()}")
        kind = LOCKFILES[path.name]
        try:
            result[key] = {(_canonical(kind, n), v) for n, v in _lock_pairs(kind, shown.stdout)}
        except (ValueError, KeyError) as exc:
            raise BaseUnavailableError(f"cannot parse {key} at {base_rev}: {exc}") from exc
    return result


def _baseline_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def left_shipped_set(
    shipped: dict[str, dict], baseline: dict, lock_names: dict[str, set[str]]
) -> list[dict]:
    """Baseline packages still locked but no longer in the measured shipped set.

    Returns ``[{"lockfile": <path>, "name": <canonical name>}, ...]`` records.
    """
    left = []
    for lockfile, names in sorted((baseline.get("sets") or {}).items()):
        if lockfile not in shipped:
            continue
        now = {_baseline_name(p["name"]) for p in shipped[lockfile]["packages"]}
        locked = {_baseline_name(n) for n in lock_names.get(lockfile, set())}
        left.extend(
            {"lockfile": lockfile, "name": name}
            for name in names
            if name not in now and name in locked
        )
    return left


def write_baseline(shipped: dict[str, dict], path: Path) -> None:
    sets = {
        lockfile: sorted({_baseline_name(p["name"]) for p in entry["packages"]})
        for lockfile, entry in sorted(shipped.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "sets": sets}, indent=2) + "\n", encoding="utf-8")


def _read_baseline(root: Path, file: Path, base_rev: str | None) -> tuple[dict | None, str]:
    """The baseline from the base revision when it has one, else the checked-out file."""
    relative = file.relative_to(root).as_posix() if file.is_absolute() else file.as_posix()
    if base_rev:
        shown = _git(root, "show", f"{base_rev}:{relative}")
        if not shown.returncode:
            try:
                return json.loads(shown.stdout), f"the base revision {base_rev[:8]}"
            except ValueError:
                pass
    target = file if file.is_absolute() else root / file
    if target.is_file():
        try:
            return json.loads(target.read_text(encoding="utf-8")), "this checkout"
        except ValueError:
            return None, ""
    return None, ""


def _pairs(records: object, source: str) -> set[tuple[str, str]]:
    if not isinstance(records, list):
        raise ShippedSetError(f"{source}: 'packages' is not a list")
    pairs = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("version"), str)
            or not record["name"].strip()
            or not record["version"].strip()
        ):
            raise ShippedSetError(f"{source}: invalid package record {record!r}")
        pairs.add((record["name"], record["version"]))
    return pairs


def load_bundled_packages(project_dir: Path) -> set[tuple[str, str]]:
    """Union of ``<project_dir>/.shipped/bundled-packages*.json``; raise if none or empty."""
    files = sorted((project_dir / ".shipped").glob("bundled-packages*.json"))
    if not files:
        raise ShippedSetError(
            f"{project_dir.name}: no .shipped/bundled-packages*.json; the build did not run "
            "or the bundler plugin is not configured"
        )
    union: set[tuple[str, str]] = set()
    for file in files:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShippedSetError(f"{file.name}: unreadable: {exc}") from exc
        if not isinstance(document, dict) or document.get("schema") != 1:
            raise ShippedSetError(f"{file.name}: unsupported or missing schema")
        pairs = _pairs(document.get("packages"), file.name)
        if not pairs:
            raise ShippedSetError(f"{file.name}: bundled no packages (empty shipped set)")
        union |= pairs
    return union


def _check_view_coverage(project_dir: Path) -> None:
    """Every per-view Vite config must have recorded its bundle."""
    views = sorted(project_dir.glob("vite.config.*.ts"))
    recorded = sorted((project_dir / ".shipped").glob("bundled-packages*.json"))
    if len(recorded) < len(views):
        raise ShippedSetError(
            f"{project_dir.name}: {len(views)} view configs but {len(recorded)} bundled-package "
            "records; a view was not built or its plugin is missing"
        )


def shipped_docusaurus_set(root: Path) -> set[tuple[str, str]]:
    """``load_bundled_packages(root / "docusaurus")``: the client bundle's packages."""
    return load_bundled_packages(root / "docusaurus")


def _run_producer(command: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=PRODUCER_TIMEOUT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or exc
        raise ShippedSetError(
            f"{' '.join(command[:2])} failed: {str(detail).strip()[:300]}"
        ) from exc
    return completed.stdout


def shipped_python_set(root: Path) -> set[tuple[str, str]]:
    """Parse ``uv export --locked --no-dev --all-extras ...`` (``name==version``).

    ``--all-extras``: users install ``cli-agent-orchestrator[otel]`` and ``[agui]``, so the
    optional extras ship even though they are not in the base closure."""
    output = _run_producer(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--all-extras",
            "--no-hashes",
            "--no-emit-project",
        ],
        root,
    )
    pairs = set()
    for line in output.splitlines():
        if match := _UV_LINE.match(line.strip()):
            pairs.add((match.group(1), match.group(2)))
    if not pairs:
        raise ShippedSetError("uv export produced no parseable 'name==version' lines")
    return pairs


def shipped_cargo_set(root: Path) -> set[tuple[str, str]]:
    """Parse ``cargo tree -e normal --prefix none --target all`` for ``tui/``."""
    output = _run_producer(
        [
            "cargo",
            "tree",
            "--locked",
            "--manifest-path",
            "tui/Cargo.toml",
            "-e",
            "normal",
            "--prefix",
            "none",
            "--target",
            "all",
        ],
        root,
    )
    pairs = set()
    for line in output.splitlines():
        if match := _CARGO_LINE.match(line.strip()):
            pairs.add((match.group(1), match.group(2)))
    if not pairs:
        raise ShippedSetError("cargo tree produced no parseable 'name vX.Y.Z' lines")
    return pairs


def _producers(root: Path, policy: dict | None = None) -> dict[str, tuple[str, object]]:
    def bundled(project: str):
        def produce() -> set[tuple[str, str]]:
            if project == "cao_mcp_apps" and policy is None:
                _check_view_coverage(root / project)
            return load_bundled_packages(root / project)

        return produce

    def docusaurus() -> set[tuple[str, str]]:
        return set(shipped_docusaurus_set(root))

    return {
        "uv.lock": ("uv export --locked --no-dev --all-extras", lambda: shipped_python_set(root)),
        "tui/Cargo.lock": ("cargo tree -e normal", lambda: shipped_cargo_set(root)),
        "web/package-lock.json": ("bundled-packages.json", bundled("web")),
        "cao_mcp_apps/package-lock.json": ("bundled-packages.json", bundled("cao_mcp_apps")),
        "docusaurus/package-lock.json": ("bundled-packages.json", docusaurus),
        **{
            lockfile: (
                "package-lock.json non-dev packages",
                lambda lockfile=lockfile: npm_runtime_packages(root, lockfile),
            )
            for lockfile in SHIPPED_EXAMPLES
        },
    }


def collect_shipped_sets(
    root: Path, inputs: list[Path], policy: dict | None = None
) -> dict[str, dict]:
    """Return the ``sets`` mapping of shipped-set.json for every lockfile in ``inputs``.

    Raises ShippedSetError for any producer failure, empty set, or a tracked lockfile
    with no producer that is not under ``NO_SHIP_PREFIXES``.
    """
    if policy is not None:
        verify_build_outputs(root, policy)
    producers = _producers(root, policy)
    sets: dict[str, dict] = {}
    for path in inputs:
        key = path.as_posix()
        if ships_nothing(key):
            continue
        if key not in producers:
            raise ShippedSetError(
                f"{key}: no shipped-set producer for this lockfile. Add one in "
                "scripts/dependency_security.py, or move the project under examples/ if it "
                "ships nothing"
            )
        label, produce = producers[key]
        pairs = produce()
        if not pairs:
            raise ShippedSetError(f"{key}: the {label} producer returned an empty shipped set")
        sets[key] = {
            "producer": label,
            "packages": [{"name": n, "version": v} for n, v in sorted(pairs)],
        }
    return sets


def validate_shipped_sets(document: object, inputs: list[Path]) -> dict[str, dict]:
    """Validate a ``shipped-set.json`` document (``--shipped-set``) exactly like producers."""
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ShippedSetError("shipped-set.json: unsupported or missing schema")
    sets = document.get("sets")
    if not isinstance(sets, dict):
        raise ShippedSetError("shipped-set.json: 'sets' is not an object")
    for path in inputs:
        key = path.as_posix()
        if ships_nothing(key):
            continue
        entry = sets.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("producer"), str):
            raise ShippedSetError(f"shipped-set.json: no entry for {key}")
        if not _pairs(entry.get("packages"), key):
            raise ShippedSetError(f"shipped-set.json: {key} has an empty shipped set")
    return sets


def _canonical(ecosystem: str, name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "uv" else name


def dependency_paths(
    packages: list[dict], name: str, version: str, limit: int = 3
) -> list[list[str]]:
    """Up to ``limit`` root-to-package name chains from Trivy ``Packages[].DependsOn``.

    ``packages`` are Trivy package records (``ID``, ``Name``, ``Version``, ``DependsOn``).
    """
    by_id = {node["ID"]: node for node in packages if node.get("ID")}
    depended_on = {child for node in packages for child in node.get("DependsOn") or []}
    roots = [node for node in packages if node.get("ID") and node["ID"] not in depended_on]
    targets = {
        node["ID"]
        for node in packages
        if node.get("ID") and node.get("Name") == name and node.get("Version") == version
    }
    found: list[list[str]] = []
    queue = deque((root["ID"], [root["Name"]]) for root in roots)
    visited = 0
    while queue and len(found) < limit and visited < 50_000:
        node_id, chain = queue.popleft()
        visited += 1
        if node_id in targets:
            found.append(chain)
            continue
        if len(chain) > 40:
            continue
        for child_id in by_id.get(node_id, {}).get("DependsOn") or []:
            child = by_id.get(child_id)
            if child is not None and child["Name"] not in chain:
                queue.append((child_id, [*chain, child["Name"]]))
    return found


def _as_date(value: object, field: str, position: str) -> date:
    if isinstance(value, datetime):
        raise IgnoreFileError(f"{position}: {field} must be a date, not a timestamp")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise IgnoreFileError(f"{position}: {field} must be an ISO date (YYYY-MM-DD)")


def load_ignore_file(path: Path, today: date) -> list[dict]:
    """Validate and return entries (each gains ``state``: ``valid`` or ``expired``).

    A missing file returns ``[]``. Raises IgnoreFileError on any schema violation.
    """
    if not path.is_file():
        return []
    try:
        import yaml  # imported here: only needed when an ignore file exists
    except ImportError as exc:
        raise IgnoreFileError("PyYAML is required to read the ignore file") from exc
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise IgnoreFileError(f"{path.name}: unreadable: {exc}") from exc
    if document is None:
        return []
    if not isinstance(document, dict) or set(document) - {"vulnerabilities"}:
        raise IgnoreFileError(f"{path.name}: only a top-level 'vulnerabilities' list is allowed")
    records = document.get("vulnerabilities") or []
    if not isinstance(records, list):
        raise IgnoreFileError(f"{path.name}: 'vulnerabilities' must be a list")
    entries = []
    for index, record in enumerate(records):
        position = f"{path.name}: vulnerabilities[{index}]"
        if not isinstance(record, dict):
            raise IgnoreFileError(f"{position}: must be a mapping")
        unknown = set(record) - {"id", "url", "statement", "added", "expired_at", "paths", "purl"}
        if unknown:
            raise IgnoreFileError(f"{position}: unknown field(s) {sorted(unknown)}")
        for field in ("id", "url", "statement", "added", "expired_at"):
            value = record.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise IgnoreFileError(f"{position}: missing {field}")
        if not isinstance(record["id"], str) or not isinstance(record["statement"], str):
            raise IgnoreFileError(f"{position}: id and statement must be strings")
        if not isinstance(record["url"], str) or not record["url"].startswith("https://"):
            raise IgnoreFileError(f"{position}: url must be an https URL")
        added = _as_date(record["added"], "added", position)
        expires = _as_date(record["expired_at"], "expired_at", position)
        if added > today:
            raise IgnoreFileError(
                f"{position}: added {added.isoformat()} is in the future; an entry cannot be "
                "back-dated to stretch the 14-day limit"
            )
        if expires <= added:
            raise IgnoreFileError(f"{position}: expired_at must be after added")
        if (expires - added).days > IGNORE_MAX_DAYS:
            raise IgnoreFileError(
                f"{position}: expired_at is {(expires - added).days} days after added; "
                f"the limit is {IGNORE_MAX_DAYS}"
            )
        if (expires - today).days > IGNORE_MAX_DAYS:
            raise IgnoreFileError(
                f"{position}: expired_at is {(expires - today).days} days from today; "
                f"the limit is {IGNORE_MAX_DAYS}"
            )
        paths = record.get("paths")
        purl = record.get("purl")
        if paths is not None and (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(item, str) for item in paths)
        ):
            raise IgnoreFileError(f"{position}: paths must be a non-empty list of lockfile paths")
        if purl is not None and (not isinstance(purl, str) or not purl.startswith("pkg:")):
            raise IgnoreFileError(f"{position}: purl must look like pkg:<type>/<name>[@<version>]")
        if paths is None and purl is None:
            raise IgnoreFileError(
                f"{position}: an entry needs 'paths' or 'purl'; an entry scoped to nothing would "
                "cover every package that carries the advisory"
            )
        entries.append(
            {
                "id": record["id"].strip(),
                "url": record["url"],
                "statement": record["statement"].strip(),
                "added": added.isoformat(),
                "expired_at": expires.isoformat(),
                "paths": paths,
                "purl": purl,
                "state": "expired" if expires <= today else "valid",
            }
        )
    return entries


_PURL_TYPES = {"npm": "npm", "bun": "npm", "uv": "pypi", "cargo": "cargo"}


def _purl_matches(purl: str, finding: dict, ecosystem: str) -> bool:
    """``pkg:<type>/<name>[@<version>]`` against a finding's package, version and ecosystem."""
    kind, _, rest = purl[len("pkg:") :].partition("/")
    rest = unquote(rest)
    at = rest.find("@", 1)  # a leading "@" is an npm scope, not a version separator
    name, version = (rest, "") if at < 0 else (rest[:at], rest[at + 1 :])
    return (
        _PURL_TYPES.get(ecosystem) == kind
        and _canonical(ecosystem, name) == _canonical(ecosystem, finding["package"])
        and (not version or version == finding["version"])
    )


def _scoped(entry: dict, finding: dict, ecosystem: str) -> bool:
    if entry["id"] != finding["advisory"]:
        return False
    if entry.get("paths") and finding["path"] in entry["paths"]:
        return True
    return bool(entry.get("purl")) and _purl_matches(entry["purl"], finding, ecosystem)


def _covering_entry(finding: dict, ignores: list[dict], ecosystem: str = "") -> dict | None:
    matches = [entry for entry in ignores if _scoped(entry, finding, ecosystem)]
    # A valid entry wins over an expired one for the same finding.
    matches.sort(key=lambda entry: entry["state"] != "valid")
    return matches[0] if matches else None


def stale_ignore_entries(
    entries: list[dict], findings: list[dict], inventory: list[dict] | None = None
) -> list[dict]:
    """Entries that match no current shipped finding (a stale or mistyped waiver)."""
    ecosystems = {i["path"]: i["ecosystem"] for i in inventory or []}
    return [
        entry
        for entry in entries
        if not any(
            f.get("tier", f.get("classification")) == "shipped"
            and _scoped(entry, f, ecosystems.get(f["path"], ""))
            for f in findings
        )
    ]


def entry_added_dates(
    root: Path,
    ignore_file: Path,
    base_rev: str | None,
    ids: list[str],
    today: date | None = None,
) -> dict[str, date]:
    """Date each entry id first appeared in ``base_rev`` history of the ignore file.

    An id that is not in that history is new in this change and is dated ``today``
    (default: the real UTC date). Every id in ``ids`` is in the result.
    The YAML ``added`` field is never trusted for this. A shallow clone cannot date an id it
    does contain (the oldest commit it has would look like the introduction), so that is an
    IgnoreFileError, never a guess.
    """
    try:
        relative = ignore_file.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = None  # outside the repository: nothing in history
    today = today or datetime.now(timezone.utc).date()
    found: dict[str, date] = {}
    if relative is None:
        return {entry_id: today for entry_id in ids}
    for entry_id in ids:
        log = _git(
            root,
            "log",
            base_rev or "HEAD",
            "--reverse",
            "--format=%cs",
            "-S",
            entry_id,
            "--",
            relative,
        )
        if log.returncode:
            raise IgnoreFileError(
                f"cannot read the history of {relative} at {base_rev or 'HEAD'}: {log.stderr.strip()}"
            )
        dates = [line for line in log.stdout.split() if line]
        if not dates:
            found[entry_id] = today  # new in this change
            continue
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            raise IgnoreFileError(
                f"{entry_id}: this checkout is shallow, so the date the entry was added cannot "
                "be established; fetch the full history of the ignore file"
            )
        found[entry_id] = date.fromisoformat(dates[0])
    return found


def classify_findings(
    findings: list[dict],
    inventory: list[dict],
    shipped: dict[str, dict],
    ignores: list[dict],
    *,
    base_packages: dict[str, set[tuple[str, str]]] | None = None,
) -> list[dict]:
    """Add ``classification``, ``tier``, ``reason``, ``dependency_paths`` and ``ignore``.

    Shipped membership is by canonical NAME at ANY version: a lockfile may carry several
    versions of one package and the build records only some, so matching the version too
    would let a vulnerable copy that is bundled look unbundled. Over-blocking is the safe
    direction.
    """
    graphs = {item["path"]: item.get("_graph", []) for item in inventory}
    ecosystems = {item["path"]: item["ecosystem"] for item in inventory}
    members = {
        path: {_canonical(ecosystems.get(path, ""), p["name"]) for p in entry["packages"]}
        for path, entry in shipped.items()
    }
    classified = []
    for finding in findings:
        path = finding["path"]
        record = dict(finding)
        ecosystem = ecosystems.get(path, "")
        name = _canonical(ecosystem, finding["package"])
        if ships_nothing(path):
            record["classification"], record["reason"] = "toolchain", "ships nothing (examples/)"
        elif path not in members:
            record["classification"] = "shipped"
            record["reason"] = "no shipped set for this lockfile; treated as shipped"
        elif name in members[path]:
            record["classification"] = "shipped"
            record["reason"] = f"in the shipped set ({shipped[path]['producer']})"
        else:
            record["classification"] = "toolchain"
            record["reason"] = f"not in the shipped set ({shipped[path]['producer']})"
        if record.get("malware"):
            record["tier"] = "malware"
        elif record["classification"] == "shipped":
            record["tier"] = "shipped"
        elif (
            base_packages is not None
            and finding["severity"] in BLOCKING
            and finding["fixed_version"]
            and (name, finding["version"]) not in base_packages.get(path, set())
        ):
            record["tier"] = "toolchain-fixable-new"
            record["reason"] += "; added or changed by this change and a fixed version exists"
        else:
            record["tier"] = "toolchain"
        record.setdefault("malware", False)
        record["dependency_paths"] = dependency_paths(
            graphs.get(path, []), finding["package"], finding["version"]
        )
        entry = (
            _covering_entry(finding, ignores, ecosystem) if record["tier"] == "shipped" else None
        )
        record["ignore"] = (
            {"id": entry["id"], "state": entry["state"], "expired_at": entry["expired_at"]}
            if entry
            else None
        )
        classified.append(record)
    return classified


def is_blocking(finding: dict) -> bool:
    """Malware, a toolchain HIGH/CRITICAL new with a fix, or a shipped HIGH/CRITICAL not
    covered by a valid, unexpired ignore entry."""
    tier = finding.get("tier") or finding["classification"]
    if tier in ("malware", "toolchain-fixable-new"):
        return True
    return (
        tier == "shipped"
        and finding["severity"] in BLOCKING
        and not (finding["ignore"] and finding["ignore"]["state"] == "valid")
    )


def render_sarif(findings: list[dict]) -> dict:
    """SARIF 2.1.0 log listing the toolchain findings (never uploaded as blocking)."""
    rules = {}
    results = []
    for finding in findings:
        if finding.get("classification") != "toolchain":
            continue
        rules.setdefault(
            finding["advisory"],
            {"id": finding["advisory"], "shortDescription": {"text": finding["advisory"]}},
        )
        chains = "; ".join(" > ".join(chain) for chain in finding.get("dependency_paths", []))
        results.append(
            {
                "ruleId": finding["advisory"],
                "level": "warning" if finding["severity"] in BLOCKING else "note",
                "message": {
                    "text": f"{finding['severity']} {finding['package']}@{finding['version']} "
                    f"is toolchain-only ({finding['reason']})"
                    + (f". Dependency path: {chains}" if chains else "")
                },
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": finding["path"]}}}],
            }
        )
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {"driver": {"name": "CAO dependency gate", "rules": list(rules.values())}},
                "results": results,
            }
        ],
    }


def render_issue_body(report: dict) -> str:
    """Markdown for the weekly tracking issue: unfixed or open toolchain HIGH/CRITICAL."""
    rows = [
        f
        for f in report["findings"]
        if f["classification"] == "toolchain" and f["severity"] in BLOCKING
    ]
    lines = [
        "Toolchain-only HIGH/CRITICAL dependency findings. They do not block pull requests",
        "(they do not reach the wheel, the web UI, the MCP App bundles, the docs site or the",
        "TUI binary), but they are still open. Updated by the weekly Dependency Security run.",
        "",
        f"Revision: `{report.get('revision') or 'unknown'}`",
        "",
        "| Severity | Advisory | Package | Installed | Fixed version | Lockfile | Dependency path |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for f in rows:
        chain = "; ".join(" > ".join(c) for c in f.get("dependency_paths", [])) or "unavailable"
        values = [
            f["severity"],
            f["advisory"],
            f["package"],
            f["version"],
            f["fixed_version"] or "Not published",
            f["path"],
            chain,
        ]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=Path("dependency-security-results"))
    parser.add_argument("--ignore-file", type=Path, default=None)
    parser.add_argument("--shipped-set", type=Path, default=None)
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--base-rev", default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    shipped = None
    document_error = None
    policy = None
    base_rev = args.base_rev or os.environ.get("CAO_DEPSEC_BASE_REV") or None
    require_base = os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    policy_path = args.policy or root / DEFAULT_POLICY
    if args.policy is not None or policy_path.is_file():
        try:
            policy = load_policy(policy_path)
        except PolicyError as exc:
            document_error = exc
    if args.shipped_set is not None:
        try:
            document = json.loads(args.shipped_set.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema") != 1:
                raise ValueError("shipped-set.json: unsupported or missing schema")
            shipped = document.get("sets")
            if not isinstance(shipped, dict):
                raise ValueError("shipped-set.json: 'sets' is not an object")
        except (OSError, ValueError) as exc:
            document_error = exc
    if document_error is not None:
        report = {
            "status": "error",
            "error": f"{type(document_error).__name__}: {document_error}",
            "input_sha256": {},
            "inventory": None,
            "findings": None,
            "blocking": None,
        }
    else:
        try:
            report = scan(
                root,
                shipped=shipped,
                ignore_file=args.ignore_file,
                today=args.today,
                policy=policy,
                base_rev=base_rev,
                baseline_file=args.baseline,
                require_base=require_base,
            )
        except Exception as exc:  # fail closed: a crash is an error, never "blocked" or a pass
            report = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "input_sha256": {},
                "inventory": None,
                "findings": None,
                "blocking": None,
            }
    code = EXIT_ERROR if report["status"] == "error" else int(bool(report["blocking"]))
    report["scanned_at"] = datetime.now(timezone.utc).isoformat()
    report["revision"] = os.environ.get("GITHUB_SHA")
    shipped_sets = report.pop("_shipped", None)
    if args.write_baseline and report["status"] != "error" and shipped_sets is not None:
        write_baseline(shipped_sets, args.baseline or root / DEFAULT_BASELINE)
        print(f"Wrote {args.baseline or DEFAULT_BASELINE}")
    summary = render_summary(report)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")
    if shipped_sets is not None:
        (args.output_dir / "shipped-set.json").write_text(
            json.dumps({"schema": 1, "sets": shipped_sets}, indent=2) + "\n", encoding="utf-8"
        )
    if report["status"] != "error":
        toolchain = [f for f in report["findings"] if f["classification"] == "toolchain"]
        (args.output_dir / "toolchain.sarif").write_text(
            json.dumps(render_sarif(toolchain), indent=2) + "\n", encoding="utf-8"
        )
        if report["toolchain_high"]:
            (args.output_dir / "toolchain-issue.md").write_text(
                render_issue_body(report), encoding="utf-8"
            )
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
