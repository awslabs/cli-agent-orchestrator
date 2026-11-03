"""Self-tests for test/security/redos_analysis.py, the load-independent ReDoS checks.

The provider guards are only as good as this helper, so it gets its own tests:
the structure check must flag the textbook superlinear shapes and pass their
standard repairs, and the step-counting matcher must agree with ``re`` while
its counts grow the way CPython's backtracking does.
"""

import random
import re
from test.security.redos_analysis import (
    BOUNDED_MAX,
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
        # Bounded repeats are no safer when their body can run twice:
        (r"(?:\s*){2}x", "match"),  # \s*\s*x written as a bounded repeat
        (r"(?:[^\S\n]*){2}x", "match"),
        (r"(?:a|a){0,200}b", "match"),  # exponential: ambiguous alternation, bounded
        (r"(?:\w+\s?){1,20}:", "match"),  # exponential: nested, bounded outer repeat
        # ...and a bounded inner repeat varies as much as an unbounded one:
        (r"(?:\w{1,20}\s?){1,20}:", "match"),
        (r"(?:\w{1,20}\s?)+:", "match"),
        (r"(?:a{0,200})*b", "match"),
        (r"(?:\S+\s*)*x", "match"),  # an optional delimiter fences nothing
        # A backtracking repeat before a lookahead that re-scans what it gives back:
        (r"x(?:ab)*(?!(?:ab)*c)\s", "match"),
        (r"[⏺●](?:\x1b\[[0-9;]*m)*(?![ \t]*+(?:\x1b\[[0-9;]*m)*+\w)\s+", "match"),
        # Unanchored, a possessive skip still restarts at every position:
        (r"(?:[^\n…]++|…(?![ \t]+\())*+…[ \t]+\(", "search"),
        # A repeat that ends the pattern but whose body is several items can
        # scan to the end within one iteration and fail, from every start:
        (r"(?:\D*\d)+", "search"),
        (r"x(?:[^\n]*\n)+", "search"),
        (r"\*(?:[^\n]*+\d)+", "search"),
        (r"(?:\D*\d)*", "finditer"),
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
        (r"\w*ing\b.*x", "match"),  # \b after a word character pins the \w* run
        (r"[\w-]*ing(?=…).*…", "match"),  # so does a lookahead the run cannot consume
        # A one-character repeat that ends the pattern cannot fail after scanning:
        (r"─{8,}", "search"),
        (r"(?:a++|b)*c", "match"),  # a possessive inner repeat cannot be re-split
        (r"(?:x{3})+y", "match"),  # nor can a fixed count
        (r"(?:\d{1,3}\.){3}\d{1,3}", "match"),  # a bounded inner repeat, fenced by "."
        (r"(?:\S+\s+)*x", "match"),  # each repeat is fenced by the other's class
        (r"Press \S+(?:[^\S\n]+\S+){0,14} to", "search"),
        (r"(?:[ \t]*+\n){0,2}+x", "match"),  # nor can a possessive bounded group
        # Line-anchored skip; the lookahead's repeat is fenced by the "…" before it.
        (r"(?m)^(?:[^\n…]++|…(?![ \t]+\())*+…[ \t]+\(", "search"),
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


@pytest.mark.parametrize(
    "pattern,mode",
    [
        (r"(?:\D*\d)+", "search"),
        (r"x(?:[^\n]*\n)+", "search"),
        (r"(?:\w{1,20}\s?){1,20}:", "match"),
        (r"(?:a{0,200})*b", "match"),
    ],
)
def test_flagged_shapes_really_grow_superlinearly(pattern, mode):
    # The step count agrees with the structure check on the shapes it flags:
    # the terminal-repeat rule and bounded repeats inside a repeat.
    assert structural_issues(pattern, mode)
    assert first_superlinear(pattern_probe(pattern, mode), input_shapes(pattern))


def test_bounded_max_is_where_a_bound_stops_counting_as_unbounded():
    # A repeat bounded at BOUNDED_MAX re-scans at most that many characters per
    # attempt (linear with a constant); one past it is treated as unbounded.
    assert BOUNDED_MAX == 256
    assert structural_issues(r"[*][^\n]{0,256}x", "search") == []
    assert structural_issues(r"[*][^\n]{0,257}x", "search")


def test_budget_stops_runaway_backtracking():
    with pytest.raises(StepBudgetExceeded):
        count_steps(r"(a+)+b", "a" * 40, "match", budget=10_000)


def test_growth_tells_quadratic_from_linear():
    quadratic = r"[*][^\n]*x"
    linear = r"(?m)^[^*\n]*+[*][^\n]*x"
    shapes = input_shapes(quadratic)
    assert first_superlinear(pattern_probe(quadratic, "search"), shapes) is not None
    assert nonlinear_shapes(pattern_probe(linear, "search"), shapes) == []
