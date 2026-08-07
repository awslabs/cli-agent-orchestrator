"""Provider-native control of a Kimi TUI running in a managed pane.

This is the Kimi-specific control adapter: the one path by which CAO
sends ordinary follow-up, a deliberate mid-turn steer, or a literal slash
command to a native Kimi session.  It is deliberately *not* generic pane
paste and *not* any ACP receipt kind.  Three facts drive its shape.

**A pane write is not provider acceptance.**  Writing bytes to a
terminal proves only that a terminal accepted bytes.  It says nothing
about whether the provider's composer had focus, whether the TUI was
mid-render, or whether the model ever saw the text.  So the transport
result and the provider observation are separate fields, reached by
separate calls, and no code path lets the first stand in for the second.
An operation that was posted but never observed reads as posted -- never
as accepted.

**The queue and the steer are different acts.**  Ordinary follow-up must
wait for the provider to be idle and land in the native queue; it must
not barge into a running turn.  A steer is the opposite: it is a
deliberate interruption of one exact turn, requested explicitly, and it
is meaningless when nothing is running.  Collapsing them into "send
text" is what makes a routine nudge silently derail a long turn, so they
are separate operations with separate ids, separate gates, and separate
evidence.

**Ambiguity is preserved, never retried.**  If the transport raises, the
bytes may or may not have landed.  Re-sending is the one action that can
turn an uncertainty into a duplicate, so it is impossible here: an
ambiguous operation freezes, resolves only through :func:`reconcile` with
evidence naming its exact id, and blocks further operations on that
session until it is resolved.
"""

from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Tuple, cast

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment, provider_contracts
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

#: The only provider this adapter speaks for.  Named rather than
#: parameterized: the gating, the composer behaviour, and the slash
#: syntax below are Kimi's, and a second provider needs its own adapter
#: rather than a flag in this one.
PROVIDER = "kimi_cli"

#: Schema tags.  Distinct from every ACP receipt kind on purpose -- a
#: consumer must never be able to satisfy an ACP receipt requirement with
#: a native control record, in either direction.
RECORD_SCHEMA = "cao-kimi-native-control-v1"
INTENT_SCHEMA = "cao-kimi-native-control-intent-v1"
TURN_OBSERVATION_SCHEMA = "cao-kimi-native-turn-observation-v1"
PROVIDER_OBSERVATION_SCHEMA = "cao-kimi-native-control-observation-v1"

KIND_QUEUE = "queue"
KIND_STEER = "steer"
KIND_CONTROL = "control"
#: Lane C (design §8.3): one text+image operator message, journaled and
#: gated exactly like a queue operation but replaying on the caller's
#: whole-request digest.
KIND_OPERATOR_MESSAGE = "operator-message"
OPERATION_KINDS = frozenset({KIND_QUEUE, KIND_STEER, KIND_CONTROL, KIND_OPERATOR_MESSAGE})

#: ``intended`` -> intent is durable, nothing has been typed.
#: ``posted``   -> bytes reached the transport.  Transport fact only.
#: ``accepted`` -> the provider was observed taking the input.
#: ``completed``-> the provider was observed finishing the operation.
#: ``refused``  -> a typed refusal, terminal.
#: ``ambiguous``-> the outcome is unknown and frozen until reconciled.
#:
#: This is the managed session-operation ladder with one state inserted.
#: ``intended`` is that ladder's ``queued`` under a different name --
#: this module already uses "queue" for an operation *kind*, and one word
#: meaning two things in the same record is how a reader ends up thinking
#: a queued message and a queued operation are the same event.
#: ``posted`` is the genuinely new state, and it exists because on a
#: native TUI "we sent it" and "the provider took it" are separate facts
#: with a real gap between them.
INTENDED = "intended"
POSTED = "posted"
ACCEPTED = "accepted"
COMPLETED = "completed"
REFUSED = "refused"
AMBIGUOUS = "ambiguous"
OPERATION_STATES = frozenset({INTENDED, POSTED, ACCEPTED, COMPLETED, REFUSED, AMBIGUOUS})

#: States that need nothing further.  ``ambiguous`` is deliberately absent:
#: it is frozen, not finished, and it still blocks the session.
RESOLVED_STATES = frozenset({COMPLETED, REFUSED})

#: Typed refusal reasons.  Each names a fact that was checked, so a
#: consumer can tell "the provider said no" from "CAO would not ask".
REFUSED_ACTIVE_TURN = "active_turn_in_progress"
REFUSED_NO_ACTIVE_TURN = "no_active_turn"
REFUSED_TURN_MISMATCH = "turn_mismatch"
REFUSED_UNSUPPORTED_CONTROL = "unsupported_control"
REFUSED_ATTACHMENT = "attachment_not_owned"
REFUSED_UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"
REFUSED_PROVIDER = "provider_refused"
#: The payload is well formed but this provider build has no proven way
#: to type it.  An operational answer about the installed provider, not a
#: caller mistake -- so it is a durable typed refusal rather than a
#: raised error, and a caller whose response was lost finds it by exact
#: id instead of having to guess whether anything was sent.
REFUSED_UNPROVEN_COMPOSER_NEWLINE = "composer_newline_unproven"
#: Lane C: the operator-message pre-write hook refused at the last safe
#: pre-write point (after the journal claim and every deliverability/idle
#: gate, immediately before the transport write), so zero bytes were
#: typed.  These name the image-attachment store's answers there; the
#: word "attachment" alone already means provider-session ownership in
#: this module (``REFUSED_ATTACHMENT``).
REFUSED_IMAGE_UNKNOWN = "image_attachment_unknown"
REFUSED_IMAGE_NOT_READY = "image_attachment_not_ready"
REFUSAL_REASONS = frozenset(
    {
        REFUSED_ACTIVE_TURN,
        REFUSED_NO_ACTIVE_TURN,
        REFUSED_TURN_MISMATCH,
        REFUSED_UNSUPPORTED_CONTROL,
        REFUSED_ATTACHMENT,
        REFUSED_UNRESOLVED_AMBIGUITY,
        REFUSED_PROVIDER,
        REFUSED_UNPROVEN_COMPOSER_NEWLINE,
        REFUSED_IMAGE_UNKNOWN,
        REFUSED_IMAGE_NOT_READY,
    }
)

#: The one control command the lifecycle contract names by itself.  Every
#: other control -- including route controls -- must be advertised by the
#: provider before it can be sent, and so has no constant here.  Even
#: this one is capability-gated; it is named only because the contract
#: names it.
CONTROL_COMPACT = "/compact"

#: Characters that must never reach a provider composer through this
#: adapter.  Refusing ESC as a class is deliberate and is *not* a
#: restatement of any specific escape sequence: bracketed-paste
#: sentinels, cursor moves, and menu-opening sequences all begin with
#: ESC, so refusing the class covers them without this module keeping a
#: second copy of a sentinel vocabulary that belongs to the
#: cross-provider control-input contract.  CR and LF are refused with it
#: because they are control bytes the provider acts on rather than
#: characters it stores -- on some builds submitting the composer early,
#: on others breaking the line.  Either way a byte inside a message would
#: be deciding, which is exactly what the keystroke plan exists to take
#: away from it.
#:
#: This governs one *literal write*.  Message text may legitimately
#: contain newlines; they are delivered as composer keystrokes between
#: literal writes rather than as bytes, so no newline ever reaches a
#: literal write and this rule is unweakened by multi-line support.
_FORBIDDEN_CHARACTERS = ("\x1b", "\r", "\n")

#: How a payload was turned into keystrokes.  Recorded in the delivery
#: receipt so a reader can tell what was typed without re-deriving it,
#: and so a future encoding cannot be mistaken for this one.
ENCODING_SINGLE_LINE = "single-line-then-enter"
ENCODING_SOFT_NEWLINE = "soft-newline-lines-then-enter"

KEYSTROKE_PLAN_SCHEMA = "cao-kimi-native-keystroke-plan-v1"

#: What the pinned provider does to the composer image when it submits.
#:
#: Named and recorded rather than assumed away.  The Kimi composer's
#: ``submitValue()`` computes ``expandPasteMarkers(lines.join("\n")).trim()``
#: *before* the model sees anything (verified identical in the 0.29.0,
#: 0.29.1, and 0.29.2 bundles).  ``expandPasteMarkers`` only rewrites
#: TUI-internal paste markers, and CAO's literal keystroke path creates
#: none, so on CAO-typed content the expansion is the identity and the
#: model-visible normalization is still join-LF-then-trim: the composer
#: image and the model's input are two different strings whenever the
#: content has leading or trailing whitespace.  Both are recorded;
#: neither is presented as the other.
#:
#: Defeating this with sentinel or framing bytes is forbidden: it would
#: put artifact bytes into model input to win an argument about
#: whitespace.  The honest move is to say what the model actually got.
NORMALIZATION_JOIN_LF_THEN_TRIM = "kimi-0.29.0:join-lf+trim"

#: Composer newline keystrokes proven against one exact provider build.
#:
#: Keyed by the full version string and never by a range.  Which key
#: inserts a newline instead of submitting is a fact about one bundle;
#: a range would be this table asserting something about builds nobody
#: has read, and the failure mode of being wrong is a task submitted in
#: pieces as several turns.
#:
#: ``0.29.0`` is read from the installed bundle, which declares
#: ``"tui.input.newLine": {defaultKeys: ["shift+enter", "ctrl+j"]}`` and
#: ``"tui.input.submit": {defaultKeys: "enter"}``.  Ctrl-J is chosen over
#: shift+enter because it is one unambiguous control byte that terminals
#: transmit identically, whereas shift+enter depends on the terminal
#: emitting a modifyOtherKeys/CSI-u sequence that many do not.
#:
#: ``submit_settle_seconds`` exists because the same bundle suppresses
#: submission during a paste burst: while ``enterSuppressUntil`` is in
#: the future, Enter *inserts a newline instead of submitting*
#: (``PASTE_ENTER_SUPPRESS_WINDOW_MS = 120``).  A task typed quickly and
#: submitted immediately can therefore land in that window and never be
#: sent at all -- no error, no turn, the message left in the composer.
#: Waiting past the window closes that hole.  This is a mechanical
#: debounce required by the pinned TUI, not a substitute for observing
#: readiness: readiness is observed separately, before any typing.
#: ``burst_reset_keystroke`` is what makes the final Enter *provably*
#: submit rather than probably submit.  The same bundle resets the burst
#: on any key event that is neither Enter nor a single printable
#: character::
#:
#:     if (!disablePasteBurst && !isEnterKey && printableForBurst === void 0)
#:         this.pasteBurst.reset();
#:
#: and ``reset()`` zeroes ``enterSuppressUntil``.  So one content-neutral
#: key immediately before Enter clears the suppression window outright,
#: with no dependence on how fast the pty delivered the preceding bytes.
#: ``End`` is chosen because it cannot alter composer content and leaves
#: the cursor where Enter expects it.  The settle is kept as well: the
#: two defences fail independently, and a quarter second is cheap next
#: to silently not submitting a task at all.
#:
#: ``normalization`` names what the provider does to the composer image
#: on submit, so a receipt can state model-visible input honestly rather
#: than implying the composer bytes reached the model untouched.
_PROVEN_COMPOSER_NEWLINE: dict[str, dict[str, Any]] = {
    "0.29.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "dist/main.mjs declares tui.input.newLine defaultKeys "
            "['shift+enter', 'ctrl+j'] with tui.input.submit 'enter'; "
            "submitValue() computes expandPasteMarkers(lines.join('\\n')).trim() "
            "(the paste-marker expansion is the identity on CAO's "
            "marker-free keystroke input); "
            "PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 and reset() clears it"
        ),
    },
    # 0.29.1 is a *separate* proven entry, not a range widening: it was
    # read from the installed 0.29.1 bundle (main.mjs sha256
    # cba31835395ff75fa6b5bc9b81a7907c7d933e7e6a7d8ba53afac23dd0f5ab04),
    # whose composer keybinds, submit normalization, and paste-burst
    # window are byte-identical to 0.29.0.  Because the model-visible
    # normalization is provably the same computation, it reuses
    # NORMALIZATION_JOIN_LF_THEN_TRIM — the identifier names the proven
    # behaviour, which both builds share, not the build.
    "0.29.1": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.29.1 dist/main.mjs (sha256 cba31835395ff75fa6b5b"
            "c9b81a7907c7d933e7e6a7d8ba53afac23dd0f5ab04) declares "
            "tui.input.newLine defaultKeys ['shift+enter', 'ctrl+j'] with "
            "tui.input.submit 'enter'; submitValue() computes "
            "expandPasteMarkers(lines.join('\\n')).trim(); "
            "PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 "
            "— byte-identical to the 0.29.0 composer facts"
        ),
    },
    # 0.29.2 is likewise a *separate* proven entry: read from the
    # installed 0.29.2 bundle (main.mjs sha256
    # 2ee6e2f15d68bffdce532d1c8e50f8d40e0230090b3a0dd1dbcdb7c29bf532db,
    # matching the npm-published digest) and confirmed by the three-way
    # 0.29.0/0.29.1/0.29.2 bundle comparison — the composer keybinds, the
    # expandPasteMarkers(lines.join("\n")).trim() submit computation, the
    # 120 ms paste-burst window, and the content-neutral reset are
    # byte-identical across all three builds.  Same proven behaviour, so
    # the same normalization identifier; no behavioral drift is claimed.
    "0.29.2": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.29.2 dist/main.mjs (sha256 2ee6e2f15d68bffdce532"
            "d1c8e50f8d40e0230090b3a0dd1dbcdb7c29bf532db, matches the "
            "npm-published digest) declares tui.input.newLine defaultKeys "
            "['shift+enter', 'ctrl+j'] with tui.input.submit 'enter'; "
            "submitValue() computes expandPasteMarkers(lines.join('\\n')).trim(); "
            "PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 with content-neutral "
            "reset — byte-identical to the 0.29.0/0.29.1 composer facts "
            "(three-way bundle comparison)"
        ),
    },
    # 0.30.0 is likewise a *separate* proven entry, not a range widening:
    # read from the installed 0.30.0 bundle (main.mjs sha256
    # 49ad0553cff0b5f60f83ba85df56bb5ccdbcb908158c80d9363d0e5a529ea51c),
    # which declares the same composer facts as the 0.29.x line — the
    # ['shift+enter', 'ctrl+j'] newLine defaultKeys, the
    # expandPasteMarkers(lines.join("\n")) submit computation, and
    # PASTE_ENTER_SUPPRESS_WINDOW_MS = 120.  Same proven behaviour, so the
    # same normalization identifier; no behavioral drift is claimed.
    "0.30.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.30.0 dist/main.mjs (sha256 49ad0553cff0b5f60f83ba"
            "85df56bb5ccdbcb908158c80d9363d0e5a529ea51c) declares the "
            "['shift+enter', 'ctrl+j'] newLine defaultKeys and computes "
            "expandPasteMarkers(this.state.lines.join('\\n')) on submit; "
            "PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 "
            "— the same composer facts as the 0.29.x line (bundle read; "
            "live acceptance per cond-0198)"
        ),
    },
    # 0.31.0 is likewise a *separate* proven entry, not a range widening:
    # read from the installed 0.31.0 bundle (main.mjs sha256
    # 689fc2a123dfc3145dab26a8e6a86c71a5dc8552b13fe0449679e065ce96774e),
    # which declares the same composer facts as the 0.29.x/0.30.0 line — the
    # ['shift+enter', 'ctrl+j'] newLine defaultKeys, the 'enter' submit, the
    # expandPasteMarkers(this.state.lines.join("\\n")).trim() submit
    # computation, and PASTE_ENTER_SUPPRESS_WINDOW_MS = 120.  Same proven
    # behaviour, so the same normalization identifier; no behavioral drift is
    # claimed.  (cond-0310: bundle-read; live text/control EFFECT on the real
    # 0.31.0 build is a separate post-review acceptance gate.)
    "0.31.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.31.0 dist/main.mjs (sha256 689fc2a123dfc3145dab26a"
            "8e6a86c71a5dc8552b13fe0449679e065ce96774e) declares the "
            "['shift+enter', 'ctrl+j'] newLine defaultKeys and 'enter' "
            "submit, and computes expandPasteMarkers(this.state.lines."
            "join('\\n')).trim() on submit; PASTE_ENTER_SUPPRESS_WINDOW_MS "
            "= 120 — the same composer facts as the 0.29.x/0.30.0 line "
            "(bundle read per cond-0310)"
        ),
    },
    # 0.32.0 is likewise a *separate* proven entry, not a range widening:
    # read from the installed 0.32.0 bundle (main.mjs sha256
    # b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79,
    # matching the npm-published digest), which declares the same composer
    # facts as the 0.29.x/0.30.0/0.31.0 line — the ['shift+enter', 'ctrl+j']
    # newLine defaultKeys, the 'enter' submit, the
    # expandPasteMarkers(this.state.lines.join("\n")).trim() submit
    # computation, and PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 with the
    # content-neutral reset — each snippet verified byte-identical against
    # the npm-published 0.31.0 bundle (cond-0315; the 0.32.0 boot was also
    # observed live on a private tmux stage).  Same proven behaviour, so the
    # same normalization identifier; no behavioral drift is claimed.
    "0.32.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.32.0 dist/main.mjs (sha256 b02ebfe77dda7d9f38cf61c"
            "5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79, matches the "
            "npm-published digest) declares the ['shift+enter', 'ctrl+j'] "
            "newLine defaultKeys and 'enter' submit, and computes "
            "expandPasteMarkers(this.state.lines.join('\\n')).trim() on "
            "submit; PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 with "
            "content-neutral reset — byte-identical to the npm-published "
            "0.31.0 bundle facts (bundle read per cond-0315)"
        ),
    },
    # 0.33.0 is likewise a *separate* proven entry, not a range widening:
    # read from the installed 0.33.0 bundle (main.mjs sha256
    # 0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2,
    # matching the npm-published digest), which declares the same composer
    # facts as the 0.29.x/0.30.0/0.31.0/0.32.0 line — each snippet verified
    # byte-identical against the npm-published 0.32.0 bundle (cond-0315;
    # the 0.33.0 boot was also observed live on a private tmux stage).
    "0.33.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "installed 0.33.0 dist/main.mjs (sha256 0e77b9c64e67a4eecb96aae"
            "011750668aab11bd781564fe3e4855513812247b2, matches the "
            "npm-published digest) declares the ['shift+enter', 'ctrl+j'] "
            "newLine defaultKeys and 'enter' submit, and computes "
            "expandPasteMarkers(this.state.lines.join('\\n')).trim() on "
            "submit; PASTE_ENTER_SUPPRESS_WINDOW_MS = 120 with "
            "content-neutral reset — byte-identical to the npm-published "
            "0.32.0 bundle facts (bundle read per cond-0315)"
        ),
    },
    # 0.34.0 is admitted as a separately verified build, not as a hard-only
    # pin.  The installed bundle (sha256
    # d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c)
    # exposes the newline/submit and paste-burst facts at the same source
    # seam as the prior proven line, and its steer binding is present.  A
    # private-tmux capture also rendered the exact ACP-minted session id,
    # bound directory, and Version 0.34.0 header.  These are explicit
    # observations for this build; future builds do not inherit them.
    "0.34.0": {
        "keystroke": "C-j",
        "burst_reset_keystroke": "End",
        "submit_settle_seconds": 0.25,
        "normalization": NORMALIZATION_JOIN_LF_THEN_TRIM,
        "evidence": (
            "Kimi Code 0.34.0 dist/main.mjs sha256 "
            "d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c; "
            "source inspection found the provider newline/submit, paste-burst, "
            "and steer bindings; a private-tmux capture after ACP session/new "
            "rendered the exact session id, bound directory, and Version 0.34.0"
        ),
    },
}

#: The steer chords proven for each pinned Kimi build.  Distinct from
#: :data:`_PROVEN_COMPOSER_NEWLINE` on purpose: a steer chord replaces
#: Enter as the submit/steer effect for a v2 ``conduct steer`` control,
#: whereas the newline pin governs multi-line composer *breaks*.  Reusing
#: the newline pin would let any key proven for a line break be sent as a
#: steer effect, which is a different act with a different proof.  A build
#: appears here only when its chord behaviour was read, on the same terms
#: as the newline pin.
_PROVEN_STEER_CHORDS: dict[str, frozenset[str]] = {
    "0.29.0": frozenset({"C-s"}),
    "0.29.1": frozenset({"C-s"}),
    # Key.ctrl("s") exists in the 0.29.2 bundle exactly as in 0.29.0/0.29.1
    # (three-way comparison; sha256 keyed in _PROVEN_COMPOSER_NEWLINE).
    "0.29.2": frozenset({"C-s"}),
    # ctrl("s") exists in the installed 0.30.0 bundle exactly as in the
    # 0.29.x line (bundle read; sha256 keyed in _PROVEN_COMPOSER_NEWLINE;
    # live acceptance per cond-0198).
    "0.30.0": frozenset({"C-s"}),
    # Key.ctrl("s") exists in the installed 0.31.0 bundle exactly as in the
    # 0.29.x/0.30.0 line (bundle read per cond-0310; sha256 keyed in
    # _PROVEN_COMPOSER_NEWLINE).
    "0.31.0": frozenset({"C-s"}),
    # Key.ctrl("s") exists in the installed 0.32.0 bundle exactly as in the
    # 0.29.x/0.30.0/0.31.0 line — the matchesKey(normalized, Key.ctrl("s"))
    # dispatch is byte-identical to the npm-published 0.31.0 bundle (bundle
    # read per cond-0315; sha256 keyed in _PROVEN_COMPOSER_NEWLINE).
    "0.32.0": frozenset({"C-s"}),
    # Key.ctrl("s") exists in the installed 0.33.0 bundle exactly as in the
    # 0.29.x–0.32.0 line — the matchesKey(normalized, Key.ctrl("s"))
    # dispatch is byte-identical to the npm-published 0.32.0 bundle (bundle
    # read per cond-0315; sha256 keyed in _PROVEN_COMPOSER_NEWLINE).
    "0.33.0": frozenset({"C-s"}),
    # Key.ctrl("s") exists in the installed 0.34.0 bundle; this is a
    # build-specific source observation, not an inheritance rule for future
    # versions.
    "0.34.0": frozenset({"C-s"}),
}


def steer_chords(provider_version: Optional[str]) -> frozenset[str]:
    """The steer chords proven for this Kimi build, or an empty set.

    Looked up by the same normalized version the newline pin uses, so the
    two tables agree about which build a request names.  An empty result is
    the honest answer for an unproven build: a chord the server has not
    read evidence for is refused with zero bytes rather than sent at a
    composer on the strength of a guess.
    """
    if not provider_version:
        return frozenset()
    return _PROVEN_STEER_CHORDS.get(
        provider_contracts.normalized_version(provider_version), frozenset()
    )


def advertised_steer_chords() -> dict[str, list[str]]:
    """The steer chords this adapter advertises for discovery (per provider).

    The union over proven builds: the discovery block on the identity route
    tells a conductor which chords exist at all, before it has named a
    version, so it can fail closed against a server that offers none.  A
    conductor that names a version still gets the per-build answer from
    :func:`steer_chords`; this is the looser, earlier question.
    """
    union: set[str] = set()
    for build_chords in _PROVEN_STEER_CHORDS.values():
        union.update(build_chords)
    return {PROVIDER: sorted(union)} if union else {}


#: Characters ECMAScript ``String.prototype.trim`` removes: its
#: WhiteSpace production plus its LineTerminator production.  Listed
#: explicitly rather than reusing Python's ``str.strip``, whose set is
#: close but not equal -- and "close" here decides whether a receipt
#: claims content reached the model that in fact did not.  Unicode
#: ``Zs`` is folded in by :func:`_is_js_whitespace`.
#:
#: U+0085 NEL is deliberately absent.  It is a line terminator in several
#: other standards, which makes it the obvious thing to include by
#: analogy, but ECMAScript's WhiteSpace and LineTerminator productions
#: both exclude it -- verified against the installed node, where
#: ``String.fromCodePoint(0x85).trim().length === 1`` while every other
#: code point listed here trims away.  Including it would understate the
#: model's input by a character.
_JS_TRIM_CHARACTERS = frozenset("\t\n\x0b\x0c\r \xa0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff")


def _is_js_whitespace(char: str) -> bool:
    return char in _JS_TRIM_CHARACTERS or unicodedata.category(char) == "Zs"


def _js_trim(text: str) -> str:
    """``String.prototype.trim`` as the pinned provider applies it."""
    start, end = 0, len(text)
    while start < end and _is_js_whitespace(text[start]):
        start += 1
    while end > start and _is_js_whitespace(text[end - 1]):
        end -= 1
    return text[start:end]


def split_submission_terminator(text: str) -> tuple[str, Optional[str]]:
    """Separate the one submitting terminator from the content.

    Exactly one trailing ``\\n`` (or ``\\r\\n``, which is the same
    intent) is the *submit*, not content: it is what the explicit final
    Enter delivers.  Everything else -- including the blank line that a
    trailing ``\\n\\n`` leaves behind -- is content.

    Deliberately "exactly one", never ``rstrip``.  Stripping all of them
    would silently delete content, and this module refuses to guess how
    much of a message a caller meant.
    """
    if text.endswith("\r\n"):
        return text[:-2], "\r\n"
    if text.endswith("\n"):
        return text[:-1], "\n"
    return text, None


def plan_composer_keystrokes(
    text: str,
    *,
    provider_version: Optional[str] = None,
    field: str = "text",
) -> dict[str, Any]:
    """Decide every keystroke before a single one is sent.

    Built as a whole, up front, so the plan can be journaled before any
    composer I/O and so an undeliverable payload is refused with nothing
    typed rather than discovered halfway through a message.

    The digests are the point of this record.  ``payload_sha256`` is over
    the caller's **original** bytes and is unaffected by any encoding
    decision here, so delivery can never quietly redefine what was asked
    for.  ``composer_sha256`` is what the composer will hold before
    submit.  ``model_input_sha256`` is what the provider will actually
    hand the model after its own normalization -- recorded separately
    precisely because it is sometimes *not* the same string, and a single
    digest would have to lie about one of them.
    """
    if not isinstance(text, str) or not text:
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {text!r}")

    content, terminator = split_submission_terminator(text)
    if not content:
        raise NativeControlInvalid(
            f"{field} is only a line terminator, so there is no content to deliver; "
            f"the terminator is the submit keystroke, not a message"
        )

    lines = content.split("\n")
    for forbidden in _FORBIDDEN_CHARACTERS:
        if forbidden == "\n":
            continue
        if forbidden in content:
            raise NativeControlInvalid(
                f"{field} contains {forbidden!r}, which must never reach a provider "
                f"composer; native control types literal lines and sends composer "
                f"keystrokes for the breaks between them"
            )

    # Normalized through the one normalizer the version pin already uses.
    # The durable binding records the provider's banner verbatim --
    # "2.1.220 (Claude Code)" -- because that is what the provider printed,
    # while this table is keyed by the bare build. A raw strip therefore
    # missed on an installed, pinned, proven build and refused it as
    # unproven, with zero task bytes, even though check_pinned_version had
    # already accepted the same string at bind time by normalizing it.
    #
    # Normalizing the lookup INPUT is not loosening the pin. The keys stay
    # bare exact builds, so an unproven version still misses. Adding the
    # banner form as a second key would have turned a table of proven
    # builds into a table of spellings, and the next banner variant would
    # be unproven again.
    pin = _PROVEN_COMPOSER_NEWLINE.get(
        provider_contracts.normalized_version(provider_version or "")
    )
    undeliverable = None
    if len(lines) > 1 and pin is None:
        # Recorded on the plan rather than raised. The payload is well
        # formed; what is missing is a proven keystroke for the *installed
        # build*, which is an operational fact about this session. The
        # caller turns it into a durable typed refusal so a lost response
        # is answerable by exact id -- raising here would leave no row and
        # reproduce the maybe-sent ambiguity this whole seam exists to
        # remove. Multi-turn chunking, bracketed paste, and flattening the
        # newlines away are each a way of appearing to deliver a message
        # that was not delivered, so none of them is the alternative.
        undeliverable = (
            f"{field} spans {len(lines)} lines but no composer newline keystroke is proven "
            f"for provider version {provider_version!r}; refusing rather than splitting the "
            f"message across turns, pasting it, or flattening the newlines out of it"
        )

    composer_image = "\n".join(lines)
    normalization = None if pin is None else pin["normalization"]
    model_input = composer_image if pin is None else _js_trim(composer_image)

    plan: dict[str, Any] = {
        "schema": KEYSTROKE_PLAN_SCHEMA,
        "encoding": ENCODING_SINGLE_LINE if len(lines) == 1 else ENCODING_SOFT_NEWLINE,
        # Over the caller's original bytes, terminator included.
        "payload_sha256": canonical_sha256(text),
        "composer_sha256": canonical_sha256(composer_image),
        "model_input_sha256": canonical_sha256(model_input),
        "provider_normalization": normalization,
        # False means the provider's own submit-time normalization will
        # change the content -- leading/trailing whitespace only. Stated
        # as a fact rather than left for a reader to infer from digests.
        "model_input_is_composer_exact": model_input == composer_image,
        "line_count": len(lines),
        "trailing_terminator": terminator,
        "provider_version": provider_version,
        "soft_newline_keystroke": None if pin is None else pin["keystroke"],
        "burst_reset_keystroke": None if pin is None else pin.get("burst_reset_keystroke"),
        "submit_settle_seconds": 0.0 if pin is None else float(pin["submit_settle_seconds"]),
        # What was actually read out of this build to justify the
        # keystrokes above, carried on the plan and therefore journaled
        # with it. Present here for the same reason as on the Claude plan:
        # a receipt that names a keystroke without naming the evidence for
        # it is a claim, and this adapter's whole contract is that its
        # composer facts were read rather than assumed.
        #
        # It was absent until a consumer asked both adapters the same
        # question and got a different answer for each -- which is the
        # kind of asymmetry that reads as "unproven build" for exactly one
        # provider and refuses every healthy generation it has.
        "composer_evidence": None if pin is None else pin["evidence"],
        "final_enter": True,
        "deliverable": undeliverable is None,
        "undeliverable_reason": undeliverable,
    }
    # Bound to the plan's own contents, which already carry the payload
    # digest -- so the plan digest identifies the exact keystrokes without
    # this record ever storing the message text.
    plan["plan_sha256"] = canonical_sha256(_canonical(plan))
    plan["lines"] = lines
    return plan


def _journalable_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan without the message text, for durable storage."""
    return {key: value for key, value in plan.items() if key != "lines"}


class NativeControlError(RuntimeError):
    """Base class for every native control failure."""

    code = "kimi-native-control-error"


class NativeControlInvalid(NativeControlError):
    """The request is malformed; nothing was journaled and nothing sent.

    Distinct from a refusal on purpose.  A refusal is an operational
    answer that is worth keeping ("the provider was busy"); this is a
    caller bug, and durably recording it as an operation would put
    fiction in the evidence trail.
    """

    code = "kimi-native-control-invalid"


class NativeControlConflict(NativeControlError):
    """A caller-minted id was reused for a materially different request."""

    code = "kimi-native-control-conflict"


class NativeControlNotFound(NativeControlError):
    """No operation exists for the given id."""

    code = "kimi-native-control-not-found"


class NativeControlUnavailable(NativeControlError):
    """The durable store could not be read or written; fail closed."""

    code = "kimi-native-control-unavailable"


class NativeControlTransport(Protocol):
    """The pane-writing capability this adapter borrows.

    Two methods rather than one, because the contract requires the
    submitting keystroke to be explicit.  A single ``send(text)`` would
    let a trailing newline inside the payload do the submitting, which is
    exactly the accident that turns a half-typed message into a sent one.

    Neither method returns anything meaningful.  That is the point: there
    is no value a transport could return that would constitute provider
    acceptance, so the interface offers none to be misread.
    """

    def send_literal(self, text: str) -> None:
        """Write ``text`` to the pane exactly, with no paste wrapper."""

    def send_enter(self) -> None:
        """Send the submitting key as its own separate keystroke."""

    def send_key(self, keystroke: str) -> None:
        """Send one named, non-submitting key: a composer newline or a
        key a provider pin requires before submitting.

        Only reached for payloads whose plan needs it, so a transport
        that predates multi-line delivery keeps working for the
        single-line payloads it already handled.
        """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def assert_artifact_free(text: str, *, field: str = "text") -> str:
    """Return ``text`` if it can be typed literally with no artifacts.

    Refuses rather than strips.  Stripping would silently change the
    message a human asked to send, and a payload carrying an escape
    sequence means the caller built it through a paste path this adapter
    exists to replace -- a fact worth surfacing, not sanitizing away.
    """
    if not isinstance(text, str) or not text:
        raise NativeControlInvalid(f"{field} must be a non-empty string; got {text!r}")
    for forbidden in _FORBIDDEN_CHARACTERS:
        if forbidden in text:
            raise NativeControlInvalid(
                f"{field} contains {forbidden!r}, which must never reach a provider "
                f"composer; native control types one literal line and sends Enter separately"
            )
    return text


def turn_observation(
    *,
    active_turn_id: Optional[str],
    observed_at: str,
    observer: str,
) -> dict[str, Any]:
    """Build the idle/busy observation that gates a queue or a steer.

    ``active_turn_id`` is required and may be ``None``; ``None`` means
    "observed idle", not "did not look".  The distinction is the whole
    value of the field: an omitted observation and an idle one are
    indistinguishable once stored, and "we did not check" must never be
    able to satisfy an idle gate.

    This adapter never derives the observation itself.  Deciding whether
    a TUI is mid-turn is a live-surface judgement belonging to whatever
    watches the pane; a guess manufactured here would be the least
    informed one in the system.
    """
    if active_turn_id is not None:
        active_turn_id = _require_text(active_turn_id, field="active_turn_id")
    return {
        "schema": TURN_OBSERVATION_SCHEMA,
        "active_turn_id": active_turn_id,
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
    }


def provider_observation(
    *,
    operation_id: str,
    observed_at: str,
    observer: str,
    evidence: Mapping[str, Any],
    entered_turn_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the provider-side fact that can move an operation forward.

    Carries ``operation_id`` inside the observation, not merely alongside
    it, so resolving a lost response requires evidence that names the
    exact operation.  An observation that only says "something was
    accepted" cannot be pointed at whichever operation is convenient.
    """
    if not isinstance(evidence, Mapping) or not evidence:
        raise NativeControlInvalid("evidence must be a non-empty mapping")
    if entered_turn_id is not None:
        entered_turn_id = _require_text(entered_turn_id, field="entered_turn_id")
    return {
        "schema": PROVIDER_OBSERVATION_SCHEMA,
        "operation_id": _require_text(operation_id, field="operation_id"),
        "observed_at": _require_text(observed_at, field="observed_at"),
        "observer": _require_text(observer, field="observer"),
        "entered_turn_id": entered_turn_id,
        "evidence": dict(evidence),
    }


def _intent(
    *,
    kind: str,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    turn_id: Optional[str],
    payload_sha256: str,
    turn_observation_record: Mapping[str, Any],
    keystroke_plan: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The record written before anything is typed.

    Everything needed to adjudicate a crash between this write and the
    keystroke is here: which operation, against which exact session and
    generation, in which mode, with which payload, what the pane looked
    like at the moment the decision was made, and -- for text payloads --
    the exact keystroke plan that was about to be executed.

    The plan is journaled *before* composer I/O rather than recorded
    after it, because its whole purpose is to be readable when the write
    did not finish.  It carries digests and shape, never the message
    text: a recovery reader needs to know what was going to be typed,
    not to hold a second copy of it.
    """
    return {
        "schema": INTENT_SCHEMA,
        "kind": kind,
        "operation_id": operation_id,
        "provider": PROVIDER,
        "native_session_id": native_session_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": execution_mode,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "turn_observation": dict(turn_observation_record),
        "keystroke_plan": None if keystroke_plan is None else _journalable_plan(keystroke_plan),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    """Project one operation, keeping transport and provider facts apart."""
    return {
        "schema": RECORD_SCHEMA,
        "operation_id": row.operation_id,
        "kind": row.kind,
        "state": row.state,
        "provider": row.provider,
        "native_session_id": row.native_session_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "execution_mode": row.execution_mode,
        "turn_id": row.turn_id,
        "payload_sha256": row.payload_sha256,
        "intent": _parse_json(row.intent_json),
        # Transport truth and provider truth are separate keys that are
        # never derived from one another. ``posted`` says bytes were
        # written; only ``provider_accepted`` says the provider took them.
        "posted": row.posted_at is not None,
        "posted_at": row.posted_at,
        "transport": _parse_json(row.transport_json),
        "provider_accepted": row.state in {ACCEPTED, COMPLETED},
        "provider_completed": row.state == COMPLETED,
        "observation": _parse_json(row.observation_json),
        "refusal_reason": row.refusal_reason,
        "ambiguity_reason": row.ambiguity_reason,
        "is_resolved": row.state in RESOLVED_STATES,
        "epoch": row.epoch,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _fetch(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.KimiNativeControlOperationModel)
        .filter(database.KimiNativeControlOperationModel.operation_id == operation_id)
        .one_or_none()
    )


def _assert_same_request(row: Any, *, kind: str, binding: Mapping[str, Any]) -> None:
    """Refuse a reused operation id that carries different content.

    Replaying the identical request is how a caller recovers from a lost
    response, so it must be free.  Reusing the id for different bytes or
    a different session is the opposite: it would overwrite the evidence
    of what was actually sent.
    """
    mismatches = [
        name
        for name, expected in (("kind", kind), *binding.items())
        if getattr(row, name) != expected
    ]
    if mismatches:
        raise NativeControlConflict(
            f"operation {row.operation_id} already exists with a different "
            f"{', '.join(sorted(mismatches))}; a caller-minted id is immutable"
        )


def _assert_session_unblocked(*, native_session_id: str, operation_id: str) -> None:
    """Refuse a new operation while an earlier one is unresolved.

    An ambiguous operation may or may not have reached the provider.
    Sending anything further would make the transcript unreadable: if the
    ambiguous input did land, the session now holds two instructions
    whose order nobody can reconstruct.  So the session is held until the
    ambiguity is resolved by exact id.

    Checked after the intent is journaled, alongside the ownership check,
    so the blocked attempt leaves a durable typed refusal rather than
    vanishing as a raised error.  A caller that later asks why nothing
    happened finds the record.
    """
    try:
        with database.SessionLocal() as db:
            blocking = (
                db.query(database.KimiNativeControlOperationModel)
                .filter(
                    database.KimiNativeControlOperationModel.native_session_id == native_session_id,
                    database.KimiNativeControlOperationModel.state == AMBIGUOUS,
                    database.KimiNativeControlOperationModel.operation_id != operation_id,
                )
                .first()
            )
            blocking_id = None if blocking is None else blocking.operation_id
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not check for unresolved ambiguity: {exc}") from exc

    if blocking_id is not None:
        raise _Refusal(
            REFUSED_UNRESOLVED_AMBIGUITY,
            f"operation {blocking_id} on session {native_session_id} is ambiguous; "
            f"it must be reconciled by exact id before further input is sent",
        )


class _Refusal(Exception):
    """Internal signal: a durable, typed refusal rather than a caller bug."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _assert_attachment_owner(
    *,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> None:
    """Require this exact owner to hold the session, attached, right now.

    Control input is a side effect on a live provider session, so it
    borrows the same exclusive-ownership record that governs attaching to
    one.  Checking here rather than trusting the caller's binding is what
    stops an operation minted for a previous generation from typing into
    the pane that replaced it.
    """
    try:
        record = native_attachment.get(PROVIDER, native_session_id)
    except native_attachment.NativeAttachmentError as exc:
        raise NativeControlUnavailable(
            f"could not read the attachment record for {PROVIDER} session "
            f"{native_session_id}: {exc}"
        ) from exc

    if record is None:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"no attachment record for {PROVIDER} session {native_session_id}; "
            f"native control requires a live owned attachment",
        )
    if record["state"] != native_attachment.ATTACHED:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"{PROVIDER} session {native_session_id} is {record['state']!r}, not "
            f"{native_attachment.ATTACHED!r}; only an attached session accepts control input",
        )
    owner = record["owner"]
    actual = (
        owner["terminal_id"],
        owner["generation"],
        owner["execution_mode"],
    )
    expected = (terminal_id, generation, execution_mode)
    if actual != expected:
        raise _Refusal(
            REFUSED_ATTACHMENT,
            f"{PROVIDER} session {native_session_id} is held by {actual}, not {expected}; "
            f"control input is refused rather than delivered to another owner's pane",
        )


def _validate_binding(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
) -> dict[str, Any]:
    """Validate the identity every operation carries, before any effect."""
    try:
        mode = em.validate_mode(execution_mode)
    except em.ExecutionModeError as exc:
        # Re-typed rather than propagated so a caller of this module needs
        # to catch exactly one error family; the original message, which
        # names the closed mode set, is preserved.
        raise NativeControlInvalid(str(exc)) from exc
    if mode != em.NATIVE_TUI:
        raise NativeControlInvalid(
            f"native control requires execution_mode {em.NATIVE_TUI!r}; got {mode!r}. "
            f"An ACP session is controlled over its own receipt-bearing path, never by "
            f"typing into a pane"
        )
    return {
        "operation_id": _require_text(operation_id, field="operation_id"),
        "provider": PROVIDER,
        "native_session_id": _require_text(native_session_id, field="native_session_id"),
        "terminal_id": _require_text(terminal_id, field="terminal_id"),
        "generation": _require_text(generation, field="generation"),
        "execution_mode": mode,
    }


def _open(
    *,
    kind: str,
    binding: Mapping[str, Any],
    turn_id: Optional[str],
    payload_sha256: str,
    observation: Mapping[str, Any],
    keystroke_plan: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], bool]:
    """Journal the intent, or return the existing operation unchanged.

    Returns ``(record, is_new)``.  ``is_new`` is False for a replay, and
    a replay never reaches the transport -- that is where at-most-once
    lives.  The row is committed before this returns, so a crash on the
    very next instruction still leaves the intent durable.
    """
    row_values = {
        **binding,
        "kind": kind,
        "state": INTENDED,
        "turn_id": turn_id,
        "payload_sha256": payload_sha256,
        "intent_json": _canonical(
            _intent(
                kind=kind,
                operation_id=cast(str, binding["operation_id"]),
                native_session_id=cast(str, binding["native_session_id"]),
                terminal_id=cast(str, binding["terminal_id"]),
                generation=cast(str, binding["generation"]),
                execution_mode=cast(str, binding["execution_mode"]),
                turn_id=turn_id,
                payload_sha256=payload_sha256,
                turn_observation_record=observation,
                keystroke_plan=keystroke_plan,
            )
        ),
        "epoch": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    operation_id = cast(str, binding["operation_id"])

    try:
        with database.SessionLocal() as db:
            existing = _fetch(db, operation_id)
            if existing is not None:
                _assert_same_request(
                    existing,
                    kind=kind,
                    binding={**binding, "turn_id": turn_id, "payload_sha256": payload_sha256},
                )
                return _row_dict(existing), False

            db.add(database.KimiNativeControlOperationModel(**row_values))
            db.commit()
            return _row_dict(_fetch(db, operation_id)), True
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        # A primary-key collision means a concurrent caller opened the
        # same operation first. That is a replay, not a failure: re-read
        # and let the identity check decide.
        try:
            with database.SessionLocal() as db:
                existing = _fetch(db, operation_id)
                if existing is not None:
                    _assert_same_request(
                        existing,
                        kind=kind,
                        binding={**binding, "turn_id": turn_id, "payload_sha256": payload_sha256},
                    )
                    return _row_dict(existing), False
        except NativeControlError:
            raise
        except Exception:  # noqa: BLE001 - the original failure is the real one
            pass
        raise NativeControlUnavailable(f"could not journal control intent: {exc}") from exc


def _update(
    *,
    operation_id: str,
    from_states: frozenset[str],
    to_state: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """CAS one operation forward, refusing any regression or lost update."""
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, operation_id)
            if row is None:
                raise NativeControlNotFound(f"no control operation {operation_id}")
            if row.state not in from_states:
                raise NativeControlConflict(
                    f"operation {operation_id} is {row.state!r}; {to_state!r} requires one of "
                    f"{sorted(from_states)}"
                )
            observed_epoch = row.epoch
            values: dict[str, Any] = {
                "state": to_state,
                "epoch": observed_epoch + 1,
                "updated_at": _now(),
            }
            values.update(extra or {})
            updated = (
                db.query(database.KimiNativeControlOperationModel).filter(
                    database.KimiNativeControlOperationModel.operation_id == operation_id,
                    database.KimiNativeControlOperationModel.epoch == observed_epoch,
                )
                # ``Query.update`` takes column-name keys, but its parameter
                # type is a union and ``dict`` is invariant in its key type,
                # so a ``dict[str, Any]`` is not assignable to it. Every key
                # below is supplied as a column-name literal.
                .update(cast("dict[Any, Any]", values), synchronize_session=False)
            )
            db.commit()
            if updated != 1:
                current = _fetch(db, operation_id)
                raise NativeControlConflict(
                    f"concurrent modification of operation {operation_id}; expected epoch "
                    f"{observed_epoch}, now {current.epoch} in state {current.state!r}"
                )
            return _row_dict(_fetch(db, operation_id))
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"control operation update failed: {exc}") from exc


def _refuse(operation_id: str, refusal: _Refusal) -> dict[str, Any]:
    """Record a typed refusal against an operation that never got posted."""
    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED}),
        to_state=REFUSED,
        extra={
            "refusal_reason": refusal.reason,
            "observation_json": _canonical({"detail": refusal.detail}),
        },
    )


def _assert_deliverable(plan: Mapping[str, Any]) -> None:
    """Refuse a payload this provider build cannot be made to accept.

    Checked after ownership, because "you do not hold this session" is a
    more fundamental answer, and before the idle gate, because a busy
    provider is a transient condition while an unproven keystroke is a
    permanent one -- reporting the transient reason would invite a retry
    that can never succeed.
    """
    if not plan.get("deliverable", True):
        raise _Refusal(
            REFUSED_UNPROVEN_COMPOSER_NEWLINE,
            cast(str, plan["undeliverable_reason"]),
        )


class ComposerWriteInterrupted(NativeControlError):
    """A keystroke plan stopped part-way, so what landed is not known.

    Carries the boundary it stopped at, because "typed but not
    submitted" is a real recoverable state while "may have submitted" is
    not, and a caller that cannot tell them apart has to assume the
    worse one.
    """

    def __init__(self, detail: str, *, enter_attempted: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.enter_attempted = enter_attempted


def execute_composer_plan(
    *,
    plan: Mapping[str, Any],
    transport: NativeControlTransport,
    submit: bool = True,
    deadline_monotonic: Optional[float] = None,
) -> dict[str, Any]:
    """Type one already-planned payload into a composer, then submit it.

    The provider-specific half of every native write, and deliberately
    free of any store: the caller journals the outcome in whatever record
    is its own at-most-once authority.  Native task admission journals it
    on the reservation's control operation; the identity-bound
    control-input path journals it on the control request.  Two callers,
    one keystroke sequence — because the sequence is where the proven
    composer newline, the paste-burst reset and the submit settle live,
    and a second copy of it would be a second set of composer facts to
    keep true.

    Exactly one Enter is sent, ever, for exactly one provider turn.  The
    line breaks inside a multi-line payload are composer keystrokes, not
    submissions, so a message with ten newlines is still one turn.

    ``submit=False`` types the payload and stops, without the pre-submit
    reset: that keystroke exists to make an Enter land, so sending it
    where no Enter follows would be a keystroke at a composer for no
    reason.

    Raises:
        NativeControlInvalid: The plan is undeliverable.  Nothing typed.
        ComposerWriteInterrupted: The transport raised part-way through.
    """
    if not plan.get("deliverable", True):
        # Belt and braces: reaching the transport with an undeliverable
        # plan would be a bug in this module, not a caller error, and it
        # must not be discovered by half-typing a message.
        raise NativeControlInvalid(
            f"refusing to type an undeliverable plan: {plan['undeliverable_reason']}"
        )
    lines = list(plan["lines"])
    newline_key = plan["soft_newline_keystroke"]
    index = 0

    try:
        for index, line in enumerate(lines):
            if index:
                # The break comes first, so a blank content line is a
                # break with nothing typed after it rather than a
                # special case that could be silently dropped.
                transport.send_key(newline_key)
            if line:
                transport.send_literal(line)
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        raise ComposerWriteInterrupted(
            f"transport raised while typing line {index + 1} of {len(lines)}: {exc}; "
            f"no Enter was sent, so the composer may hold a partial message",
            enter_attempted=False,
        ) from exc

    if not submit:
        return {"lines_typed": len(lines), "enter_sent": False}

    submit_composer_plan(
        plan=plan,
        transport=transport,
        deadline_monotonic=deadline_monotonic,
    )
    return {"lines_typed": len(lines), "enter_sent": True}


def submit_composer_plan(
    *,
    plan: Mapping[str, Any],
    transport: NativeControlTransport,
    deadline_monotonic: Optional[float] = None,
) -> None:
    """Submit text already typed from ``plan`` with Kimi's proven boundary.

    This is split from :func:`execute_composer_plan` so a caller that must
    observe the text resting in Kimi's composer can type with
    ``submit=False``, make that observation, and then cross the exact same
    reset/settle/Enter boundary.  It never types payload content and sends
    exactly one Enter.
    """
    # Clear the provider's paste-burst window before submitting. Without
    # this the final Enter can be swallowed and turned into yet another
    # composer newline -- no error, no turn, the task simply never sent.
    # The reset key is content-neutral; the settle covers the same window
    # by a second, independent route.
    reset_key = plan.get("burst_reset_keystroke")
    settle = float(plan.get("submit_settle_seconds") or 0.0)
    try:
        if reset_key:
            transport.send_key(reset_key)
        if settle > 0:
            remaining = (
                None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                raise TimeoutError("overall write deadline expired before submit settle")
            if remaining is not None and settle > remaining:
                time.sleep(remaining)
                raise TimeoutError("overall write deadline expired during submit settle")
            time.sleep(settle)
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        raise ComposerWriteInterrupted(
            f"the payload was typed but the pre-submit keystroke raised: {exc}; "
            f"the composer may hold unsubmitted text",
            enter_attempted=False,
        ) from exc

    try:
        transport.send_enter()
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        raise ComposerWriteInterrupted(
            f"payload was written but the submitting Enter raised: {exc}; "
            f"the composer may hold unsubmitted text",
            enter_attempted=True,
        ) from exc


def _post(
    *,
    operation_id: str,
    plan: Mapping[str, Any],
    transport: NativeControlTransport,
) -> dict[str, Any]:
    """Execute the keystroke plan, then record what this adapter did.

    Any transport failure -- at any boundary -- becomes ambiguous rather
    than failed.  A raised exception does not prove the bytes did not
    land, and treating it as proof of non-delivery is precisely what
    would justify a retry and produce a duplicate.  The ambiguity says
    which side of the Enter boundary was reached, because "typed but not
    submitted" is a real, recoverable state while "may have submitted" is
    not.
    """
    try:
        execute_composer_plan(plan=plan, transport=transport)
    except ComposerWriteInterrupted as exc:
        return mark_ambiguous(operation_id=operation_id, reason=exc.detail)

    return _update(
        operation_id=operation_id,
        from_states=frozenset({INTENDED}),
        to_state=POSTED,
        extra={
            "posted_at": _now(),
            "transport_json": _canonical(
                {
                    # Recorded as observed facts about what this adapter
                    # did, never as a claim about the provider.
                    "enter_sent_separately": True,
                    "enter_count": 1,
                    "transport_contract": "literal-lines-composer-breaks-then-explicit-enter",
                    "encoding": plan["encoding"],
                    "plan_sha256": plan["plan_sha256"],
                    "payload_sha256": plan["payload_sha256"],
                    "composer_sha256": plan["composer_sha256"],
                    # What the provider hands the model after its own
                    # submit-time normalization. Separate from the
                    # composer digest because for content with leading or
                    # trailing whitespace they genuinely differ, and one
                    # digest would have to misdescribe one of them.
                    "model_input_sha256": plan["model_input_sha256"],
                    "provider_normalization": plan["provider_normalization"],
                    "model_input_is_composer_exact": plan["model_input_is_composer_exact"],
                    "line_count": plan["line_count"],
                    "payload_single_line": plan["line_count"] == 1,
                }
            ),
        },
    )


def queue(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    text: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Send ordinary follow-up, gated on the provider being idle.

    Refuses while a turn is running rather than queueing optimistically.
    Kimi's own queue is the thing being honoured here: text typed during
    an active turn is not reliably queued, it is as likely to be consumed
    by whatever the TUI is showing, and a follow-up that lands mid-turn
    changes the running work instead of following it.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    plan = plan_composer_keystrokes(text, provider_version=provider_version, field="text")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_QUEUE,
        binding=binding,
        turn_id=None,
        payload_sha256=plan["payload_sha256"],
        observation=observed,
        keystroke_plan=plan,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        _assert_deliverable(plan)
        if observed["active_turn_id"] is not None:
            raise _Refusal(
                REFUSED_ACTIVE_TURN,
                f"turn {observed['active_turn_id']} is active; ordinary follow-up waits for "
                f"idle. A deliberate mid-turn message is a steer, requested explicitly",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def operator_message(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    text: str,
    payload_sha256: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
    pre_write: Optional[Callable[[], Optional[Tuple[str, str]]]] = None,
) -> dict[str, Any]:
    """Submit one operator message (long text, multi-line, image references).

    Lane C's typed operation (design §8.3): the same journaling, gating,
    and at-most-once discipline as :func:`queue`, with two deliberate
    differences:

    - ``payload_sha256`` is supplied by the caller and digests the *whole
      request* (draft text + attachment ids + token map), not just the
      provider-bound text.  The replay identity of an operator message is
      everything the operator sent, so a same-id request whose attachments
      changed is a conflict, not a replay.
    - ``text`` arrives already reference-substituted by the
      operator-message service (staged image paths per the registry's
      ``reference_template``).  This adapter types what it is given; which
      bytes name an image is the service's contract.

    The caller (operator-message service) holds the pane-input lease, has
    already re-proven identity under it, and gates idle itself; the
    observed-idle assertion here is the same defense in depth queue keeps.

    ``pre_write`` is the caller's last-safe-point hook (Lane C r1: the
    image-attachment ``ready → submitted(operation_id)`` binding).  It runs
    after the journal claim and every deliverability/idle gate, immediately
    before the transport write.  Its answer is ``None`` to proceed or a
    ``(refusal_reason, detail)`` pair — one of the ``REFUSED_IMAGE_*``
    reasons — which is journaled as a typed refusal with zero bytes typed.
    Once the hook succeeds the write proceeds; an ambiguity after that
    point freezes the operation exactly as any post-claim uncertainty does.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    plan = plan_composer_keystrokes(text, provider_version=provider_version, field="text")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_OPERATOR_MESSAGE,
        binding=binding,
        turn_id=None,
        payload_sha256=payload_sha256,
        observation=observed,
        keystroke_plan=plan,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        _assert_deliverable(plan)
        if observed["active_turn_id"] is not None:
            raise _Refusal(
                REFUSED_ACTIVE_TURN,
                f"turn {observed['active_turn_id']} is active; an operator message waits "
                "for idle, exactly as the control-input readiness gate requires",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    if pre_write is not None:
        hook_refusal = pre_write()
        if hook_refusal is not None:
            reason, detail = hook_refusal
            if reason not in (REFUSED_IMAGE_UNKNOWN, REFUSED_IMAGE_NOT_READY):
                raise NativeControlInvalid(
                    f"the pre-write hook answered with refusal reason {reason!r}, "
                    "which is not one of the image-binding refusal reasons"
                )
            return _refuse(binding["operation_id"], _Refusal(reason, detail))

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def steer(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    turn_id: str,
    text: str,
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Deliberately steer one exact active turn.

    Binds to ``turn_id`` and refuses if the observed turn is a different
    one or if nothing is running.  Without that binding a steer written
    for a turn that ended in the meantime would land in whatever turn
    started next -- arriving as an instruction about work it was never
    about.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    plan = plan_composer_keystrokes(text, provider_version=provider_version, field="text")
    target_turn = _require_text(turn_id, field="turn_id")
    observed = _validated_turn_observation(observation)

    record, is_new = _open(
        kind=KIND_STEER,
        binding=binding,
        turn_id=target_turn,
        payload_sha256=plan["payload_sha256"],
        observation=observed,
        keystroke_plan=plan,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        _assert_deliverable(plan)
        active = observed["active_turn_id"]
        if active is None:
            raise _Refusal(
                REFUSED_NO_ACTIVE_TURN,
                f"steer {operation_id} targets turn {target_turn} but the session was observed "
                f"idle; a steer with nothing to steer is refused, not downgraded to follow-up",
            )
        if active != target_turn:
            raise _Refusal(
                REFUSED_TURN_MISMATCH,
                f"steer {operation_id} targets turn {target_turn} but turn {active} is running; "
                f"the intended turn has already ended",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def control(
    *,
    operation_id: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    command: str,
    advertised_commands: Sequence[str],
    observation: Mapping[str, Any],
    transport: NativeControlTransport,
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Send a literal slash command, with an explicit separate Enter.

    ``advertised_commands`` is required and is the provider's own current
    capability list.  Nothing is hardcoded as universally supported --
    not even ``/compact`` -- because a command this build does not have
    is not a slash command to Kimi, it is a line of text, and sending it
    blind would post that text into the conversation as if it were a
    message.  An unadvertised command is a typed refusal with zero bytes
    written.
    """
    binding = _validate_binding(
        operation_id=operation_id,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
    )
    payload = assert_artifact_free(command, field="command")
    if not payload.startswith("/"):
        raise NativeControlInvalid(
            f"control command {payload!r} does not start with '/'; ordinary text is sent "
            f"through queue() or steer(), which gate it correctly"
        )
    if isinstance(advertised_commands, (str, bytes)) or not isinstance(
        advertised_commands, Sequence
    ):
        # A bare string would pass a substring test and quietly advertise
        # every command that happens to be a prefix of another.
        raise NativeControlInvalid(
            "advertised_commands must be a sequence of command strings from the provider's "
            "own capability list, not a single string"
        )
    observed = _validated_turn_observation(observation)

    # A slash command stays one literal line: the assertion above already
    # refused any newline, so this plan is always single-line and shares
    # the one executor rather than keeping a second typing path alive.
    plan = plan_composer_keystrokes(payload, provider_version=provider_version, field="command")
    record, is_new = _open(
        kind=KIND_CONTROL,
        binding=binding,
        turn_id=None,
        payload_sha256=plan["payload_sha256"],
        observation=observed,
        keystroke_plan=plan,
    )
    if not is_new:
        return record

    try:
        _assert_session_unblocked(
            native_session_id=binding["native_session_id"],
            operation_id=binding["operation_id"],
        )
        _assert_attachment_owner(
            native_session_id=binding["native_session_id"],
            terminal_id=binding["terminal_id"],
            generation=binding["generation"],
            execution_mode=binding["execution_mode"],
        )
        _assert_deliverable(plan)
        if payload not in set(advertised_commands):
            raise _Refusal(
                REFUSED_UNSUPPORTED_CONTROL,
                f"{payload!r} is not advertised by this session "
                f"({sorted(set(advertised_commands))}); sending it would post it as chat text",
            )
    except _Refusal as refusal:
        return _refuse(binding["operation_id"], refusal)

    return _post(operation_id=binding["operation_id"], plan=plan, transport=transport)


def _validated_turn_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Require a well-formed, present observation before gating on it."""
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("observation must be a mapping built by turn_observation()")
    if observation.get("schema") != TURN_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"observation schema must be {TURN_OBSERVATION_SCHEMA!r}; got "
            f"{observation.get('schema')!r}"
        )
    if "active_turn_id" not in observation:
        raise NativeControlInvalid(
            "observation must carry active_turn_id (None means observed idle); an absent key "
            "cannot be distinguished from an observation that was never made"
        )
    return dict(observation)


def record_observation(
    *,
    operation_id: str,
    observation: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any]:
    """Record the provider-side fact that resolves a posted operation.

    This is the only way an operation becomes accepted, completed, or
    provider-refused.  It requires an observation naming this exact
    operation id, so nothing here can be satisfied by a general "the
    provider seems fine" signal.
    """
    if outcome not in {ACCEPTED, COMPLETED, REFUSED}:
        raise NativeControlInvalid(
            f"outcome must be one of {sorted({ACCEPTED, COMPLETED, REFUSED})}; got {outcome!r}"
        )
    evidence = _validated_provider_observation(observation, operation_id=operation_id)

    from_states = {
        ACCEPTED: frozenset({POSTED}),
        COMPLETED: frozenset({POSTED, ACCEPTED}),
        REFUSED: frozenset({POSTED, ACCEPTED}),
    }[outcome]
    extra: dict[str, Any] = {"observation_json": _canonical(evidence)}
    if outcome == REFUSED:
        extra["refusal_reason"] = REFUSED_PROVIDER
    return _update(
        operation_id=operation_id,
        from_states=from_states,
        to_state=outcome,
        extra=extra,
    )


def _validated_provider_observation(
    observation: Mapping[str, Any], *, operation_id: str
) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise NativeControlInvalid("observation must be a mapping built by provider_observation()")
    if observation.get("schema") != PROVIDER_OBSERVATION_SCHEMA:
        raise NativeControlInvalid(
            f"observation schema must be {PROVIDER_OBSERVATION_SCHEMA!r}; got "
            f"{observation.get('schema')!r}"
        )
    if observation.get("operation_id") != operation_id:
        raise NativeControlInvalid(
            f"observation names operation {observation.get('operation_id')!r} but was offered "
            f"for {operation_id!r}; evidence must name the exact operation it resolves"
        )
    return dict(observation)


def mark_ambiguous(*, operation_id: str, reason: str) -> dict[str, Any]:
    """Freeze an operation whose outcome cannot be known.

    Reachable from every pre-resolution state, including ``intended``: a
    crash between journaling and typing is exactly as unknown as a lost
    response after it.  Ambiguity is idempotent so a repeated report does
    not conflict, and it never overwrites a resolved outcome.
    """
    detail = _require_text(reason, field="reason")
    try:
        return _update(
            operation_id=operation_id,
            from_states=frozenset({INTENDED, POSTED, ACCEPTED, AMBIGUOUS}),
            to_state=AMBIGUOUS,
            extra={"ambiguity_reason": detail},
        )
    except NativeControlConflict as exc:
        raise NativeControlConflict(
            f"operation {operation_id} is already resolved and cannot become ambiguous: {exc}"
        ) from exc


def reconcile(
    *,
    operation_id: str,
    observation: Mapping[str, Any],
    outcome: str,
) -> dict[str, Any]:
    """Resolve an ambiguous operation by exact-id evidence only.

    The single lawful exit from ambiguity, and deliberately not a retry:
    it takes evidence and changes a record, and it never sends anything.
    A caller that cannot obtain evidence naming this operation leaves it
    ambiguous, which keeps the session blocked -- the correct outcome,
    because the alternative is guessing whether the earlier input landed.
    """
    if outcome not in {ACCEPTED, COMPLETED, REFUSED}:
        raise NativeControlInvalid(
            f"outcome must be one of {sorted({ACCEPTED, COMPLETED, REFUSED})}; got {outcome!r}"
        )
    evidence = _validated_provider_observation(observation, operation_id=operation_id)
    extra: dict[str, Any] = {"observation_json": _canonical(evidence)}
    if outcome == REFUSED:
        extra["refusal_reason"] = REFUSED_PROVIDER
    return _update(
        operation_id=operation_id,
        from_states=frozenset({AMBIGUOUS}),
        to_state=outcome,
        extra=extra,
    )


def get(operation_id: str) -> Optional[dict[str, Any]]:
    """Return one operation, or ``None`` if the id is unknown."""
    try:
        with database.SessionLocal() as db:
            row = _fetch(db, _require_text(operation_id, field="operation_id"))
            return None if row is None else _row_dict(row)
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read control operation: {exc}") from exc


def unresolved_ambiguity(native_session_id: str) -> Optional[dict[str, Any]]:
    """Return the ambiguous operation blocking a session, if any.

    Exposed so a caller can see *why* it is blocked and go find the
    evidence, rather than discovering the block only as a refusal.
    """
    try:
        with database.SessionLocal() as db:
            row = (
                db.query(database.KimiNativeControlOperationModel)
                .filter(
                    database.KimiNativeControlOperationModel.native_session_id
                    == _require_text(native_session_id, field="native_session_id"),
                    database.KimiNativeControlOperationModel.state == AMBIGUOUS,
                )
                .first()
            )
            return None if row is None else _row_dict(row)
    except NativeControlError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise NativeControlUnavailable(f"could not read control operations: {exc}") from exc
