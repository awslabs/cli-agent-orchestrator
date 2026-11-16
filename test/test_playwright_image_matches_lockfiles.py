"""The Playwright image tag in ci.yml must match the lockfile of each job that uses it."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = re.compile(
    r"mcr\.microsoft\.com/playwright:v(\d+\.\d+\.\d+)-[a-z]+(?:@sha256:[0-9a-f]{64})?"
)


def test_playwright_image_matches_each_job_lockfile() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    checked = 0
    for name, job in jobs.items():
        container = job.get("container")
        image = container if isinstance(container, str) else (container or {}).get("image", "")
        match = IMAGE.fullmatch(image or "")
        if not match:
            continue
        default_dir = job.get("defaults", {}).get("run", {}).get("working-directory", ".")
        lock_dirs = {
            s.get("working-directory", default_dir)
            for s in job["steps"]
            if "npm ci" in s.get("run", "")
        }
        assert lock_dirs, f"{name}: no `npm ci` step"
        for lock_dir in lock_dirs:
            lock = json.loads((ROOT / lock_dir / "package-lock.json").read_text())
            locked = lock["packages"]["node_modules/playwright-core"]["version"]
            assert locked == match.group(1), (
                f"{name}: the image is v{match.group(1)}, but {lock_dir} locks playwright-core {locked}. "
                "Change the image tag in ci.yml to the locked version."
            )
        checked += 1
    assert checked == 5, f"expected 5 jobs on the Playwright image, found {checked}"
