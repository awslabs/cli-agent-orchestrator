"""Load-independent ReDoS checks for the provider status and extraction regexes.

Provider status detection runs untrusted text: whatever an agent CLI paints on
its terminal reaches these patterns, and the agent decides what it paints. A
pattern whose backtracking can re-enter the same characters many times costs
polynomial time in the buffer length (CWE-1333). Timing a pattern against a
wall-clock budget catches that, but it flakes on a loaded CI box, so nothing
here measures seconds. Instead there are two independent checks.

* :func:`structural_issues` reads the pattern's ``sre`` parse tree and reports
  the shapes that make CPython's backtracking engine superlinear:

  - a repeat nested inside another repeat with no unambiguous fence between
    iterations;
  - an alternation under a repeat whose branches can start with the same
    character;
  - two repeats in sequence that can both consume the same characters, with
    nothing between them that pins the boundary;
  - for ``search``/``finditer`` use, a repeat that can scan over characters from
    which another attempt could start and scan again ("start-position
    rescans", the quadratic in an unanchored ``[glyph][^\\n]*X``).

* :func:`count_steps` is a small backtracking matcher that follows the same
  search order as ``sre`` (leftmost-first, greedy/lazy/possessive repeats,
  lookarounds) and counts the work it does. The growth helpers feed a pattern
  inputs of size n, 4n and 16n and compare step counts: linear work grows ~4x
  per step, quadratic ~16x. Every span it reports is checked against ``re``
  itself, so the counts describe the real pattern, not a look-alike.

The inputs for the growth checks are generated from the pattern's own parse
tree (:func:`input_shapes`): the path to each repeat, the characters its body
accepts, and the pattern's literals. No hand-written attack strings live in the
tests; a shape is "repeat this short unit", and the generator derives the unit.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from re import _constants as C  # type: ignore[attr-defined]
from re import _parser as P  # type: ignore[attr-defined]
from typing import Callable, Iterable, Iterator, Optional, Sequence, Union

PatternLike = Union[str, "re.Pattern[str]"]

#: Repeats with an upper bound at or below this are treated as bounded: they can
#: re-scan at most this many characters per attempt, so they cannot turn a
#: linear scan quadratic on their own (e.g. kimi's ``[^)●]{0,80}``).
BOUNDED_MAX = 256

_UNBOUNDED = C.MAXREPEAT


def _unpack(pattern: PatternLike, flags: int) -> tuple[str, int]:
    if isinstance(pattern, re.Pattern):
        return pattern.pattern, pattern.flags
    return pattern, flags


# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------


def _is_word(ch: str, ascii_only: bool) -> bool:
    return (ch.isalnum() or ch == "_") and (not ascii_only or ch.isascii())


def _is_space(ch: str, ascii_only: bool) -> bool:
    if ascii_only:
        return ch in " \t\n\r\f\v"
    return ch.isspace()


def _is_digit(ch: str, ascii_only: bool) -> bool:
    if ascii_only:
        return "0" <= ch <= "9"
    return ch.isdecimal()


def _category(code: object, ascii_only: bool) -> Callable[[str], bool]:
    name = str(code)
    if name == "CATEGORY_DIGIT":
        return lambda ch: _is_digit(ch, ascii_only)
    if name == "CATEGORY_NOT_DIGIT":
        return lambda ch: not _is_digit(ch, ascii_only)
    if name == "CATEGORY_SPACE":
        return lambda ch: _is_space(ch, ascii_only)
    if name == "CATEGORY_NOT_SPACE":
        return lambda ch: not _is_space(ch, ascii_only)
    if name == "CATEGORY_WORD":
        return lambda ch: _is_word(ch, ascii_only)
    if name == "CATEGORY_NOT_WORD":
        return lambda ch: not _is_word(ch, ascii_only)
    raise NotImplementedError(f"category {name}")


def _fold(ch: str) -> set[str]:
    return {ch, ch.lower(), ch.upper()}


def _single_char_predicate(op: object, av: object, flags: int) -> Optional[Callable[[str], bool]]:
    """Predicate for a node that consumes exactly one character, else None."""
    icase = bool(flags & re.IGNORECASE)
    ascii_only = bool(flags & re.ASCII)
    if op is C.LITERAL:
        lit = chr(av)  # type: ignore[arg-type]
        if icase:
            variants = _fold(lit)
            return lambda ch: bool(_fold(ch) & variants)
        return lambda ch: ch == lit
    if op is C.NOT_LITERAL:
        lit = chr(av)  # type: ignore[arg-type]
        if icase:
            variants = _fold(lit)
            return lambda ch: not (_fold(ch) & variants)
        return lambda ch: ch != lit
    if op is C.ANY:
        if flags & re.DOTALL:
            return lambda ch: True
        return lambda ch: ch != "\n"
    if op is C.IN:
        negate = False
        tests: list[Callable[[str], bool]] = []
        for item_op, item_av in av:  # type: ignore[attr-defined]
            if item_op is C.NEGATE:
                negate = True
            elif item_op is C.LITERAL:
                lit = chr(item_av)
                tests.append(lambda ch, lit=lit: ch == lit)
            elif item_op is C.RANGE:
                lo, hi = chr(item_av[0]), chr(item_av[1])
                tests.append(lambda ch, lo=lo, hi=hi: lo <= ch <= hi)
            elif item_op is C.CATEGORY:
                tests.append(_category(item_av, ascii_only))
            else:
                raise NotImplementedError(f"class item {item_op}")

        def member(ch: str) -> bool:
            cands = _fold(ch) if icase else (ch,)
            hit = any(test(c) for c in cands for test in tests)
            return hit != negate

        return member
    return None


# ---------------------------------------------------------------------------
# The step-counting matcher
# ---------------------------------------------------------------------------


class StepBudgetExceeded(Exception):
    """The matcher did more work than the caller allowed."""


Cont = Callable[[int], bool]
Node = Callable[[int, Cont], bool]


class _Engine:
    """A backtracking matcher over the ``sre`` parse tree that counts its steps.

    It visits alternatives and repeat counts in the same order as ``sre``, so it
    finds the same leftmost-first match and does asymptotically the same
    backtracking. One step is one node visit, one character a repeat consumes,
    or one backtracking retry.
    """

    def __init__(self, pattern: PatternLike, flags: int = 0, budget: int = 10**6) -> None:
        source, flags = _unpack(pattern, flags)
        tree = P.parse(source, flags)
        self.flags = tree.state.flags
        self.budget = budget
        self.steps = 0
        self.text = ""
        self.n = 0
        self.root = self._seq(list(tree), self.flags)

    # -- bookkeeping ------------------------------------------------------
    def _tick(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > self.budget:
            raise StepBudgetExceeded(self.steps)

    # -- compilation ------------------------------------------------------
    def _seq(self, nodes: Sequence[tuple[object, object]], flags: int) -> Node:
        compiled = [self._node(op, av, flags) for op, av in nodes]

        def chain(index: int) -> Node:
            if index == len(compiled):
                return lambda pos, k: k(pos)
            head = compiled[index]
            if index == len(compiled) - 1:
                return head
            tail = chain(index + 1)
            return lambda pos, k: head(pos, lambda p: tail(p, k))

        return chain(0)

    def _node(self, op: object, av: object, flags: int) -> Node:
        pred = _single_char_predicate(op, av, flags)
        if pred is not None:
            return self._char(pred)
        if op is C.AT:
            return self._at(av, flags)
        if op is C.BRANCH:
            alts = [self._seq(list(alt), flags) for alt in av[1]]  # type: ignore[index]

            def branch(pos: int, k: Cont) -> bool:
                for alt in alts:
                    self._tick()
                    if alt(pos, k):
                        return True
                return False

            return branch
        if op is C.SUBPATTERN:
            _group, add, delete, sub = av  # type: ignore[misc]
            return self._seq(list(sub), (flags | add) & ~delete)
        if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
            lo, hi, sub = av  # type: ignore[misc]
            return self._repeat(op, lo, hi, list(sub), flags)
        if op is C.ATOMIC_GROUP:
            body = self._seq(list(av), flags)  # type: ignore[arg-type]
            return self._atomic(body)
        if op in (C.ASSERT, C.ASSERT_NOT):
            direction, sub = av  # type: ignore[misc]
            return self._assert(op is C.ASSERT, direction, sub, flags)
        raise NotImplementedError(f"regex construct {op}")

    def _char(self, pred: Callable[[str], bool]) -> Node:
        def char(pos: int, k: Cont) -> bool:
            self._tick()
            if pos < self.n and pred(self.text[pos]):
                return k(pos + 1)
            return False

        return char

    def _at(self, code: object, flags: int) -> Node:
        multiline = bool(flags & re.MULTILINE)
        ascii_only = bool(flags & re.ASCII)
        name = str(code)

        def word_at(pos: int) -> bool:
            return 0 <= pos < self.n and _is_word(self.text[pos], ascii_only)

        def holds(pos: int) -> bool:
            if name == "AT_BEGINNING":
                return pos == 0 or (multiline and self.text[pos - 1] == "\n")
            if name == "AT_BEGINNING_STRING":
                return pos == 0
            if name == "AT_END":
                if multiline:
                    return pos == self.n or self.text[pos] == "\n"
                return pos == self.n or (pos == self.n - 1 and self.text[pos] == "\n")
            if name == "AT_END_STRING":
                return pos == self.n
            if name == "AT_BOUNDARY":
                return word_at(pos - 1) != word_at(pos)
            if name == "AT_NON_BOUNDARY":
                return word_at(pos - 1) == word_at(pos)
            raise NotImplementedError(name)

        def at(pos: int, k: Cont) -> bool:
            self._tick()
            return holds(pos) and k(pos)

        return at

    def _first_end(self, body: Node, pos: int) -> Optional[int]:
        found: list[int] = []

        def grab(p: int) -> bool:
            found.append(p)
            return True

        return found[0] if body(pos, grab) else None

    def _atomic(self, body: Node) -> Node:
        def atomic(pos: int, k: Cont) -> bool:
            end = self._first_end(body, pos)
            return end is not None and k(end)

        return atomic

    def _assert(self, positive: bool, direction: int, sub: object, flags: int) -> Node:
        body = self._seq(list(sub), flags)  # type: ignore[call-overload]
        width = sub.getwidth()[0] if direction < 0 else 0  # type: ignore[attr-defined]

        def look(pos: int, k: Cont) -> bool:
            self._tick()
            if direction < 0:
                start = pos - width
                ok = start >= 0 and body(start, lambda p: p == pos)
            else:
                ok = body(pos, lambda p: True)
            return ok == positive and k(pos)

        return look

    def _repeat(
        self, op: object, lo: int, hi: int, sub: list[tuple[object, object]], flags: int
    ) -> Node:
        limit = float("inf") if hi is _UNBOUNDED else hi
        pred = _single_char_predicate(sub[0][0], sub[0][1], flags) if len(sub) == 1 else None
        if pred is not None:
            return self._repeat_one(op, lo, limit, pred)
        body = self._seq(sub, flags)

        if op is C.POSSESSIVE_REPEAT:

            def possessive(pos: int, k: Cont) -> bool:
                count, cur = 0, pos
                while count < limit:
                    self._tick()
                    end = self._first_end(body, cur)
                    if end is None or (end == cur and count >= lo):
                        break
                    count, cur = count + 1, end
                return count >= lo and k(cur)

            return possessive

        greedy = op is C.MAX_REPEAT

        def repeat(pos: int, k: Cont) -> bool:
            def step(count: int, cur: int) -> bool:
                self._tick()

                def more() -> bool:
                    if count >= limit:
                        return False
                    return body(
                        cur, lambda p: not (p == cur and count >= lo) and step(count + 1, p)
                    )

                if greedy:
                    if more():
                        return True
                    return count >= lo and k(cur)
                if count >= lo and k(cur):
                    return True
                return more()

            return step(0, pos)

        return repeat

    def _repeat_one(self, op: object, lo: int, limit: float, pred: Callable[[str], bool]) -> Node:
        def run_length(pos: int, cap: float) -> int:
            end = pos
            while end < self.n and end - pos < cap and pred(self.text[end]):
                end += 1
            self._tick(end - pos + 1)
            return end

        if op is C.POSSESSIVE_REPEAT:

            def possessive(pos: int, k: Cont) -> bool:
                end = run_length(pos, limit)
                return end - pos >= lo and k(end)

            return possessive

        if op is C.MAX_REPEAT:

            def greedy(pos: int, k: Cont) -> bool:
                end = run_length(pos, limit)
                for p in range(end, pos + lo - 1, -1):
                    self._tick()
                    if k(p):
                        return True
                return False

            return greedy

        def lazy(pos: int, k: Cont) -> bool:
            end = run_length(pos, lo)
            if end - pos < lo:
                return False
            cur, count = end, lo
            while True:
                self._tick()
                if k(cur):
                    return True
                if count >= limit or cur >= self.n or not pred(self.text[cur]):
                    return False
                cur, count = cur + 1, count + 1

        return lazy

    # -- running ----------------------------------------------------------
    def _reset(self, text: str) -> None:
        self.text = text
        self.n = len(text)

    def match_at(self, text: str, pos: int, full: bool = False) -> Optional[tuple[int, int]]:
        self._reset(text)
        end: list[int] = []

        def accept(p: int) -> bool:
            if full and p != self.n:
                return False
            end.append(p)
            return True

        return (pos, end[0]) if self.root(pos, accept) else None

    def search(self, text: str, pos: int = 0) -> Optional[tuple[int, int]]:
        self._reset(text)
        for start in range(pos, self.n + 1):
            self._tick()
            span = self.match_at(text, start)
            if span is not None:
                return span
        return None

    def finditer(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        pos = 0
        while pos <= len(text):
            span = self.search(text, pos)
            if span is None:
                break
            spans.append(span)
            pos = span[1] if span[1] > span[0] else span[1] + 1
        return spans


@contextmanager
def _deep_recursion(limit: int = 200_000) -> Iterator[None]:
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, limit))
    try:
        yield
    finally:
        sys.setrecursionlimit(old)


MODES = ("search", "finditer", "match", "fullmatch")


def count_steps(
    pattern: PatternLike,
    text: str,
    mode: str = "search",
    flags: int = 0,
    budget: int = 10**6,
    verify: bool = True,
) -> int:
    """Steps a backtracking engine spends running ``pattern`` over ``text``.

    ``mode`` is how the caller uses the pattern: ``search``, ``finditer``
    (also ``findall``/``sub``), ``match`` or ``fullmatch``. Raises
    :class:`StepBudgetExceeded` past ``budget``. With ``verify`` the spans found
    are checked against ``re``, so a count can never describe a different match.
    """
    if mode not in MODES:
        raise ValueError(mode)
    source, flags = _unpack(pattern, flags)
    engine = _Engine(source, flags, budget)
    compiled = re.compile(source, flags)
    with _deep_recursion():
        if mode == "search":
            got: object = engine.search(text)
            want: object = (m := compiled.search(text)) and m.span()
        elif mode == "finditer":
            got = engine.finditer(text)
            want = [m.span() for m in compiled.finditer(text)]
        elif mode == "match":
            got = engine.match_at(text, 0)
            want = (m := compiled.match(text)) and m.span()
        else:
            got = engine.match_at(text, 0, full=True)
            want = (m := compiled.fullmatch(text)) and m.span()
    if verify and (got or None) != (want or None):
        raise AssertionError(f"step matcher disagrees with re on {source!r}: {got!r} != {want!r}")
    return engine.steps


# ---------------------------------------------------------------------------
# Instrumented runs of code that calls compiled patterns
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self, budget: int) -> None:
        self.steps = 0
        self.budget = budget

    def add(self, amount: int) -> None:
        self.steps += amount
        if self.steps > self.budget:
            raise StepBudgetExceeded(self.steps)


@dataclass
class _SimMatch:
    """The slice of ``re.Match`` that scanner code reads."""

    string: str
    span_: tuple[int, int]
    re: "re.Pattern[str]"

    def start(self, group: int = 0) -> int:
        if group:
            return self._real().start(group)
        return self.span_[0]

    def end(self, group: int = 0) -> int:
        if group:
            return self._real().end(group)
        return self.span_[1]

    def span(self, group: int = 0) -> tuple[int, int]:
        return self.start(group), self.end(group)

    def group(self, group: int = 0) -> str:
        if group:
            return self._real().group(group)
        return self.string[self.span_[0] : self.span_[1]]

    def _real(self) -> "re.Match[str]":
        real = self.re.match(self.string, self.span_[0])
        assert real is not None and real.span() == self.span_
        return real


class CountingPattern:
    """Stands in for a compiled pattern and charges its steps to a counter."""

    def __init__(self, real: "re.Pattern[str]", counter: _Counter) -> None:
        self._real = real
        self._counter = counter
        self.pattern = real.pattern
        self.flags = real.flags

    def _engine(self) -> _Engine:
        return _Engine(self._real.pattern, self._real.flags, budget=10**12)

    def _charge(self, engine: _Engine) -> None:
        self._counter.add(engine.steps)

    def _wrap(self, text: str, span: Optional[tuple[int, int]]) -> Optional[_SimMatch]:
        return None if span is None else _SimMatch(str(text), span, self._real)

    def search(self, text: str, pos: int = 0) -> Optional[_SimMatch]:
        engine = self._engine()
        with _deep_recursion():
            span = engine.search(str(text), pos)
        self._charge(engine)
        real = self._real.search(text, pos)
        assert (real and real.span()) == (span or None) or (real is None and span is None)
        return self._wrap(text, span)

    def match(self, text: str, pos: int = 0) -> Optional[_SimMatch]:
        engine = self._engine()
        with _deep_recursion():
            span = engine.match_at(str(text), pos)
        self._charge(engine)
        real = self._real.match(text, pos)
        assert (real.span() if real else None) == span
        return self._wrap(text, span)

    def finditer(self, text: str) -> Iterator[_SimMatch]:
        engine = self._engine()
        with _deep_recursion():
            spans = engine.finditer(str(text))
        self._charge(engine)
        assert spans == [m.span() for m in self._real.finditer(text)]
        return iter([_SimMatch(str(text), s, self._real) for s in spans])

    def sub(self, repl: str, text: str) -> str:
        if not isinstance(repl, str) or "\\" in repl:
            raise NotImplementedError("CountingPattern.sub takes a plain string replacement")
        out, last = [], 0
        for m in self.finditer(text):
            out.append(str(text)[last : m.start()])
            out.append(repl)
            last = m.end()
        out.append(str(text)[last:])
        result = "".join(out)
        assert result == self._real.sub(repl, text)
        return result


class CountingStr(str):
    """A ``str`` whose ``find`` charges the characters it scans.

    ``split`` charges one pass and returns counted parts, so a scan that works
    line by line is still counted.
    """

    counter: _Counter

    def __new__(cls, value: str, counter: _Counter) -> "CountingStr":
        obj = super().__new__(cls, value)
        obj.counter = counter
        return obj

    def find(self, sub: str, start: Optional[int] = None, end: Optional[int] = None) -> int:  # type: ignore[override]
        begin = 0 if start is None else start
        result = super().find(sub, start, end)
        stop = (len(self) if end is None else end) if result == -1 else result + len(sub)
        self.counter.add(max(1, stop - begin))
        return result

    def split(self, sep: Optional[str] = None, maxsplit: int = -1) -> list[str]:  # type: ignore[override]
        self.counter.add(max(1, len(self)))
        return [CountingStr(part, self.counter) for part in super().split(sep, maxsplit)]


def instrumented_steps(
    run: Callable[[str], object],
    text: str,
    patterns: dict[object, Iterable[str]],
    budget: int = 10**7,
) -> int:
    """Total steps ``run(text)`` spends in the named compiled patterns and ``str.find``.

    ``patterns`` maps a module (or any object) to attribute names of compiled
    patterns it uses; each is swapped for a :class:`CountingPattern` for the run.
    ``text`` reaches ``run`` as a :class:`CountingStr`.
    """
    counter = _Counter(budget)
    saved: list[tuple[object, str, object]] = []
    try:
        for owner, names in patterns.items():
            for name in names:
                real = getattr(owner, name)
                saved.append((owner, name, real))
                setattr(owner, name, CountingPattern(real, counter))
        run(CountingStr(text, counter))
    finally:
        for owner, name, real in reversed(saved):
            setattr(owner, name, real)
    return counter.steps


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

_PROBE_CHARS = (
    " \t\n\r\x0b\x0c\xa0 　"  # whitespace, ASCII and not
    "09٣azAZ_é中"  # digits and word characters, ASCII and not
    ".-/:()[]%@!#,;=<>|*+?$^{}'\"`~&\\"
    "\x00\x07\x1b￿…·•─❯●"
)


@dataclass
class _Ctx:
    flags: int
    alphabet: frozenset[str]

    def chars_of(self, pred: Callable[[str], bool]) -> frozenset[str]:
        return frozenset(ch for ch in self.alphabet if pred(ch))


def _alphabet(tree: object, flags: int) -> frozenset[str]:
    chars = set(_PROBE_CHARS)

    def walk(nodes: Iterable[tuple[object, object]]) -> None:
        for op, av in nodes:
            if op in (C.LITERAL, C.NOT_LITERAL):
                chars.update(_fold(chr(av)))  # type: ignore[arg-type]
            elif op is C.IN:
                for item_op, item_av in av:  # type: ignore[attr-defined]
                    if item_op is C.LITERAL:
                        chars.update(_fold(chr(item_av)))
                    elif item_op is C.RANGE:
                        lo, hi = item_av
                        chars.update({chr(lo), chr(hi), chr((lo + hi) // 2)})
                        if lo > 0:
                            chars.add(chr(lo - 1))
                        if hi < sys.maxunicode:
                            chars.add(chr(hi + 1))
            elif op is C.BRANCH:
                for alt in av[1]:  # type: ignore[index]
                    walk(alt)
            elif op is C.SUBPATTERN:
                walk(av[-1])  # type: ignore[index]
            elif op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
                walk(av[2])  # type: ignore[index]
            elif op is C.ATOMIC_GROUP:
                walk(av)  # type: ignore[arg-type]
            elif op in (C.ASSERT, C.ASSERT_NOT):
                walk(av[1])  # type: ignore[index]

    walk(tree)  # type: ignore[arg-type]
    return frozenset(chars)


def _is_unbounded(hi: int) -> bool:
    return hi is _UNBOUNDED or hi > BOUNDED_MAX


@dataclass
class _Shape:
    """What a subpattern can consume: nullable, its first characters, all characters."""

    nullable: bool
    first: frozenset[str]
    chars: frozenset[str]


def _shape_seq(nodes: Sequence[tuple[object, object]], ctx: _Ctx, flags: int) -> _Shape:
    nullable = True
    first: set[str] = set()
    chars: set[str] = set()
    for op, av in nodes:
        sh = _shape(op, av, ctx, flags)
        chars |= sh.chars
        if nullable:
            first |= sh.first
        nullable = nullable and sh.nullable
    return _Shape(nullable, frozenset(first), frozenset(chars))


def _shape(op: object, av: object, ctx: _Ctx, flags: int) -> _Shape:
    pred = _single_char_predicate(op, av, flags)
    if pred is not None:
        cs = ctx.chars_of(pred)
        return _Shape(False, cs, cs)
    if op in (C.AT, C.ASSERT, C.ASSERT_NOT):
        return _Shape(True, frozenset(), frozenset())
    if op is C.BRANCH:
        shapes = [_shape_seq(alt, ctx, flags) for alt in av[1]]  # type: ignore[index]
        return _Shape(
            any(s.nullable for s in shapes),
            frozenset().union(*(s.first for s in shapes)),
            frozenset().union(*(s.chars for s in shapes)),
        )
    if op is C.SUBPATTERN:
        _g, add, delete, sub = av  # type: ignore[misc]
        return _shape_seq(sub, ctx, (flags | add) & ~delete)
    if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
        lo, _hi, sub = av  # type: ignore[misc]
        sh = _shape_seq(sub, ctx, flags)
        return _Shape(lo == 0 or sh.nullable, sh.first, sh.chars)
    if op is C.ATOMIC_GROUP:
        return _shape_seq(av, ctx, flags)  # type: ignore[arg-type]
    raise NotImplementedError(f"regex construct {op}")


@dataclass
class _Item:
    """One element of a path through the pattern, for the walking checks."""

    kind: str  # "token" | "repeat" | "lookbehind" | "lookahead" | "anchor"
    shape: _Shape
    label: str
    possessive: bool = False
    #: repeat only: its body is one single-character item, as in ``─{8,}``.
    single_char: bool = False
    negative: bool = False
    look_chars: frozenset[str] = field(default_factory=frozenset)
    #: lookahead only: the characters its body can start with, whether the body
    #: can match empty, and the characters of the unbounded repeats inside it.
    look_first: frozenset[str] = field(default_factory=frozenset)
    look_nullable: bool = True
    look_repeat_chars: frozenset[str] = field(default_factory=frozenset)


_MAX_PATHS = 4096


def _paths(
    nodes: Sequence[tuple[object, object]], ctx: _Ctx, flags: int, source: str
) -> list[list[_Item]]:
    """Expand a sequence into the paths through it (branches and optionals fork)."""
    paths: list[list[_Item]] = [[]]
    for op, av in nodes:
        options = _node_paths(op, av, ctx, flags, source)
        paths = [p + o for p in paths for o in options][:_MAX_PATHS]
    return paths


def _label(op: object, av: object) -> str:
    return f"{str(op).lower()}"


def _node_paths(op: object, av: object, ctx: _Ctx, flags: int, source: str) -> list[list[_Item]]:
    pred = _single_char_predicate(op, av, flags)
    if pred is not None:
        return [[_Item("token", _shape(op, av, ctx, flags), _label(op, av))]]
    if op is C.AT:
        return [[_Item("anchor", _Shape(True, frozenset(), frozenset()), str(av))]]
    if op in (C.ASSERT, C.ASSERT_NOT):
        direction, sub = av  # type: ignore[misc]
        sh = _shape_seq(sub, ctx, flags)
        if direction < 0:
            return [
                [
                    _Item(
                        "lookbehind",
                        _Shape(True, frozenset(), frozenset()),
                        "lookbehind",
                        negative=op is C.ASSERT_NOT,
                        look_chars=sh.chars,
                    )
                ]
            ]
        repeat_chars: set[str] = set()
        for r_op, r_av, _depth in _repeats_in(list(sub)):
            if _is_unbounded(r_av[1]):  # type: ignore[index]
                repeat_chars |= _shape_seq(r_av[2], ctx, flags).chars  # type: ignore[index]
        return [
            [
                _Item(
                    "lookahead",
                    _Shape(True, frozenset(), frozenset()),
                    "negative lookahead" if op is C.ASSERT_NOT else "lookahead",
                    negative=op is C.ASSERT_NOT,
                    look_chars=sh.chars,
                    look_first=sh.first,
                    look_nullable=sh.nullable,
                    look_repeat_chars=frozenset(repeat_chars),
                )
            ]
        ]
    if op is C.BRANCH:
        out: list[list[_Item]] = []
        for alt in av[1]:  # type: ignore[index]
            out.extend(_paths(alt, ctx, flags, source))
        return out[:_MAX_PATHS]
    if op is C.SUBPATTERN:
        _g, add, delete, sub = av  # type: ignore[misc]
        return _paths(sub, ctx, (flags | add) & ~delete, source)
    if op is C.ATOMIC_GROUP:
        sh = _shape_seq(av, ctx, flags)  # type: ignore[arg-type]
        return [[_Item("repeat" if not sh.nullable else "token", sh, "atomic", possessive=True)]]
    if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
        lo, hi, sub = av  # type: ignore[misc]
        sh = _shape_seq(sub, ctx, flags)
        if _is_unbounded(hi):
            item = _Item(
                "repeat",
                _Shape(lo == 0 or sh.nullable, sh.first, sh.chars),
                f"{'possessive ' if op is C.POSSESSIVE_REPEAT else ''}repeat over "
                f"{_describe(sh.chars)}",
                possessive=op is C.POSSESSIVE_REPEAT,
                single_char=_single_char_body(sub, flags),
            )
            return [[item]]
        if op is C.POSSESSIVE_REPEAT and not _single_char_body(sub, flags):
            # Atomic once matched: it can rescan, but nothing outside can make
            # it re-split its own iterations.
            return [
                [
                    _Item(
                        "repeat",
                        _Shape(lo == 0 or sh.nullable, sh.first, sh.chars),
                        f"possessive bounded repeat over {_describe(sh.chars)}",
                        possessive=True,
                    )
                ]
            ]
        # Bounded: one pass through the body stands for its iterations, and an
        # optional repeat can also be skipped. Adjacent iterations are checked
        # against each other by the nesting check, which treats a bounded repeat
        # whose body can vary like an unbounded one (see _nested_issues).
        inner = _paths(sub, ctx, flags, source)
        return (([[]] if lo == 0 else []) + inner)[:_MAX_PATHS]
    raise NotImplementedError(f"regex construct {op}")


def _single_char_body(sub: object, flags: int) -> bool:
    body = list(sub)  # type: ignore[call-overload]
    return len(body) == 1 and _single_char_predicate(body[0][0], body[0][1], flags) is not None


def _describe(chars: frozenset[str]) -> str:
    shown = "".join(sorted(chars))[:12]
    return repr(shown) + ("…" if len(chars) > 12 else "")


def _repeats_in(nodes: Sequence[tuple[object, object]]) -> Iterator[tuple[object, object, int]]:
    """Yield (op, av, depth) for every repeat in the tree."""

    def walk(
        seq: Iterable[tuple[object, object]], depth: int
    ) -> Iterator[tuple[object, object, int]]:
        for op, av in seq:
            if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
                yield op, av, depth
                yield from walk(av[2], depth + 1)  # type: ignore[index]
            elif op is C.SUBPATTERN:
                yield from walk(av[-1], depth)  # type: ignore[index]
            elif op is C.BRANCH:
                for alt in av[1]:  # type: ignore[index]
                    yield from walk(alt, depth)
            elif op is C.ATOMIC_GROUP:
                yield from walk(av, depth)  # type: ignore[arg-type]
            elif op in (C.ASSERT, C.ASSERT_NOT):
                yield from walk(av[1], depth)  # type: ignore[index]

    return walk(nodes, 0)


def _nested_issues(nodes: Sequence[tuple[object, object]], ctx: _Ctx, flags: int) -> list[str]:
    """S1/S2: nested unbounded repeats without a fence; ambiguous alternation under a repeat."""
    issues: list[str] = []
    for op, av, _depth in _repeats_in(nodes):
        lo, hi, sub = av  # type: ignore[misc]
        if op is C.POSSESSIVE_REPEAT:
            continue
        body = list(sub)
        # A bounded repeat that can run its body twice is as ambiguous as an
        # unbounded one whenever the body itself can vary: (?:\w+\s?){1,20}:
        # and (?:a|a){0,200}b are exponential.
        if not _is_unbounded(hi) and not (hi >= 2 and _varies(body)):
            continue
        # S2: alternation directly under the repeat
        for b_op, b_av in _flatten_groups(body):
            if b_op is C.BRANCH:
                shapes = [_shape_seq(alt, ctx, flags) for alt in b_av[1]]  # type: ignore[index]
                if any(s.nullable for s in shapes):
                    issues.append("alternation under a repeat has an empty branch")
                for i in range(len(shapes)):
                    for j in range(i + 1, len(shapes)):
                        if shapes[i].first & shapes[j].first:
                            issues.append(
                                "alternation under a repeat has branches that can start "
                                f"with the same character ({_describe(shapes[i].first & shapes[j].first)})"
                            )
        # S1: unbounded repeats inside the body need a fence on both sides. A
        # possessive inner repeat cannot be re-split across iterations, so it
        # needs none. A sequence's neighbours in its parent (the token before a
        # lookaround or a branch) fence its ends; the body's own ends meet the
        # neighbouring iterations, so its last element fences its first and
        # its first fences its last.
        for seq, before, after in _sequences_in(body, body[-1], body[0]):
            for index, (i_op, i_av) in enumerate(seq):
                if i_op not in (C.MAX_REPEAT, C.MIN_REPEAT):
                    continue
                if not _is_unbounded(i_av[1]):  # type: ignore[index]
                    continue
                inner = _shape_seq(i_av[2], ctx, flags).chars  # type: ignore[index]
                left = seq[index - 1] if index > 0 else before
                right = seq[index + 1] if index + 1 < len(seq) else after
                if not (_fence(left, inner, ctx, flags) and _fence(right, inner, ctx, flags)):
                    issues.append(
                        f"repeat over {_describe(inner)} nested in another repeat with no fence "
                        "on both sides"
                    )
    return issues


def _flatten_groups(body: list[tuple[object, object]]) -> Iterator[tuple[object, object]]:
    for op, av in body:
        if op is C.SUBPATTERN:
            yield from _flatten_groups(list(av[-1]))  # type: ignore[index]
        else:
            yield op, av


Node_ = tuple[object, object]


def _sequences_in(
    body: list[Node_], before: Optional[Node_] = None, after: Optional[Node_] = None
) -> Iterator[tuple[list[Node_], Optional[Node_], Optional[Node_]]]:
    """Every sequence in ``body``, with the nodes just outside its two ends."""
    yield body, before, after
    for index, (op, av) in enumerate(body):
        left = body[index - 1] if index > 0 else before
        right = body[index + 1] if index + 1 < len(body) else after
        if op is C.SUBPATTERN:
            yield from _sequences_in(list(av[-1]), left, right)  # type: ignore[index]
        elif op is C.BRANCH:
            for alt in av[1]:  # type: ignore[index]
                yield from _sequences_in(list(alt), left, right)
        elif op in (C.ASSERT, C.ASSERT_NOT):
            # Zero-width: the character before its body is the one ``left`` matched.
            yield from _sequences_in(list(av[1]), left, None)  # type: ignore[index]
        elif op is C.ATOMIC_GROUP:
            yield from _sequences_in(list(av), left, right)  # type: ignore[arg-type]
        elif op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT) and not _is_unbounded(av[1]):  # type: ignore[index]
            yield from _sequences_in(list(av[2]), left, right)  # type: ignore[index]


def _varies(body: list[Node_]) -> bool:
    """The body holds an unbounded repeat or an alternation (at any depth)."""
    for seq, _before, _after in _sequences_in(body):
        for op, av in seq:
            if op is C.BRANCH:
                return True
            if op in (C.MAX_REPEAT, C.MIN_REPEAT) and _is_unbounded(av[1]):  # type: ignore[index]
                return True
    return False


def _fence(node: Optional[Node_], inner: frozenset[str], ctx: _Ctx, flags: int) -> bool:
    """``node`` must consume at least one character the inner repeat cannot."""
    if node is None:
        return False
    op, av = node
    if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
        lo, _hi, sub = av  # type: ignore[misc]
        body = list(sub)
        if lo < 1 or len(body) != 1:
            return False
        op, av = body[0]
    pred = _single_char_predicate(op, av, flags)
    if pred is None:
        return False
    return not (ctx.chars_of(pred) & inner)


def _overlap_issues(paths: list[list[_Item]], word: frozenset[str]) -> list[str]:
    """S3: two repeats in a row over shared characters with no boundary-pinning token.

    Also: a backtracking repeat followed by a lookahead that re-scans the same
    characters with a repeat of its own (each character the first repeat gives
    back re-runs the lookahead's scan). A positive lookahead, or ``\\b``, that
    demands a character the first repeat cannot consume pins the end of its run,
    so nothing after it can be split against it.
    """
    issues: set[str] = set()
    for path in paths:
        for i, first in enumerate(path):
            if first.kind != "repeat" or first.possessive:
                continue
            x = first.shape.chars
            between: list[frozenset[str]] = []
            previous = x
            for later in path[i + 1 :]:
                if later.kind == "lookahead":
                    if (later.look_first & x) and (later.look_repeat_chars & x):
                        issues.add(
                            f"{first.label} is followed by a {later.label} that re-scans "
                            f"its characters ({_describe(later.look_repeat_chars & x)})"
                        )
                    if not later.negative and not later.look_nullable:
                        if not (later.look_first & x):
                            break  # the next character cannot be in the run: pinned
                    continue
                if later.kind == "anchor":
                    if later.label in ("AT_BOUNDARY", "AT_NON_BOUNDARY"):
                        prev_word = previous <= word
                        prev_nonword = not (previous & word)
                        boundary = later.label == "AT_BOUNDARY"
                        # After a word character \b demands a non-word one next.
                        if boundary and prev_word and x <= word:
                            break
                        if boundary and prev_nonword and not (x & word):
                            break
                    continue
                if later.kind == "repeat":
                    shared = x & later.shape.chars
                    if shared and all(t & shared for t in between):
                        issues.add(
                            f"{first.label} and a later {later.label} can split the same "
                            f"characters ({_describe(shared)}) in many ways"
                        )
                    if not later.shape.nullable:
                        if not (later.shape.chars & x):
                            break
                        between.append(later.shape.chars)
                        previous = later.shape.chars
                elif later.kind == "token":
                    if not (later.shape.chars & x):
                        break
                    between.append(later.shape.chars)
                    previous = later.shape.chars
    return sorted(issues)


def _rescan_issues(paths: list[list[_Item]], multiline_start: bool) -> list[str]:
    """S4: a repeat scans characters from which another attempt can start and scan again."""
    issues: set[str] = set()
    for path in paths:
        triggers: Optional[frozenset[str]] = None  # None = any position starts an attempt
        index = 0
        if path and path[0].kind == "anchor":
            label = path[0].label
            if label == "AT_BEGINNING_STRING" or (label == "AT_BEGINNING" and not multiline_start):
                continue  # one start position: nothing to rescan
            if label == "AT_BEGINNING":
                triggers = frozenset("\n")
                index = 1
        elif path and path[0].kind == "lookbehind" and not path[0].negative:
            triggers = path[0].look_chars
            index = 1
        mandatory: list[frozenset[str]] = []
        blockers: list[_Item] = []
        rest = path[index:]
        for position, item in enumerate(rest):
            if item.kind == "lookbehind":
                blockers.append(item)
                continue
            if item.kind in ("anchor", "lookahead"):
                continue
            # A repeat of one character that ends the pattern cannot fail after
            # scanning: either it reaches its minimum and the attempt succeeds,
            # or it stops short within that minimum. Search resumes after a
            # success. A body of several items can scan to the end inside one
            # iteration and still fail, from every start: (?:\D*\d)+.
            terminal = position == len(rest) - 1 and item.single_char
            if item.kind == "repeat" and not terminal:
                c = item.shape.chars
                crosses = triggers is None or bool(c & triggers)
                passable = all(t & c for t in mandatory)
                blocked = any(
                    (b.negative and c <= b.look_chars)
                    or (not b.negative and not (c & b.look_chars))
                    for b in blockers
                )
                if crosses and passable and not blocked:
                    issues.add(f"{item.label} can rescan characters that start another attempt")
            if not item.shape.nullable:
                mandatory.append(item.shape.chars)
    return sorted(issues)


def structural_issues(pattern: PatternLike, mode: str = "search", flags: int = 0) -> list[str]:
    """Shapes in ``pattern`` that make backtracking superlinear (empty means clean).

    ``mode`` is how callers use the pattern; ``match``/``fullmatch`` run one
    attempt from a fixed position, so the start-position rescan check is skipped.
    """
    if mode not in MODES:
        raise ValueError(mode)
    source, flags = _unpack(pattern, flags)
    tree = P.parse(source, flags)
    flags = tree.state.flags
    ctx = _Ctx(flags, _alphabet(list(tree), flags))
    nodes = list(tree)
    issues = _nested_issues(nodes, ctx, flags)
    word = ctx.chars_of(lambda ch: _is_word(ch, bool(flags & re.ASCII)))
    paths = _paths(nodes, ctx, flags, source)
    issues += _overlap_issues(paths, word)
    for seq, _before, _after in _sequences_in(nodes):
        for op, av in seq:
            if op in (C.ASSERT, C.ASSERT_NOT):
                issues += _overlap_issues(_paths(av[1], ctx, flags, source), word)  # type: ignore[index]
    if mode in ("search", "finditer"):
        issues += _rescan_issues(paths, bool(flags & re.MULTILINE))
    return sorted(set(issues))


# ---------------------------------------------------------------------------
# Inputs grown from the pattern's own structure
# ---------------------------------------------------------------------------


def _samples(op: object, av: object, ctx: _Ctx, flags: int, limit: int = 3) -> list[str]:
    pred = _single_char_predicate(op, av, flags)
    if pred is None:
        return [""]
    preferred = [ch for ch in ctx.alphabet if pred(ch)]
    preferred.sort(key=lambda ch: (ch.isspace() is False, ch))
    return preferred[:limit] or [""]


def _minimal(
    nodes: Sequence[tuple[object, object]], ctx: _Ctx, flags: int, cap: int = 4
) -> list[str]:
    out = [""]
    for op, av in nodes:
        out = [a + b for a in out for b in _node_minimal(op, av, ctx, flags, cap)][:cap]
    return out


def _node_minimal(op: object, av: object, ctx: _Ctx, flags: int, cap: int) -> list[str]:
    if _single_char_predicate(op, av, flags) is not None:
        return _samples(op, av, ctx, flags, 2)
    if op is C.AT:
        return ["", "\n"] if str(av) == "AT_BEGINNING" and flags & re.MULTILINE else [""]
    if op is C.BRANCH:
        out: list[str] = []
        for alt in av[1]:  # type: ignore[index]
            out += _minimal(alt, ctx, flags, cap)
        return out[:cap] or [""]
    if op is C.SUBPATTERN:
        _g, add, delete, sub = av  # type: ignore[misc]
        return _minimal(sub, ctx, (flags | add) & ~delete, cap)
    if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
        lo, _hi, sub = av  # type: ignore[misc]
        return [b * lo for b in _minimal(sub, ctx, flags, cap)][:cap] or [""]
    if op is C.ATOMIC_GROUP:
        return _minimal(av, ctx, flags, cap)  # type: ignore[arg-type]
    return [""]


def input_shapes(pattern: PatternLike, flags: int = 0) -> list[tuple[str, str]]:
    """Repeatable units derived from ``pattern``: (prefix, unit) pairs.

    For every unbounded repeat, the shortest strings that reach it, each followed
    by characters its body accepts; plus the pattern's literal runs and the
    characters of each class. A growth check repeats ``unit`` after ``prefix``.
    """
    source, flags = _unpack(pattern, flags)
    tree = P.parse(source, flags)
    flags = tree.state.flags
    ctx = _Ctx(flags, _alphabet(list(tree), flags))
    shapes: set[tuple[str, str]] = set()

    def walk(seq: Sequence[tuple[object, object]], prefixes: list[str], flags: int) -> None:
        prefixes = prefixes[:4]
        literal = ""
        for op, av in seq:
            if op is C.LITERAL:
                literal += chr(av)  # type: ignore[arg-type]
            elif literal:
                shapes.add(("", literal))
                shapes.add(("", literal + " "))
                literal = ""
            if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
                lo, hi, sub = av  # type: ignore[misc]
                if _is_unbounded(hi) or hi >= 8:
                    bodies = [b for b in _minimal(sub, ctx, flags) if b] or ["a"]
                    if len(sub) == 1:
                        bodies = _samples(sub[0][0], sub[0][1], ctx, flags, 4) or bodies
                    for p in prefixes:
                        for b in bodies[:4]:
                            if not b:
                                continue
                            shapes.add(("", p + b))
                            shapes.add(("", b))
                            if p:
                                shapes.add((p, b))
                walk(list(sub), prefixes, flags)
            elif op is C.SUBPATTERN:
                _g, add, delete, sub = av  # type: ignore[misc]
                walk(list(sub), prefixes, (flags | add) & ~delete)
            elif op is C.BRANCH:
                for alt in av[1]:  # type: ignore[index]
                    walk(list(alt), prefixes, flags)
            elif op in (C.ASSERT, C.ASSERT_NOT):
                walk(list(av[1]), prefixes, flags)  # type: ignore[index]
            elif op is C.ATOMIC_GROUP:
                walk(list(av), prefixes, flags)  # type: ignore[arg-type]
            elif op is C.IN:
                for ch in _samples(op, av, ctx, flags, 4):
                    shapes.add(("", ch))
            nxt = _node_minimal(op, av, ctx, flags, 3)
            prefixes = [a + b for a in prefixes for b in nxt][:4]
        if literal:
            shapes.add(("", literal))
            shapes.add(("", literal + " "))

    walk(list(tree), [""], flags)
    return sorted(s for s in shapes if s[1])


def grow(prefix: str, unit: str, reps: int) -> str:
    """``prefix`` + ``unit`` repeated ``reps`` times + a terminator.

    The terminator (NUL) is a character none of these patterns accept, so a
    match attempt has to run to the end of the filler and fail there. Growth is
    measured in repetitions of the unit, so a long unit and a one-character
    unit both get n, 4n and 16n copies.
    """
    return prefix + unit * reps + "\x00"


#: Repetitions of the unit for n, 4n and 16n.
SIZES = (16, 64, 256)


@dataclass
class Growth:
    """Steps at n, 4n and 16n repetitions; ``None`` once the budget ran out.

    Linear work grows ~4x per step. ``linear`` allows up to 6x, for the fixed
    cost of the prefix and of setting up each attempt; ``superlinear`` needs
    more than 10x, which a quadratic reaches (~16x) and a linear scan cannot.
    """

    shape: tuple[str, str]
    steps: list[Optional[int]]

    @property
    def superlinear(self) -> bool:
        a, b, c = self.steps
        if a is None:
            return True
        if b is None or c is None:
            return True
        return b > 10 * a or c > 10 * b

    @property
    def linear(self) -> bool:
        a, b, c = self.steps
        if a is None or b is None or c is None:
            return False
        if not a:
            raise AssertionError(f"no steps counted on {self.shape!r}: the probe measures nothing")
        return b <= 6 * a and c <= 6 * b


def measure(
    probe: Callable[[str], int], shape: tuple[str, str], sizes: Sequence[int] = SIZES
) -> Growth:
    steps: list[Optional[int]] = []
    for reps in sizes:
        try:
            steps.append(probe(grow(shape[0], shape[1], reps)))
        except StepBudgetExceeded:
            steps.extend([None] * (len(sizes) - len(steps)))
            break
    return Growth(shape, steps)


def pattern_probe(
    pattern: PatternLike, mode: str, flags: int = 0, budget: int = 400_000
) -> Callable[[str], int]:
    def probe(text: str) -> int:
        return count_steps(pattern, text, mode, flags, budget)

    return probe


def superlinear_shapes(
    probe: Callable[[str], int], shapes: Iterable[tuple[str, str]]
) -> list[Growth]:
    """Every shape on which ``probe``'s work grows faster than linearly."""
    return [g for g in (measure(probe, s) for s in shapes) if g.superlinear]


def nonlinear_shapes(
    probe: Callable[[str], int], shapes: Iterable[tuple[str, str]]
) -> list[Growth]:
    """Every shape on which ``probe``'s work does NOT grow linearly."""
    return [g for g in (measure(probe, s) for s in shapes) if not g.linear]


def first_superlinear(
    probe: Callable[[str], int], shapes: Iterable[tuple[str, str]]
) -> Optional[Growth]:
    """The first shape on which ``probe``'s work grows faster than linearly, if any."""
    for shape in shapes:
        growth = measure(probe, shape)
        if growth.superlinear:
            return growth
    return None
