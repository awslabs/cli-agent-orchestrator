#!/usr/bin/env python3
"""Scan every tracked npm, Bun, uv and Cargo lockfile, including unfixed/dev dependencies."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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
            findings.append(
                {
                    "path": target,
                    "advisory": required_text(vulnerability, "VulnerabilityID"),
                    "severity": severity,
                    "package": required_text(vulnerability, "PkgName"),
                    "version": required_text(vulnerability, "InstalledVersion"),
                    "fixed_version": fixed,
                }
            )
    if missing := set(expected) - seen:
        raise ValueError(f"Unscanned lockfiles: {', '.join(sorted(missing))}")
    return sorted(inventory, key=lambda item: item["path"]), findings


def cell(value: object) -> str:
    text = html.escape(str(value)).replace("\r", " ").replace("\n", " ")
    return text.translate({ord(char): f"&#{ord(char)};" for char in "|[]()`\\*"})


def render_summary(report: dict) -> str:
    lines = ["## Dependency Security", "", f"**Status: {report['status']}**", ""]
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
    else:
        lines += [
            "Full locked graphs, including development and unfixed dependencies.",
            "Every HIGH/CRITICAL finding blocks; local mitigations are not exemptions.",
            "",
            "| Lockfile | Ecosystem | Packages |",
            "| --- | --- | ---: |",
        ]
        for graph in report["inventory"]:
            lines.append(
                "| "
                + " | ".join(cell(graph[key]) for key in ("path", "ecosystem", "packages"))
                + " |"
            )
        lines += [
            "",
            f"Blocking findings: **{report['blocking']}**. "
            f"Total findings: **{len(report['findings'])}**.",
            "Distinct advisories: "
            f"**{len({finding['advisory'] for finding in report['findings']})}**.",
        ]
        if report["findings"]:
            lines += [
                "",
                "| Severity | Advisory | Package | Installed | Fixed version | Lockfile |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for finding in report["findings"]:
                values = [
                    finding["severity"],
                    finding["advisory"],
                    finding["package"],
                    finding["version"],
                    finding["fixed_version"] or "Not published",
                    finding["path"],
                ]
                lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return "\n".join(lines) + "\n"


def scan(root: Path) -> dict:
    hashes = {}
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
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "input_sha256": hashes,
            "inventory": None,
            "findings": None,
            "blocking": None,
        }
    blocking = sum(finding["severity"] in BLOCKING for finding in findings)
    return {
        "status": "blocked" if blocking else "passed",
        "blocking": blocking,
        "input_sha256": hashes,
        "inventory": inventory,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dependency-security-results"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report = scan(root)
    code = 2 if report["status"] == "error" else int(bool(report["blocking"]))
    report["scanned_at"] = datetime.now(timezone.utc).isoformat()
    report["revision"] = os.environ.get("GITHUB_SHA")
    summary = render_summary(report)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
