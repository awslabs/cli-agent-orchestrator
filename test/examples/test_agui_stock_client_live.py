"""Regression tests for the stock-client live demo launcher."""

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "examples" / "ag-ui" / "ag-ui-stock-client-live" / "run.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def test_server_startup_allows_more_than_twenty_seconds(tmp_path: Path) -> None:
    """A slow but healthy server must outlive the old 40-poll startup budget."""
    scratch_root = tmp_path / "repo"
    scratch_example = scratch_root / "examples" / "ag-ui" / "ag-ui-stock-client-live"
    scratch_example.mkdir(parents=True)
    scratch_script = scratch_example / "run.sh"
    shutil.copy2(RUN_SCRIPT, scratch_script)
    (scratch_example / "node_modules").mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_count = tmp_path / "curl-count"
    _write_executable(
        fake_bin / "cao-server",
        "#!/bin/sh\nexec /bin/sleep 300\n",
    )
    for command in ("curl", "node", "sleep", "uv"):
        _write_executable(fake_bin / command, "#!/bin/sh\nexit 127\n")

    home = tmp_path / "home"
    temp = tmp_path / "tmp"
    home.mkdir()
    temp.mkdir()
    env = {
        "FAKE_CURL_COUNT": str(curl_count),
        "HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
        "TMPDIR": str(temp),
        "BASH_FUNC_curl%%": """() {
            count=0
            if [ -f "$FAKE_CURL_COUNT" ]; then
                IFS= read -r count <"$FAKE_CURL_COUNT"
            fi
            count=$((count + 1))
            printf '%s\\n' "$count" >"$FAKE_CURL_COUNT"
            [ "$count" -ge 42 ]
        }""",
        "BASH_FUNC_node%%": "() { :; }",
        "BASH_FUNC_sleep%%": "() { :; }",
        "BASH_FUNC_uv%%": "() { :; }",
    }
    result = subprocess.run(
        [str(scratch_script)],
        cwd=scratch_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert int(curl_count.read_text()) >= 42
