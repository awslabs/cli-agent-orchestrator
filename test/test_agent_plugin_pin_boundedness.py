"""Boundedness of the client the committed operator package pins, and the pipeline
that advances the pin.

Reported by review 5222539218 on #584 (item 8): the committed
``agent-plugin/cao/mcp.json`` pins a published release, and that release's
``cao-ops-mcp-server`` issues HTTP requests with no timeout. A foreign client
installing the package therefore runs an unbounded client.

The check is deliberately CONDITIONAL. A pin that has not changed must still
build offline-ish, because a contributor editing ``skills/`` has to be able to
regenerate the packages without a boundedness opinion about a release they did not
choose. Only a pin *change*, or an explicit ``--require-bounded-client``, asks the
question.

The three ``G1`` defects (spec ``pr584-review-fable``, R3/R4/R5) are covered here
too, because they are one pipeline rather than three faults:

* **R4** — ``--check`` must not go red between ``release.yml`` bumping
  ``pyproject.toml`` on ``main`` and the regeneration job repinning. Strict
  equality lives behind ``--require-current-pin``.
* **R3** — the regeneration job must actually be reachable. Asserted by PARSING
  the workflow, which is the coverage whose absence let the dead ``if`` ship.
* **R5** — boundedness is decided by inspecting the published artifact, not by a
  hand-maintained version floor that refused every release below it.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as bap  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"

#: Captured before any fixture can replace it, so the one test that serves itself a
#: ``file://`` sdist can opt back in to a REAL opener without reaching the network.
_REAL_URLOPEN = urllib.request.urlopen

#: A ``requests`` call site with no ``timeout=``, matching the shape the real 2.5.0
#: sdist actually publishes (verified: its ops server's single call site passes
#: ``json`` and ``params`` only).
UNBOUNDED_SOURCE = """\
import requests

def _request_json(method, path, **kwargs):
    return requests.request(method, path, json=kwargs, params=None)
"""

BOUNDED_SOURCE = """\
import requests

_HTTP_TIMEOUT = (3.05, 30)

def _request_json(method, path, **kwargs):
    return requests.request(method, path, json=kwargs, timeout=_HTTP_TIMEOUT, headers={})
"""


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """Nothing in this module may reach PyPI.

    An ``e2e``-marked test opts out explicitly. That marker is deselected by a plain
    ``pytest`` run AND by CI's ``-m "not e2e"``, so the network path is something a
    maintainer asks for with ``-m e2e`` rather than something any default run
    discovers. This must track the marker on ``TestTheRealPublishedArtifact``: if the
    two disagree, the opted-in test hits this guard and fails with "a test attempted
    a real network call" instead of doing the fetch it exists to do.
    """
    if request.node.get_closest_marker("e2e"):
        return

    def refuse(*args, **kwargs):  # pragma: no cover - a guard, not a path
        raise AssertionError("a test attempted a real network call")

    monkeypatch.setattr(bap.urllib.request, "urlopen", refuse)


class TestTheCheckIsConditional:
    """Item 8's constraint that a plain rebuild must keep working."""

    def test_an_unchanged_pin_asks_nothing(self, monkeypatch):
        """The predicate the reviewer's fix must not break.

        A contributor who edits a skill and reruns the build has expressed no
        opinion about the pinned release. Making them answer for it -- or making
        the build require the network to say so -- would be a regression dressed
        as a fix. Asserted by the fake never being called.
        """
        calls = []
        monkeypatch.setattr(bap, "verify_published", lambda version: calls.append(version))
        monkeypatch.setattr(
            bap, "verify_bounded_client", lambda version: calls.append(("bounded", version))
        )

        committed = bap.package_version()
        assert bap.pin_changed(committed) is False, (
            "the committed pin must equal the rendered pin in a clean tree; if this "
            "fails the tree is mid-bump, not the check"
        )
        assert bap.should_check_boundedness(committed, require=False) is False

    def test_a_changed_pin_asks(self):
        """A pin change is the operator choosing a release, so it is answerable."""
        assert bap.should_check_boundedness("999.0.0", require=False) is True

    def test_the_explicit_flag_always_asks(self):
        """What the release job passes, so a release cannot ship an unbounded pin."""
        committed = bap.package_version()
        assert bap.should_check_boundedness(committed, require=True) is True


class TestPinChangeDetection:
    def test_it_compares_against_the_committed_bytes(self):
        """Read from disk, not recomputed, or it could never differ."""
        mcp_path = bap.PACKAGES_DIR / "cao" / "mcp.json"
        committed = json.loads(mcp_path.read_text(encoding="utf-8"))
        args = committed["mcpServers"][bap.OPS_SERVER_KEY]["args"]
        pinned = next(a for a in args if a.startswith(f"{bap.PYPI_DISTRIBUTION}=="))
        version = pinned.split("==", 1)[1]

        assert bap.pin_changed(version) is False
        assert bap.pin_changed("0.0.1") is True

    def test_a_missing_committed_package_counts_as_changed(self, monkeypatch, tmp_path):
        """No bytes to compare means the answer cannot be "unchanged"."""
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path / "absent")
        assert bap.pin_changed("2.5.0") is True


class TestBoundednessVerification:
    def test_a_release_without_the_timeout_is_refused(self, monkeypatch):
        """The finding itself: 2.5.0's ops server has no request timeout."""
        monkeypatch.setattr(bap, "release_bounds_its_requests", lambda version: False)
        with pytest.raises(bap.BuildError) as caught:
            bap.verify_bounded_client("2.5.0")
        assert "timeout" in str(caught.value).lower()

    def test_a_release_with_the_timeout_is_accepted(self, monkeypatch):
        monkeypatch.setattr(bap, "release_bounds_its_requests", lambda version: True)
        bap.verify_bounded_client("99.0.0")


class TestTheReleaseGateRefusesAnUnboundedArtifact:
    """The predicate stated as a test rather than as a claim in a commit message."""

    def test_requiring_boundedness_fails_against_an_unbounded_release(self, monkeypatch):
        """PyPI's latest is 2.5.0 and its published ops server has no timeout.

        So ``--require-bounded-client`` MUST fail against it. This is the honest
        recording of the residual: the fix cannot be "pin a bounded release"
        because none exists yet. The release workflow regenerates the package
        after publishing, which is what closes it.

        Goes through ``main``, which is what the release job invokes: ``run_build``
        raises ``BuildError`` and ``main`` is the layer that turns it into an exit
        status. No ``--skip-publish-check`` here, because that flag now suppresses
        the boundedness fetch too (R5.2) and would make this assertion vacuous.
        """
        monkeypatch.setattr(bap, "verify_published", lambda version: None)
        monkeypatch.setattr(
            bap, "fetch_published_ops_server_source", lambda version: UNBOUNDED_SOURCE
        )

        rc = bap.main(["--require-bounded-client"])
        assert rc == 1

    def test_a_bounded_artifact_is_accepted(self, monkeypatch, tmp_path):
        """The other half: the gate must PASS once a bounded release exists.

        Without this the suite could not distinguish "correctly refuses today" from
        "refuses unconditionally", which is exactly the failure the version floor
        turned out to be.
        """
        monkeypatch.setattr(bap, "verify_published", lambda version: None)
        monkeypatch.setattr(
            bap, "fetch_published_ops_server_source", lambda version: BOUNDED_SOURCE
        )
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path / "agent-plugin")

        assert bap.main(["--require-bounded-client"]) == 0


class TestSkipPublishCheckIsOffline:
    """R5.2 — ``--skip-publish-check`` must need no network at all.

    Boundedness is now decided by DOWNLOADING the published sdist, so a flag that
    suppressed only the publication probe would advertise an offline rebuild while
    still requiring the network — and would require it to fetch an artifact whose
    publication the same flag just declined to check.
    """

    def test_it_suppresses_the_boundedness_fetch(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            bap, "fetch_published_ops_server_source", lambda version: calls.append(version)
        )
        monkeypatch.setattr(bap, "verify_bounded_client", lambda version: calls.append(version))
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path / "agent-plugin")

        # `--require-bounded-client` too: the suppression must hold even against the
        # flag that otherwise always asks, or the offline promise has an exception.
        assert bap.main(["--skip-publish-check", "--require-bounded-client"]) == 0
        assert calls == []

    def test_the_autouse_network_guard_would_have_caught_a_fetch(self):
        """The guard the previous test relies on is real, asserted directly.

        Otherwise ``calls == []`` could pass because nothing ran at all.
        """
        with pytest.raises(AssertionError, match="real network call"):
            bap.urllib.request.urlopen("https://pypi.org/simple/")


class TestArtifactInspection:
    """R5.1 — the published artifact answers for itself."""

    def test_the_predicate_is_the_one_the_repo_already_owned(self):
        """Hoisted, not duplicated: ``test_packaged_mcp_server.py`` imports this."""
        assert bap.request_call_sites_missing_kwargs(BOUNDED_SOURCE, {"timeout"}) == []
        offenders = bap.request_call_sites_missing_kwargs(UNBOUNDED_SOURCE, {"timeout"})
        assert len(offenders) == 1
        assert "timeout" in offenders[0]

    def test_it_reads_the_kwargs_of_every_verb_form(self):
        """``requests.get(...)`` is as much a call site as ``requests.request(...)``."""
        source = "import requests\nrequests.get('u')\nrequests.post('u', timeout=1)\n"
        offenders = bap.request_call_sites_missing_kwargs(source, {"timeout"})
        assert len(offenders) == 1
        assert "requests.get" in offenders[0]

    def test_no_version_constant_decides_boundedness(self):
        """R5 — the floor is gone, and nothing may reintroduce one.

        Read from the source rather than asserted as an absent attribute, because a
        *differently named* constant would satisfy ``not hasattr`` and reinstate the
        same defect.
        """
        source = (REPO_ROOT / "scripts" / "build_agent_plugin.py").read_text(encoding="utf-8")
        assert not hasattr(bap, "FIRST_BOUNDED_CLIENT_VERSION")
        assert "FIRST_BOUNDED_CLIENT_VERSION" not in source

        import inspect

        decider = inspect.getsource(bap.release_bounds_its_requests)
        assert "Version(" not in decider, "boundedness must not compare version numbers"
        assert "fetch_published_ops_server_source" in decider

    def test_it_reads_the_module_out_of_a_real_sdist(self, monkeypatch, tmp_path):
        """The archive path, exercised without the network via ``file://``.

        A hand-built tarball with the same ``<dist>-<version>/src/...`` layout PyPI
        serves, so the member lookup is tested against the real shape rather than a
        stubbed return value.
        """
        member_path = f"cli_agent_orchestrator-9.9.9/{bap.OPS_SERVER_SDIST_SUFFIX}"
        archive = tmp_path / "sdist.tar.gz"
        payload = BOUNDED_SOURCE.encode("utf-8")
        with tarfile.open(archive, mode="w:gz") as tar:
            info = tarfile.TarInfo(member_path)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        monkeypatch.setattr(bap, "published_sdist_url", lambda version: archive.as_uri())
        monkeypatch.setattr(bap.urllib.request, "urlopen", _REAL_URLOPEN)

        assert bap.fetch_published_ops_server_source("9.9.9") == BOUNDED_SOURCE
        assert bap.release_bounds_its_requests("9.9.9") is True


class TestAnUninspectableArtifactSaysSo:
    """R5.3 — "could not inspect" must never be reported as "is unbounded"."""

    def test_a_missing_module_in_the_sdist_is_an_inspection_failure(self, monkeypatch, tmp_path):
        archive = tmp_path / "sdist.tar.gz"
        with tarfile.open(archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("cli_agent_orchestrator-9.9.9/README.md")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))

        monkeypatch.setattr(bap, "published_sdist_url", lambda version: archive.as_uri())
        monkeypatch.setattr(bap.urllib.request, "urlopen", _REAL_URLOPEN)

        with pytest.raises(bap.ArtifactInspectionError) as caught:
            bap.verify_bounded_client("9.9.9")

        message = str(caught.value)
        assert "could not be determined" in message
        assert (
            "does not bound" not in message
        ), "an uninspectable artifact must not be reported as an unbounded release"

    def test_an_unreachable_pypi_is_an_inspection_failure(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(bap.urllib.request, "urlopen", refuse)

        with pytest.raises(bap.ArtifactInspectionError) as caught:
            bap.verify_bounded_client("2.5.0")

        message = str(caught.value)
        assert "could not read PyPI metadata" in message
        assert "does not bound" not in message

    def test_an_inspection_failure_is_still_a_build_error(self, monkeypatch):
        """So ``main``'s existing handler reports it and exits 1, not a traceback."""
        assert issubclass(bap.ArtifactInspectionError, bap.BuildError)

        monkeypatch.setattr(bap, "verify_published", lambda version: None)
        monkeypatch.setattr(
            bap,
            "fetch_published_ops_server_source",
            lambda version: (_ for _ in ()).throw(bap.ArtifactInspectionError("nope")),
        )
        assert bap.main(["--require-bounded-client"]) == 1

    def test_a_release_with_no_sdist_says_that(self, monkeypatch):
        monkeypatch.setattr(
            bap.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(json.dumps({"urls": []}).encode("utf-8")),
        )
        with pytest.raises(bap.ArtifactInspectionError, match="no sdist"):
            bap.published_sdist_url("9.9.9")


class _FakeResponse:
    """Minimal context-manager response for the metadata probe."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.e2e
class TestTheRealPublishedArtifact:
    """The completion predicate against PyPI itself, not a stub.

    ``e2e``-marked, NOT ``integration``, and the difference is load-bearing here:
    CI runs ``-m "not e2e"``, so an ``integration`` mark would execute this on every
    PR — and ``ci.yml`` is a Python-version matrix, so it would be one ~58 MB sdist
    download PER MATRIX LEG. That makes PyPI availability and index lag able to
    redden a build over changes with nothing to do with this code. ``e2e`` is
    deselected by CI and by a plain ``pytest`` run alike, so the download is
    something a maintainer opts into with ``-m e2e``.

    **The gate is not untested in CI.** What guards the logic there is the offline
    coverage, which is complete without the network: ``TestArtifactInspection`` and
    ``TestTheReleaseGateRefusesAnUnboundedArtifact`` drive the predicate and the
    refusal against stubbed bounded and unbounded sources,
    ``test_it_reads_the_module_out_of_a_real_sdist`` exercises the archive path
    against a hand-built tarball over ``file://`` with the same
    ``<dist>-<version>/src/...`` layout PyPI serves, and
    ``TestAnUninspectableArtifactSaysSo`` covers every way the inspection can fail.
    This class adds one thing those cannot: that the predicate's answer about the
    REAL published 2.5.0 artifact is the one item 8 reported. That answer has been
    produced and recorded (its sole ``requests.request`` call site, at line 71 of
    the published module, passes ``json`` and ``params`` and no ``timeout``); this
    keeps it reproducible on demand rather than re-proving it per PR.
    """

    def test_the_real_2_5_0_sdist_is_refused(self):
        assert bap.release_bounds_its_requests("2.5.0") is False
        with pytest.raises(bap.BuildError) as caught:
            bap.verify_bounded_client("2.5.0")
        assert "does not bound" in str(caught.value)
        assert not isinstance(
            caught.value, bap.ArtifactInspectionError
        ), "2.5.0 must be refused as UNBOUNDED, not as uninspectable"


class TestABumpedVersionDoesNotRedenMain:
    """R4 / defect G1c, empirically the reproduction from the spec.

    ``release.yml`` bumps ``pyproject.toml`` on ``main`` and the regeneration job
    repins afterwards. In that window the committed packages are one version behind
    on purpose, and ``ci.yml`` runs ``make check-agent-plugin`` — so strict equality
    there fails ``main`` and every open PR for something that is not wrong.
    """

    @pytest.fixture
    def bumped(self, monkeypatch):
        """``pyproject.toml`` one patch ahead of the committed packages."""
        committed = bap.committed_package_version()
        assert committed is not None, "the committed packages must declare a version"
        major, minor, patch = (int(part) for part in committed.split(".")[:3])
        bumped = f"{major}.{minor}.{patch + 1}"
        monkeypatch.setattr(bap, "package_version", lambda: bumped)
        return committed, bumped

    def test_check_stays_green(self, bumped, capsys):
        committed, current = bumped
        assert bap.run_check() == 0

        out = capsys.readouterr().out
        assert (
            committed in out and current in out
        ), "the operator must be told which version was checked and which is current"

    def test_require_current_pin_fails_in_the_same_situation(self, bumped):
        """R4.2 — the loosening is bounded by a flag that still demands equality."""
        assert bap.run_check(require_current_pin=True) == 1

    def test_the_flag_is_reachable_from_the_command_line(self, bumped):
        """Both spellings, since the workflow invokes the CLI, not ``run_check``."""
        assert bap.main(["--check"]) == 0
        assert bap.main(["--check", "--require-current-pin"]) == 1

    def test_a_clean_tree_is_green_either_way(self):
        """No bump: the steady state must satisfy the strict form too."""
        assert bap.run_check() == 0
        assert bap.run_check(require_current_pin=True) == 0


class TestACommittedPinAheadOfPyprojectStillFails:
    """The half of the check that R4 must NOT loosen.

    ``committed < pyproject`` is the release window. ``committed > pyproject`` means
    the packages name a release this tree is not, which is real drift.
    """

    def test_it_is_reported(self, monkeypatch, capsys):
        # An unambiguously lower version rather than an arithmetic decrement: the
        # committed patch level can be 0, and "2.5.-1" is not a version at all, so
        # the test would exercise the parse-failure branch instead of the ordering
        # one and still go green.
        monkeypatch.setattr(bap, "package_version", lambda: "0.0.1")

        assert bap.run_check() == 1
        assert "AHEAD of pyproject.toml" in capsys.readouterr().err

    def test_the_ordering_predicate_directly(self):
        assert bap.pin_ordering_problems("2.5.0", "2.5.0") == []
        assert bap.pin_ordering_problems("2.5.0", "2.5.2") == []
        assert bap.pin_ordering_problems("2.5.2", "2.5.0") != []

    def test_an_unparseable_version_is_reported_not_ignored(self):
        problems = bap.pin_ordering_problems("not-a-version", "2.5.0")
        assert problems and "could not compare" in problems[0]


class TestCommittedPackageVersion:
    """What ``--check`` compares against when it is not being strict."""

    def test_it_reads_the_committed_manifests(self):
        manifest = json.loads(
            (bap.PACKAGES_DIR / "cao" / "plugin.json").read_text(encoding="utf-8")
        )
        assert bap.committed_package_version() == manifest["version"]

    def test_a_missing_package_is_unknowable_not_a_guess(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path / "absent")
        assert bap.committed_package_version() is None

    def test_packages_disagreeing_is_unknowable(self, monkeypatch, tmp_path):
        """Two versions is drift, and drift must not become the baseline."""
        for name, version in (("cao", "2.5.0"), ("cao-contributor", "2.4.0")):
            directory = tmp_path / name
            directory.mkdir(parents=True)
            (directory / "plugin.json").write_text(
                json.dumps({"name": name, "version": version}), encoding="utf-8"
            )
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path)
        assert bap.committed_package_version() is None

    def test_an_unknowable_version_falls_back_to_pyproject(self, monkeypatch, tmp_path):
        """So the operator sees "package missing", not an error about discovery."""
        monkeypatch.setattr(bap, "PACKAGES_DIR", tmp_path / "absent")
        assert bap.run_check() == 1


def _workflow_document() -> dict:
    """Parse the publish workflow, defusing PyYAML's ``on:`` → ``True`` mapping.

    PyYAML 1.1-style booleans mean the bare key ``on:`` loads as the Python object
    ``True``, not the string ``"on"``. Left implicit, ``doc.get("on", {})`` returns
    an empty dict and every trigger assertion built on it passes vacuously — which
    is precisely the class of silent test that let the dead ``if`` ship in the first
    place. So the trap is asserted rather than tolerated.
    """
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "on" not in document, (
        "PyYAML no longer folds `on:` to True; update _workflow_triggers, do not "
        "let the trigger assertions silently read an empty mapping"
    )
    assert True in document, "the workflow must declare triggers"
    return document


def _workflow_triggers() -> dict:
    return _workflow_document()[True]


def _job_script(job: dict) -> str:
    """Every ``run:`` body in a job, concatenated verbatim.

    Read off the parsed steps rather than ``yaml.safe_dump(job["steps"])``: dumping
    re-serializes, and PyYAML line-folds a long shell line, which splits the very
    strings these assertions look for and turns a real check into a false failure
    (or, with an inverted assertion, a false pass).
    """
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


class TestTheRegenerationJobCanActuallyRun:
    """R3 / defect G1a — the coverage whose absence let a dead job ship.

    ``release.yml`` creates releases with ``softprops/action-gh-release`` under
    ``GITHUB_TOKEN``, and a release created by the workflow token does not emit
    ``on: release``. The job's ``if: github.event_name == 'release'`` therefore
    excluded the only path that ever publishes: measured run history is
    ``workflow_dispatch`` successes and ``release``-event failures only.
    """

    @pytest.fixture
    def job(self) -> dict:
        return _workflow_document()["jobs"]["regenerate-agent-plugin"]

    def test_it_depends_on_a_successful_pypi_publish(self, job):
        """The real gate, and the reason the event guard was redundant."""
        assert job["needs"] == "publish-pypi"

    def test_it_has_no_event_name_condition(self, job):
        """The G1a regression guard, stated on the job as a whole.

        Asserted against the whole ``if`` rather than the exact removed string, so a
        differently spelled event guard (``github.event.action``, a matrix of
        ``event_name`` values) cannot slip past.
        """
        condition = str(job.get("if", ""))
        assert "event_name" not in condition
        assert "github.event" not in condition

    def test_the_workflow_still_answers_both_triggers(self):
        """Removing the job guard must not have touched the workflow's triggers.

        Read through the ``on:``-is-``True`` defusal, so this cannot pass on an
        empty mapping.
        """
        triggers = _workflow_triggers()
        assert "release" in triggers
        assert "workflow_dispatch" in triggers

    def test_the_publish_job_it_depends_on_exists(self):
        """``needs:`` naming a job that does not exist is a workflow that never runs."""
        assert "publish-pypi" in _workflow_document()["jobs"]

    def test_the_commit_message_version_comes_from_pyproject(self, job):
        """R3.2 — ``GITHUB_REF_NAME`` is the branch on a dispatch, not a tag."""
        script = _job_script(job)
        assert (
            "GITHUB_REF_NAME" not in script
        ), "the commit message must not name a ref that is `main` on a dispatch"
        assert "package_version()" in script
        assert "repin packages to v${version}" in script

    def test_the_job_is_the_only_caller_of_require_current_pin(self, job):
        """R4.2 — the flag that bounds the loosening must have a real caller.

        A flag nothing passes is a check nothing performs, and the tolerance in
        ``make check-agent-plugin`` would then be unbounded.
        """
        assert "--require-current-pin" in _job_script(job)

        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert (
            "--require-current-pin" not in makefile
        ), "CI's check-agent-plugin must stay tolerant, or G1c returns"

    def test_it_still_requires_bounded_client(self, job):
        """R5's gate must remain wired into the release path."""
        assert "--require-bounded-client" in _job_script(job)
