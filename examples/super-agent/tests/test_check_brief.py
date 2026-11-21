"""bin/check_brief.py: a filled brief or message passes; any placeholder left is refused."""
import re

import pytest

from conftest import KIT, base_env, run

TEMPLATES = sorted((KIT / "templates").glob("*.md"))


def blocks(path):
    """The worker-facing parts of a template: its fenced blocks (``` or ~~~)."""
    return re.findall(r"^(```|~~~)[^\n]*\n(.*?)^\1$", path.read_text(), re.S | re.M)


def fill(text):
    return re.sub(r"(?<![%$\w{])\{([a-z][a-z0-9_]*)\}(?!\})", lambda m: f"<{m.group(1)}-value>", text)


def check(tmp_path, *texts):
    files = []
    for i, t in enumerate(texts):
        p = tmp_path / f"brief-{i}.md"
        p.write_text(t)
        files.append(p)
    return run("check_brief.py", *files, env=base_env(tmp_path))


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_every_filled_template_block_passes(tpl, tmp_path):
    found = blocks(tpl)
    assert found, tpl.name
    r = check(tmp_path, *(fill(body) for _, body in found))
    assert r.returncode == 0, r.stdout
    assert all(line.startswith("OK ") for line in r.stdout.splitlines())


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_one_kit_placeholder_left_is_refused(tpl, tmp_path):
    body = "\n".join(b for _, b in blocks(tpl))
    names = set(re.findall(r"(?<![%$\w{])\{(kit|repo|repo_url|login|cao_api|cao_source)\}", body))
    assert names, tpl.name  # every template uses at least one of the kit's own placeholders
    for name in sorted(names):
        text = fill(body.replace("{" + name + "}", "\0")).replace("\0", "{" + name + "}")
        r = check(tmp_path, text)
        assert r.returncode == 1 and r.stdout.startswith("LEFT ") and "{" + name + "}" in r.stdout


@pytest.mark.parametrize("text, left", [
    ("Creator: terminal {creator_id}.", "{creator_id}"),
    ("Kit: {{KIT}}.", "{{KIT}}"),
    ("python3 KIT/bin/holds.py body", "KIT/"),
    ("cd PB/state", "PB/"),
])
def test_leftovers_are_named(tmp_path, text, left):
    r = check(tmp_path, text)
    assert r.returncode == 1 and left in r.stdout


def test_shell_curl_and_json_braces_are_not_placeholders(tmp_path):
    text = ("curl -w '%{url_effective} %{http_code}' x; echo ${HOME} $K; "
            '{"days": {}, "prs": {}}; jq \'{a: .b}\'; /srv/wp/super-agent/bin/x.py; MY_KIT/x\n')
    r = check(tmp_path, text)
    assert r.returncode == 0, r.stdout


def test_unreadable_file_exits_2(tmp_path):
    r = run("check_brief.py", tmp_path / "missing.md", env=base_env(tmp_path))
    assert r.returncode == 2 and r.stdout.startswith("UNREADABLE")


# --message: a launch or assign message is refused when a shell could run part of it.

MESSAGE_FIX = "put the brief in a <<'EOF' file and point the message at it"


def check_message(tmp_path, text):
    p = tmp_path / "message.txt"
    p.write_text(text)
    return run("check_brief.py", "--message", p, env=base_env(tmp_path)), p


def test_message_with_backtick_is_refused_with_line_and_fix(tmp_path):
    r, p = check_message(tmp_path, "Read the brief.\nthen run `git push` now\n")
    assert r.returncode == 1
    assert r.stdout == f"LEFT {p}: line 2: ` in a message passed on a shell command line, where a shell runs it; {MESSAGE_FIX}\n"


def test_message_with_command_substitution_is_refused(tmp_path):
    r, p = check_message(tmp_path, "id is $(whoami)\n")
    assert r.returncode == 1
    assert r.stdout == f"LEFT {p}: line 1: $( in a message passed on a shell command line, where a shell runs it; {MESSAGE_FIX}\n"


def test_plain_message_passes_in_message_mode(tmp_path):
    r, p = check_message(tmp_path, "Your brief is /srv/wp/work/pr-9/01-fix.md. Read it and follow it. ${HOME} (x) $K\n")
    assert r.returncode == 0 and r.stdout == f"OK {p}\n"


def test_brief_file_may_hold_backticks_without_message_mode(tmp_path):
    r = check(tmp_path, "Run `git status` and $(pwd) later.\n")
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_every_filled_template_block_with_a_backtick_is_named_not_crashed(tpl, tmp_path):
    """Template example blocks are briefs, so --message may refuse them; it must refuse by line, never crash."""
    for _, body in blocks(tpl):
        r, _ = check_message(tmp_path, fill(body))
        assert r.returncode in (0, 1), r.stdout
        assert r.returncode == 0 or "in a message" in r.stdout


# --brief: every brief must carry the foreground sentence.

FOREGROUND_FIX = ("add the line 'Foreground only. Never end your turn while a job you started is still running, "
                  "and never wait for a wakeup.'")


def check_brief_file(tmp_path, text):
    p = tmp_path / "brief.md"
    p.write_text(text)
    return run("check_brief.py", "--brief", p, env=base_env(tmp_path)), p


def test_brief_without_the_foreground_sentence_is_refused_with_its_name(tmp_path):
    r, p = check_brief_file(tmp_path, "# Task\nFix it.\nRun the tests in the background and report later.\n")
    assert r.returncode == 1
    assert r.stdout == f"LEFT {p}: the foreground sentence is missing; {FOREGROUND_FIX}\n"


@pytest.mark.parametrize("line", [
    "- **Foreground only.** Never end your turn while a job you started is still running, and never\n",
    "Stay in the foreground and report back with send_message.\n",
    "You run in the foreground and leave no job in the background.\n",
    "foreground only\n",
])
def test_brief_with_the_foreground_sentence_passes(tmp_path, line):
    r, p = check_brief_file(tmp_path, "# Task\n" + line)
    assert r.returncode == 0 and r.stdout == f"OK {p}\n"


def test_a_denied_foreground_does_not_count(tmp_path):
    r, _ = check_brief_file(tmp_path, "Don't stay in the foreground; use the background.\n")
    assert r.returncode == 1 and "foreground sentence is missing" in r.stdout


def test_brief_check_still_names_placeholders_and_the_missing_sentence(tmp_path):
    r, p = check_brief_file(tmp_path, "Fix {pr}.\n")
    assert r.returncode == 1
    assert r.stdout.splitlines()[0] == f"LEFT {p}: {{pr}}"
    assert "foreground sentence is missing" in r.stdout.splitlines()[1]


def test_brief_and_message_modes_cannot_be_mixed(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("Foreground only.\n")
    r = run("check_brief.py", "--brief", "--message", p, env=base_env(tmp_path))
    assert r.returncode == 2


@pytest.mark.parametrize("tpl", [t for t in TEMPLATES if t.name in ("fixer.md", "main-merger.md", "prepush-reviewer.md")],
                         ids=lambda p: p.name)
def test_the_templates_own_brief_skeleton_carries_the_foreground_sentence(tpl, tmp_path):
    body = fill([b for _, b in blocks(tpl) if b.lstrip().startswith("#")][0])
    r, _ = check_brief_file(tmp_path, body)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("tpl", [t for t in TEMPLATES if t.name in ("publisher.md", "automerge-armer.md")], ids=lambda p: p.name)
def test_the_inline_briefs_in_publisher_and_armer_templates_carry_it(tpl, tmp_path):
    for _, body in blocks(tpl):
        if body.lstrip().startswith(("Publish for", "Update the branch", "Arm squash")):
            r, _ = check_brief_file(tmp_path, fill(body))
            assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_template_instruction_runs_message_only_for_shell_passed_messages(tpl):
    line = next(l for l in tpl.read_text().splitlines() if "Before you send anything, run" in l)
    assert "check_brief.py --brief <each brief file>" in line
    assert "only for a message you will pass through a shell" in line
    assert "MCP assign or handoff tools never pass through a shell and need no `--message` check" in line


@pytest.mark.parametrize("tpl", [t for t in TEMPLATES if t.name in ("publisher.md", "automerge-armer.md")], ids=lambda p: p.name)
def test_publisher_and_armer_assign_messages_pass_the_documented_instruction(tpl, tmp_path):
    """They go out through the MCP assign tool, so the instruction asks for --brief only; --brief must pass them."""
    msgs = [b for _, b in blocks(tpl) if b.lstrip().startswith(("Publish for", "Update the branch", "Arm squash"))]
    assert msgs, tpl.name
    for body in msgs:
        assert "$(" in body  # these really hold $(, which is fine off the shell command line
        r, _ = check_brief_file(tmp_path, fill(body))
        assert r.returncode == 0, r.stdout
