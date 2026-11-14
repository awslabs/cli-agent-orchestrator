"""A leak grep over the whole sample: no local paths, account numbers, real ids,
token-shaped strings, or US-only model ids in a Sydney sample."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from remote_ec2_support import (
    FAKE_INSTANCE_ID,
    FAKE_VOLUME_ID,
    OTHER_INSTANCE_ID,
    OTHER_VOLUME_ID,
    SAMPLE_DIR,
)

THIS_FILE = Path(__file__).resolve()
# Placeholder or test-fixture user names that may follow /Users/ or /home/.
ALLOWED_USERS = r"(?:<you>|<user>|<box-user>|boxuser|@BOX_)"

PATTERNS = {
    "a real /Users/<name> path": re.compile(rf"/Users/(?!{ALLOWED_USERS})[A-Za-z0-9]"),
    "a real /home/<name> path": re.compile(rf"/home/(?!{ALLOWED_USERS})[A-Za-z0-9]"),
    "a 12-digit number (account id?)": re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])"),
    "a real-looking instance id": re.compile(r"\bi-[0-9a-f]{8,17}\b"),
    "a real-looking AMI, volume, subnet or group id": re.compile(
        r"\b(?:ami|vol|subnet|sg|vpc|snap)-[0-9a-f]{8,17}\b"
    ),
    "a US-only model id": re.compile("us" + r"\.anthropic\."),
    "an AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "a Telegram bot token": re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"),
    "a GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "a personal email address": re.compile(
        r"[A-Za-z0-9._%+-]+@(?:gmail|yahoo|hotmail|outlook|icloud|proton)\.[a-z]{2,}", re.I
    ),
}


def sample_files() -> list[Path]:
    return sorted(
        p
        for p in SAMPLE_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.resolve() != THIS_FILE
    )


def test_the_scan_sees_the_sample() -> None:
    names = {p.name for p in sample_files()}
    assert {"README.md", "bootstrap.sh", "cao-server.service", "config.snippet"} <= names


@pytest.mark.parametrize("label", sorted(PATTERNS))
def test_no_leaks(label: str) -> None:
    pattern = PATTERNS[label]
    hits = []
    for path in sample_files():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                if match.group(0) in {
                    FAKE_INSTANCE_ID,
                    FAKE_VOLUME_ID,
                    OTHER_INSTANCE_ID,
                    OTHER_VOLUME_ID,
                }:
                    continue
                hits.append(f"{path.relative_to(SAMPLE_DIR)}:{number}: {match.group(0)}")
    assert hits == [], f"{label}:\n" + "\n".join(hits)


MODEL_CONFIGS = [
    "config/cao-settings.json.snippet",
    "config/claude-settings.box.json.example",
    "iam/instance-role-policy.json.example",
]


@pytest.mark.parametrize("rel", MODEL_CONFIGS)
def test_sydney_configs_use_au_or_global_ids_only(rel: str) -> None:
    text = (SAMPLE_DIR / rel).read_text(encoding="utf-8")
    prefixes = set(re.findall(r"\b([a-z]+)\.anthropic\.claude-", text))
    assert prefixes, f"{rel} names no model ids"
    assert prefixes <= {"au", "global"}, prefixes


def test_claude_settings_use_the_instance_role_not_a_profile() -> None:
    text = (SAMPLE_DIR / "config" / "claude-settings.box.json.example").read_text(encoding="utf-8")
    assert '"AWS_REGION": "ap-southeast-2"' in text
    assert "AWS_PROFILE" not in text
