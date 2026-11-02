"""Self-tests for test/security/redos_analysis.py, the load-independent ReDoS checks.

The provider guards are only as good as this helper, so it gets its own tests:
the structure check must flag the textbook superlinear shapes and pass their
standard repairs, and the step-counting matcher must agree with ``re`` while
its counts grow the way CPython's backtracking does.
"""

import random
import re
from test.security.redos_analysis import (
    StepBudgetExceeded,
    count_steps,
    first_superlinear,
    input_shapes,
    nonlinear_shapes,
    pattern_probe,
    structural_issues,
)

import pytest


@pytest.mark.parametrize(
    "pattern,mode",
    [
        (r"(a+)+b", "match"),  # nested repeats, no fence
        (r"(?:a|ab)*c", "match"),  # alternation under a repeat, shared first character
        (r"\s*\s*x", "match"),  # two repeats over the same characters
        (r"[^\n]*-[^\n]*x", "match"),  # a separator both repeats can consume
        (r"[*][^\n]*x", "search"),  # unanchored: every '*' rescans the line
        (r"(?m)^\s*x", "search"),  # line-anchored, but \s carries into the next line
        (r"(?:\x1b\[[0-9;]*m)*x", "search"),  # fenced, but every ESC starts a rescan
    ],
)
def test_superlinear_shapes_are_flagged(pattern, mode):
    assert structural_issues(pattern, mode)


@pytest.mark.parametrize(
    "pattern,mode",
    [
        (r"(?:a+)++b", "match"),  # possessive outer repeat
        (r"(?:\x1b\[[0-9;]*m)*x", "match"),  # nested, fenced by ESC [ ... m
        (r"\s*,\s*x", "match"),  # a separator the first repeat cannot consume
        (r"(?m)^[^*\n]*+[*][^\n]*x", "search"),  # line-anchored, one attempt per line
        (r"(?m)^[^\S\n]*x", "search"),  # horizontal whitespace stops at the newline
        (r"(?<!\d)\d+ of", "search"),  # starts only where a digit run starts
        (r"[*][^\n]*x", "match"),  # one attempt: no rescans in match mode
    ],
)
def test_linear_shapes_pass(pattern, mode):
    assert structural_issues(pattern, mode) == []


@pytest.mark.parametrize(
    "pattern,flags",
    [
        (r"(?m)^[ \t]*(?:\x1b\[[0-9;]*m)*+[⏺●](?!\s*x)\s+", 0),
        (r"(?<=…)[^\n…]*+", 0),
        (r"\bfor\b|\d{1,3}%|[^)●]{0,80}●", re.IGNORECASE),
        (r"a*?b|(?:ab)*+c|x{2,5}?y", 0),
        (r"(?s)A.*?\[[^y]*+y", 0),
    ],
)
def test_matcher_agrees_with_re(pattern, flags):
    rng = random.Random(pattern)
    alphabet = "abcxy \t\n[]()…●⏺\x1b;0m19%A"
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 24)))
        for mode in ("search", "finditer", "match", "fullmatch"):
            count_steps(pattern, text, mode, flags)  # raises if a span disagrees


def test_budget_stops_runaway_backtracking():
    with pytest.raises(StepBudgetExceeded):
        count_steps(r"(a+)+b", "a" * 40, "match", budget=10_000)


def test_growth_tells_quadratic_from_linear():
    quadratic = r"[*][^\n]*x"
    linear = r"(?m)^[^*\n]*+[*][^\n]*x"
    shapes = input_shapes(quadratic)
    assert first_superlinear(pattern_probe(quadratic, "search"), shapes) is not None
    assert nonlinear_shapes(pattern_probe(linear, "search"), shapes) == []
