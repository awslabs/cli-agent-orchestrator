"""Structural guard for the `security` job's three-step Trivy contract (#568 OQ-1 / DR-5,
docs/issues/568-js-yaml-omap-dos/design.md).

Nothing else pins this job's step shape: the deleted `test_trivyignore.py` was the only
test that did, and `test_wheel_matrix.py::test_trivy_can_fail_the_build` only checks that
some step somewhere has `exit-code: '1'`. Without this guard, any of the mutations below
would keep every other test green while quietly weakening or disabling a gate.

Steps are identified by their `with:` content, not by `name:`, because a rename shouldn't
blind the guard. Scalars are compared as `str(v).lower()` because YAML parses
`ignore-unfixed: true` as a bool, not the string `'true'`. Each trivy step's `with:` keys
are checked against an explicit allowlist, and no workflow, job or step `env:` may set a
`TRIVY_*` variable: the pinned action's `entrypoint.sh` layers `trivy_envs.txt` on top of
the inherited environment, so an inherited `TRIVY_*` variable reaches Trivy unchanged even
though it never appears under `with:`.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SECURITY_SCAN_SH = REPO_ROOT / "scripts" / "security-scan.sh"

TRIVY_ACTION_PREFIX = "aquasecurity/trivy-action@"
UPLOAD_SARIF_PREFIX = "github/codeql-action/upload-sarif@"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FORBIDDEN_WITH_KEYS = {
    "trivyignores",
    "skip-dirs",
    "skip-files",
    "trivy-config",
    "ignore-policy",
    "continue-on-error",
}

AUTOLOADED_TRIVY_CONFIGS = (
    ".trivyignore",
    ".trivyignore.yaml",
    "trivy.yaml",
    "trivy-secret.yaml",
)

# Exactly the `with:` keys each step uses. Anything else -- `vuln-type`, `version`, a future
# input -- can silently change what a step scans or which binary runs it, with no entry in
# FORBIDDEN_WITH_KEYS to catch it.
STEP_WITH_ALLOWLIST = {
    "report": {"scan-type", "scan-ref", "format", "output", "ignore-unfixed", "exit-code"},
    "secret": {"scan-type", "scan-ref", "scanners", "format", "exit-code"},
    "vuln": {"scan-type", "scan-ref", "format", "severity", "ignore-unfixed", "exit-code"},
}


def _trivy_env_keys(env) -> set[str]:
    return {str(k) for k in (env or {}) if str(k).upper().startswith("TRIVY_")}


def _lower(value) -> str:
    return str(value).lower()


def _is_trivy_step(step: dict) -> bool:
    return str(step.get("uses", "")).startswith(TRIVY_ACTION_PREFIX)


def _classify_trivy_step(step: dict) -> str:
    """Identify a trivy-action step by its `with:` content, not its name."""
    with_ = step.get("with") or {}
    if _lower(with_.get("format")) == "sarif":
        return "report"
    if _lower(with_.get("scanners")) == "secret":
        return "secret"
    return "vuln"


def _load_ci_doc() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _security_steps(doc: dict) -> list:
    return doc["jobs"]["security"]["steps"]


def check_security_job(doc: dict) -> list[str]:
    """Return every guard-assertion violation found in `doc`'s `security` job. Empty = clean."""
    errors: list[str] = []
    job = (doc.get("jobs") or {}).get("security")
    if job is None:
        return ["jobs.security is missing"]
    steps = job.get("steps") or []

    workflow_env_hits = _trivy_env_keys(doc.get("env"))
    if workflow_env_hits:
        errors.append(f"workflow env: sets TRIVY_* keys {sorted(workflow_env_hits)}")
    job_env_hits = _trivy_env_keys(job.get("env"))
    if job_env_hits:
        errors.append(f"job security env: sets TRIVY_* keys {sorted(job_env_hits)}")
    for i, s in enumerate(steps):
        step_env_hits = _trivy_env_keys(s.get("env"))
        if step_env_hits:
            errors.append(f"step {i} env: sets TRIVY_* keys {sorted(step_env_hits)}")

    trivy_indices = [i for i, s in enumerate(steps) if _is_trivy_step(s)]
    if len(trivy_indices) != 3:
        errors.append(f"expected exactly 3 trivy-action steps, found {len(trivy_indices)}")

    shas = {
        steps[i]["uses"].split("@", 1)[1] for i in trivy_indices if "@" in steps[i].get("uses", "")
    }
    if len(shas) > 1:
        errors.append(f"trivy-action steps are pinned to different refs: {sorted(shas)}")
    for sha in shas:
        if not SHA_RE.match(sha):
            errors.append(f"trivy-action pin is not a 40-hex sha: {sha!r}")

    for i in trivy_indices:
        with_ = steps[i].get("with") or {}
        if _lower(with_.get("scan-type")) != "fs":
            errors.append(f"step {i}: scan-type != 'fs'")
        if with_.get("scan-ref") != ".":
            errors.append(f"step {i}: scan-ref != '.'")

    roles: dict[str, list[int]] = {"report": [], "secret": [], "vuln": []}
    for i in trivy_indices:
        roles[_classify_trivy_step(steps[i])].append(i)

    for role in ("report", "secret", "vuln"):
        if len(roles[role]) != 1:
            errors.append(f"expected exactly one {role!r} trivy step, found {len(roles[role])}")

    upload_indices = [
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith(UPLOAD_SARIF_PREFIX)
    ]
    if len(upload_indices) != 1:
        errors.append(f"expected exactly one upload-sarif step, found {len(upload_indices)}")

    if all(len(roles[r]) == 1 for r in ("report", "secret", "vuln")) and len(upload_indices) == 1:
        order = [roles["report"][0], roles["secret"][0], roles["vuln"][0], upload_indices[0]]
        if order != sorted(order):
            errors.append(
                f"steps are out of order (want report < secret gate < vuln gate < upload): {order}"
            )

    if roles["report"]:
        step = steps[roles["report"][0]]
        with_ = step.get("with") or {}
        if _lower(with_.get("format")) != "sarif":
            errors.append("report: format != 'sarif'")
        if _lower(with_.get("exit-code")) != "0":
            errors.append("report: exit-code != '0'")
        if _lower(with_.get("ignore-unfixed")) != "true":
            errors.append("report: ignore-unfixed is not true")
        upload_sarif_file = None
        if upload_indices:
            upload_sarif_file = (steps[upload_indices[0]].get("with") or {}).get("sarif_file")
        if with_.get("output") != upload_sarif_file:
            errors.append(
                f"report: output {with_.get('output')!r} != upload sarif_file {upload_sarif_file!r}"
            )
        for key in ("severity", "scanners", "limit-severities-for-sarif"):
            if key in with_:
                errors.append(f"report: has forbidden key {key!r}")
        extra = set(with_.keys()) - STEP_WITH_ALLOWLIST["report"]
        if extra:
            errors.append(f"report: with: has keys outside the allowlist {sorted(extra)}")
        if "if" in step:
            errors.append("report: has an 'if'")

    if roles["secret"]:
        step = steps[roles["secret"][0]]
        with_ = step.get("with") or {}
        if with_.get("scanners") != "secret":
            errors.append(f"secret gate: scanners != 'secret' (got {with_.get('scanners')!r})")
        if _lower(with_.get("format")) != "table":
            errors.append("secret gate: format != 'table'")
        if _lower(with_.get("exit-code")) != "1":
            errors.append("secret gate: exit-code != '1'")
        for key in ("severity", "limit-severities-for-sarif"):
            if key in with_:
                errors.append(f"secret gate: has forbidden key {key!r}")
        extra = set(with_.keys()) - STEP_WITH_ALLOWLIST["secret"]
        if extra:
            errors.append(f"secret gate: with: has keys outside the allowlist {sorted(extra)}")
        if "if" in step:
            errors.append("secret gate: has an 'if'")

    if roles["vuln"]:
        step = steps[roles["vuln"][0]]
        with_ = step.get("with") or {}
        if _lower(with_.get("format")) != "table":
            errors.append("vulnerability gate: format != 'table'")
        if _lower(with_.get("exit-code")) != "1":
            errors.append("vulnerability gate: exit-code != '1'")
        severity = {
            p.strip().upper() for p in str(with_.get("severity", "")).split(",") if p.strip()
        }
        if severity != {"CRITICAL", "HIGH", "UNKNOWN"}:
            errors.append(
                f"vulnerability gate: severity set {severity} != {{'CRITICAL', 'HIGH', 'UNKNOWN'}}"
            )
        if _lower(with_.get("ignore-unfixed")) != "true":
            errors.append("vulnerability gate: ignore-unfixed is not true")
        if "scanners" in with_:
            errors.append("vulnerability gate: has forbidden key 'scanners'")
        extra = set(with_.keys()) - STEP_WITH_ALLOWLIST["vuln"]
        if extra:
            errors.append(
                f"vulnerability gate: with: has keys outside the allowlist {sorted(extra)}"
            )
        if "if" in step:
            errors.append("vulnerability gate: has an 'if'")

    if upload_indices:
        step = steps[upload_indices[0]]
        if _lower(step.get("if")) != "always()":
            errors.append(f"upload: if != 'always()' (got {step.get('if')!r})")

    for i, s in enumerate(steps):
        with_ = s.get("with") or {}
        hit = FORBIDDEN_WITH_KEYS & set(with_.keys())
        if hit:
            errors.append(f"step {i}: forbidden with: keys {sorted(hit)}")
        if "continue-on-error" in s:
            errors.append(f"step {i}: has continue-on-error")
    if "continue-on-error" in job:
        errors.append("job security: has continue-on-error")
    if "if" in job:
        errors.append("job security: has a job-level if")

    return errors


def check_no_autoloaded_trivy_config(root: Path) -> list[str]:
    """Trivy auto-loads these cwd-relative files by default; none may exist at the repo root."""
    return [name for name in AUTOLOADED_TRIVY_CONFIGS if (root / name).exists()]


def _strip_comment_lines(script_text: str) -> str:
    return "\n".join(line for line in script_text.splitlines() if not line.strip().startswith("#"))


def _trivy_fs_calls(script_text: str) -> list[str]:
    """Each `trivy fs \\` invocation up through its scan-path line, as a single string.

    Comment lines are dropped first, so a commented-out `trivy fs` or `--exit-code 1` can't
    be mistaken for a live one.
    """
    text = _strip_comment_lines(script_text)
    return re.findall(r"trivy fs\b.*?\n\s*\.\s*(?:\|\|[^\n]*)?", text, flags=re.DOTALL)


def check_local_mirror(script_text: str) -> list[str]:
    errors: list[str] = []
    calls = _trivy_fs_calls(script_text)
    secret_calls = [c for c in calls if re.search(r"--scanners\s+secret\b", c)]
    severity_calls = [c for c in calls if re.search(r"--severity\s+", c)]

    if len(secret_calls) != 1:
        errors.append(
            f"expected exactly one 'trivy fs --scanners secret' call, found {len(secret_calls)}"
        )
    for call in secret_calls:
        if "--severity" in call:
            errors.append("the secret-scan call also passes --severity, narrowing it")

    if len(severity_calls) != 1:
        errors.append(
            f"expected exactly one 'trivy fs --severity ...' call, found {len(severity_calls)}"
        )
    for call in severity_calls:
        match = re.search(r"--severity\s+(\S+)", call)
        severity = {p.strip().upper() for p in match.group(1).split(",")} if match else set()
        if severity != {"CRITICAL", "HIGH", "UNKNOWN"}:
            errors.append(
                f"the vulnerability call's --severity set is {severity}, not CRITICAL/HIGH/UNKNOWN"
            )
        if "--ignore-unfixed" not in call:
            errors.append("the vulnerability call is missing --ignore-unfixed")
        if "--scanners" in call:
            errors.append(
                "the vulnerability call narrows --scanners; it should scan the Trivy default set"
            )

    if len(calls) != 2:
        errors.append(f"expected exactly 2 'trivy fs' calls, found {len(calls)}")

    for call in calls:
        if not re.search(r"--exit-code\s+1\b", call):
            errors.append(
                f"a trivy fs call has no --exit-code 1, so it can't fail the script: {call!r}"
            )
        if not re.search(r"\|\|\s*exit_code=1\s*$", call):
            errors.append(
                f"a trivy fs call doesn't end in '|| exit_code=1', so it can't fail the script: {call!r}"
            )

    return errors


def test_security_job_matches_the_step_contract():
    assert check_security_job(_load_ci_doc()) == []


def test_local_mirror_matches_the_gates():
    assert check_local_mirror(SECURITY_SCAN_SH.read_text(encoding="utf-8")) == []


def test_no_autoloaded_trivy_config_at_repo_root():
    assert check_no_autoloaded_trivy_config(REPO_ROOT) == []


def _trivy_role_index(doc: dict, role: str) -> int:
    steps = _security_steps(doc)
    for i, step in enumerate(steps):
        if _is_trivy_step(step) and _classify_trivy_step(step) == role:
            return i
    raise AssertionError(f"no {role!r} trivy step found in fixture")


def _mutate_gate_gets_if_failure(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["if"] = "failure()"


def _mutate_upload_loses_always(doc: dict) -> None:
    for step in _security_steps(doc):
        if str(step.get("uses", "")).startswith(UPLOAD_SARIF_PREFIX):
            step.pop("if", None)


def _mutate_gate_becomes_sarif(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["with"]["format"] = "sarif"


def _mutate_unknown_dropped(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["with"]["severity"] = "CRITICAL,HIGH"


def _mutate_severity_narrowed_to_critical(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["with"]["severity"] = "CRITICAL"


def _mutate_secret_step_deleted(doc: dict) -> None:
    steps = _security_steps(doc)
    del steps[_trivy_role_index(doc, "secret")]


def _mutate_secret_step_gets_severity(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "secret")]["with"]["severity"] = "CRITICAL,HIGH"


def _mutate_trivyignores_added(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "report")]["with"]["trivyignores"] = ".trivyignore.yaml"


def _mutate_report_moves_after_a_gate(doc: dict) -> None:
    steps = _security_steps(doc)
    report_i = _trivy_role_index(doc, "report")
    vuln_i = _trivy_role_index(doc, "vuln")
    steps[report_i], steps[vuln_i] = steps[vuln_i], steps[report_i]


def _mutate_secret_gate_env_trivy_severity(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "secret")]["env"] = {"TRIVY_SEVERITY": "CRITICAL,HIGH"}


def _mutate_job_env_trivy_severity(doc: dict) -> None:
    doc["jobs"]["security"]["env"] = {"TRIVY_SEVERITY": "CRITICAL,HIGH"}


def _mutate_secret_gate_env_trivy_skip_dirs(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "secret")]["env"] = {"TRIVY_SKIP_DIRS": "plantdir"}


def _mutate_workflow_env_trivy_ignorefile(doc: dict) -> None:
    doc["env"] = dict(doc.get("env") or {})
    doc["env"]["TRIVY_IGNOREFILE"] = ".github/trivy-exemptions.yaml"


def _mutate_vuln_gate_gets_vuln_type(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["with"]["vuln-type"] = "os"


def _mutate_vuln_gate_gets_version_pin(doc: dict) -> None:
    steps = _security_steps(doc)
    steps[_trivy_role_index(doc, "vuln")]["with"]["version"] = "v0.40.0"


MUTATIONS = {
    "gate_gets_if_failure": _mutate_gate_gets_if_failure,
    "upload_loses_always": _mutate_upload_loses_always,
    "gate_becomes_sarif": _mutate_gate_becomes_sarif,
    "unknown_dropped": _mutate_unknown_dropped,
    "severity_narrowed_to_critical": _mutate_severity_narrowed_to_critical,
    "secret_step_deleted": _mutate_secret_step_deleted,
    "secret_step_gets_severity": _mutate_secret_step_gets_severity,
    "trivyignores_added": _mutate_trivyignores_added,
    "report_moves_after_a_gate": _mutate_report_moves_after_a_gate,
    "secret_gate_env_trivy_severity": _mutate_secret_gate_env_trivy_severity,
    "job_env_trivy_severity": _mutate_job_env_trivy_severity,
    "secret_gate_env_trivy_skip_dirs": _mutate_secret_gate_env_trivy_skip_dirs,
    "workflow_env_trivy_ignorefile": _mutate_workflow_env_trivy_ignorefile,
    "vuln_gate_gets_vuln_type": _mutate_vuln_gate_gets_vuln_type,
    "vuln_gate_gets_version_pin": _mutate_vuln_gate_gets_version_pin,
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_guard_catches_each_mutation(name):
    """A guard that cannot fail is worse than no guard: it reports success either way."""
    doc = copy.deepcopy(_load_ci_doc())
    MUTATIONS[name](doc)
    violations = check_security_job(doc)
    assert violations != [], f"mutation {name!r} was not caught"


def test_guard_catches_a_root_trivyignore(tmp_path):
    (tmp_path / ".trivyignore").write_text("", encoding="utf-8")
    assert check_no_autoloaded_trivy_config(tmp_path) != []


def _mutate_secret_call_loses_exit_code_flag(script_text: str) -> str:
    return re.sub(r"(--scanners secret \\\n\s*)--exit-code 1 \\\n", r"\1", script_text, count=1)


def _mutate_secret_call_exit_code_becomes_true(script_text: str) -> str:
    return re.sub(r"\|\|\s*exit_code=1", "|| true", script_text, count=1)


LOCAL_MIRROR_MUTATIONS = {
    "secret_call_loses_exit_code_flag": _mutate_secret_call_loses_exit_code_flag,
    "secret_call_exit_code_becomes_true": _mutate_secret_call_exit_code_becomes_true,
}


@pytest.mark.parametrize("name", sorted(LOCAL_MIRROR_MUTATIONS))
def test_local_mirror_guard_catches_each_mutation(name):
    original = SECURITY_SCAN_SH.read_text(encoding="utf-8")
    mutated = LOCAL_MIRROR_MUTATIONS[name](original)
    assert mutated != original, f"mutation {name!r} did not change the script"
    assert check_local_mirror(mutated) != [], f"mutation {name!r} was not caught"
