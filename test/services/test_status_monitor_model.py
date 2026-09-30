"""Model-based randomized check of StatusMonitor's turn protocol (#735, PR #812).

Point tests only cover the orders someone thought of; review rounds of PR #812 kept
finding interleavings nobody had (a stale read landing after a dispatch, a special
key closing past an open send, a failed send finishing an accepted one). This drives
the REAL StatusMonitor through its real read paths with random, seeded event orders
against a fake agent that holds the ground truth, and checks two rules after every
step:

  EARLY  no successfully delivered message is reported done before an answer that
         covers it exists. Input sent while the agent is busy is folded into the
         running turn (claude_code and kiro-cli really do this), so that turn's answer
         covers it. A close after TURN_START_BACKSTOP_S is the documented liveness
         valve, not an early close.
  SLOW   once the agent has answered everything and the reads have run, every
         delivered message is closed BEFORE the backstop. Exempt: native polling (a
         turn too fast for any poll to see working is its documented cost), and input
         sent to a busy agent whose answer landed before any work frame after its
         dispatch was read (it closes at the backstop). The model's agent FOLDS such
         input, as claude_code and kiro-cli do; an agent that queues it is out of
         scope — docs/api.md says why a busy send is refused.
  STUCK  once the agent is idle and the backstop has passed, polling closes every turn.

Each provider shape runs twice: with no event loop (every read inline) and with a
fake loop whose timers fire on demand, so the real burst/quiescence scheduling, the
rising edge and the mid-burst probe run as they do live.

Raw output is modelled as tokens ("|W3" work sign for turn 3, "|D3" its answer, "|I0"
an idle prompt, "|P" a half-received repaint), which the fake raw detector reads the
way kiro's and grok's do. A "delayed read" runs other events inside the detector call,
so a verdict is applied after the world moved on.
"""

import asyncio
import random
import types
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus as S
from cli_agent_orchestrator.services import status_monitor as smod
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

TID = "t1"
KINDS = ("kiro", "grok", "claude", "native")
_RAW = {"W": S.PROCESSING, "P": S.PROCESSING, "D": S.COMPLETED, "I": S.IDLE}
_SCREEN = {"work": S.PROCESSING, "done": S.COMPLETED, "idle": S.IDLE}


class _Handle:
    def __init__(self, cb, args):
        self.cb, self.args, self.cancelled = cb, args, False

    def cancel(self):
        self.cancelled = True


class _Task:
    def add_done_callback(self, fn):
        fn(self)


class _FakeLoop:
    """Just enough of an event loop: timers fire when the schedule says so, tasks run
    to completion at once (on a private real loop, for asyncio.to_thread)."""

    def __init__(self):
        self.timers = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def call_later(self, _delay, cb, *args):
        handle = _Handle(cb, args)
        self.timers.append(handle)
        return handle

    def create_task(self, coro):
        runner = asyncio.new_event_loop()
        try:
            runner.run_until_complete(coro)
        finally:
            runner.close()
        return _Task()

    def fire(self):
        due, self.timers = [h for h in self.timers if not h.cancelled], []
        for handle in due:
            handle.cb(*handle.args)
        return bool(due)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Agent:
    def __init__(self):
        self.working = None  # set of turns the current work covers
        self.pending = []  # delivered, not yet picked up
        self.answered = set()
        self.screen = ("idle", 0)
        self.animate = False  # a working TUI redraws its spinner after new input
        self.work_since = 0.0


class _Provider:
    """kiro: raw + work sign; grok: raw, no sign; claude: retained screen, assumes
    PROCESSING on dispatch, mid-burst probe; native: herdr-style agent-state query."""

    def __init__(self, kind, agent):
        self.kind, self.agent = kind, agent
        self.supports_screen_detection = kind == "claude"
        self.supports_midburst_processing_probe = kind == "claude"
        self.assume_processing_on_dispatch = kind == "claude"
        self.observe_execution_output = None
        self.read_hook = None

    def get_status(self, buffer):
        hook, self.read_hook = self.read_hook, None
        if self.kind == "native":
            busy = self.agent.working or self.agent.pending
            verdict = S.PROCESSING if busy else S.COMPLETED
            if hook:
                hook()  # the query saw the state before the delay
            return verdict
        if hook:
            hook()
        toks = [t for t in buffer.split("|") if t]
        return _RAW.get(toks[-1][0], S.UNKNOWN) if toks else S.UNKNOWN

    def shows_turn_work(self, buffer):
        return ("|W" in buffer) if self.kind == "kiro" else None

    def notify_status_buffer_reset(self, epoch):
        pass

    def mark_input_received(self):
        pass

    def probe_processing_from_screen(self, lines):
        return bool(lines) and lines[0] == "WORK"


def _run(kind, rng, trace, loop_mode=False):
    clock, agent = _Clock(), _Agent()
    prov = _Provider(kind, agent)
    sm = StatusMonitor()
    loop = _FakeLoop() if loop_mode else None
    if loop is not None:
        sm._loop = loop
    delivered, delivered_at, folded = [], {}, set()
    backend = types.SimpleNamespace(supports_event_inbox=lambda: kind == "native")

    def token(frame):
        return {"work": "W", "done": "D", "idle": "I"}[frame[0]] + str(frame[1])

    def draw(frame):
        agent.screen = frame
        if kind in ("kiro", "grok", "claude"):
            # claude's chunks take the real pipeline too: raw buffer + pyte feed +
            # screen scheduling, exactly as live output does
            sm._process_chunk(TID, "|" + token(frame))

    def send(fail=False):
        turn = sm.notify_input_sent(
            TID, assume_processing=prov.assume_processing_on_dispatch, real_send=True
        )
        sm.clear_rolling_buffer(TID, prov, turn=turn)
        if fail:
            sm.abort_turn(TID, turn)
            trace.append(f"send(turn={turn}) FAILED")
            return
        sm.notify_input_delivered(TID)
        delivered.append(turn)
        delivered_at[turn] = clock.t
        if agent.working is not None:
            agent.working.add(turn)
            agent.animate = True
            folded.add(turn)
        else:
            agent.pending.append(turn)
        trace.append(f"send(turn={turn})")

    def special():
        trace.append(f"special_key(turn={sm.notify_input_sent(TID)})")

    def pickup():
        if agent.working is None and agent.pending:
            agent.working, agent.pending = set(agent.pending), []
            agent.work_since = clock.t
            trace.append(f"agent_picks_up({sorted(agent.working)})")
            draw(("work", max(agent.working)))

    def work_frame():
        if agent.working is not None:
            trace.append("agent_work_frame")
            draw(("work", max(agent.working)))

    def finish():
        if agent.working is not None:
            if agent.animate:  # it keeps animating while it works on the new input
                agent.animate = False
                draw(("work", max(agent.working)))
            while clock.t - agent.work_since < 1.5:
                # A real turn keeps animating its spinner for seconds (live: at least
                # ~2s before the answer, a frame every ~0.1s). Model frames every 0.5s
                # rather than an answer drawn within a single frame.
                clock.t += 0.5
                draw(("work", max(agent.working)))
            done, agent.working = agent.working, None
            agent.answered |= done
            trace.append(f"agent_answers({sorted(done)})")
            draw(("done", max(done)))

    def repaint():
        trace.append(f"repaint({agent.screen})")
        if kind == "kiro":
            sm._process_chunk(TID, "|P")  # half-received: no prompt yet, no sign
            if agent.screen[0] != "work":
                sm._process_chunk(TID, "|" + token(agent.screen))
        elif kind in ("grok", "claude"):
            sm._process_chunk(TID, "|" + token(agent.screen))

    def quiesce():
        trace.append("quiescence")
        if loop is not None:
            loop.fire()
        elif kind in ("kiro", "grok"):
            sm._on_raw_quiescent(TID)
        elif kind == "claude":
            sm._on_screen_quiescent(TID, prov)

    def poll():
        trace.append("poll")
        sm.get_status(TID)

    def probe():
        # With a loop the real scheduling runs the probe; forcing _bursting here
        # without the quiescence timer a real burst always arms would model a
        # burst that never ends.
        if kind == "claude" and loop is None:
            trace.append("midburst_probe")
            with sm._lock:
                sm._bursting[TID] = True
            sm._midburst_processing_probe(TID, prov)

    def tick():
        clock.t += rng.choice([0.3, 1.0, 5.0])
        if agent.working is not None:
            draw(("work", max(agent.working)))  # a working TUI keeps redrawing

    def delayed():
        inner = [
            rng.choice([pickup, work_frame, finish, tick, send, special, lambda: send(True)])
            for _ in range(rng.randint(1, 3))
        ]

        def hook():
            trace.append("  [read begins; meanwhile:]")
            for e in inner:
                e()
            trace.append("  [read returns]")

        if kind == "claude":
            frame = agent.screen

            def detect(_tid, _p):
                sm._detect_screen = lambda _t, _q: _SCREEN[agent.screen[0]]
                hook()
                return _SCREEN[frame[0]]  # the frame this read rendered

            sm._detect_screen = detect
            if loop is not None and loop.timers and rng.random() < 0.7:
                loop.fire()
            else:
                rng.choice(
                    [lambda: sm._on_screen_quiescent(TID, prov), lambda: sm.get_status(TID)]
                )()
            sm._detect_screen = lambda _t, _q: _SCREEN[agent.screen[0]]
        else:
            prov.read_hook = hook
            if kind == "native":
                sm.get_status(TID)
            elif loop is not None and loop.timers and rng.random() < 0.7:
                loop.fire()
            else:
                rng.choice([lambda: sm._on_raw_quiescent(TID), lambda: sm.get_status(TID)])()
            prov.read_hook = None

    events = [
        pickup,
        work_frame,
        finish,
        repaint,
        quiesce,
        poll,
        probe,
        tick,
        delayed,
        send,
        special,
        lambda: send(True),
    ]
    weights = [3, 3, 3, 2, 3, 3, 1, 3, 2, 2, 1, 1]

    def early(where):
        done = sm.turn_state(TID)[1]
        for t in delivered:
            if t <= done and t not in agent.answered:
                if clock.t - delivered_at[t] >= smod.TURN_START_BACKSTOP_S:
                    continue
                return f"EARLY after {where}: turn {t} done (turn_done={done}), not answered"
        return None

    with (
        patch.object(smod, "provider_manager") as pm,
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch.object(smod.time, "monotonic", clock),
        patch.object(smod, "get_server_settings", return_value={"state_buffer_max": 100000}),
    ):
        pm.get_provider.return_value = prov
        sm._detect_screen = lambda _t, _q: _SCREEN[agent.screen[0]]
        sm._screen_lines = lambda _t: (["WORK" if agent.screen[0] == "work" else "READY"], None)
        sm._last_status[TID] = S.IDLE
        for _ in range(rng.randint(0, 2)):  # provider init keystrokes
            special()
        for _ in range(rng.randint(8, 40)):
            rng.choices(events, weights=weights)[0]()
            tick()
            err = early(trace[-1] if trace else "start")
            if err:
                return err
        for _ in range(3):
            pickup()
            finish()
        for _ in range(3):
            quiesce()
            poll()
        err = early("drain")
        if err:
            return err
        # Input sent to a busy agent is exempt: it closes when work is drawn after
        # its dispatch (the model's agent folds it, as claude_code and kiro-cli do),
        # and at the backstop when the answer lands before any such frame is read.
        prompt = [t for t in delivered if t not in folded]
        if kind != "native" and prompt and sm.turn_state(TID)[1] < max(prompt):
            return (
                f"SLOW: everything answered, but turn_state={sm.turn_state(TID)} "
                f"leaves turn {max(prompt)} for the backstop"
            )
        clock.t += smod.TURN_START_BACKSTOP_S + 5
        for _ in range(3):
            poll()
            quiesce()
            repaint()
            poll()
        turn, done = sm.turn_state(TID)
        if done < turn:
            return f"STUCK: agent idle and backstop passed, turn_state={(turn, done)}"
        return early("final")


@pytest.mark.parametrize("loop_mode", [False, True], ids=["inline", "loop"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seed", [735, 812])
def test_turn_protocol_rules_hold_under_random_interleavings(kind, seed, loop_mode):
    rng = random.Random(seed)
    for i in range(600):
        trace = []
        err = _run(kind, random.Random(rng.random()), trace, loop_mode)
        assert err is None, f"schedule {i} ({kind}, seed {seed}): {err}\n  " + "\n  ".join(
            trace[-30:]
        )
