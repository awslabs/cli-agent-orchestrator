"""Per-key locks whose map holds only the keys in use (#745)."""

import asyncio
import threading
import time

from cli_agent_orchestrator.utils.keyed_lock import AsyncKeyedLock, KeyedLock


class TestKeyedLock:
    def test_an_entry_lives_only_while_its_key_is_held(self):
        locks = KeyedLock()
        for n in range(100):
            with locks.hold(("s", f"w{n}")):
                assert len(locks) == 1
        assert len(locks) == 0

    def test_a_waiter_and_a_late_caller_share_the_holders_lock(self):
        # The eviction hazard: the holder leaves while a caller waits. A
        # caller arriving after that must queue behind the waiter, not get a
        # fresh lock and run beside it.
        locks = KeyedLock()
        inside = []
        overlap = []
        guard = threading.Lock()
        entered = {"holder": threading.Event(), "waiter": threading.Event()}
        leave = {"holder": threading.Event(), "waiter": threading.Event()}

        def critical(name):
            with locks.hold("k"):
                with guard:
                    if inside:
                        overlap.append((name, list(inside)))
                    inside.append(name)
                if name in entered:
                    entered[name].set()
                    leave[name].wait(5)
                with guard:
                    inside.remove(name)

        holder = threading.Thread(target=critical, args=("holder",))
        holder.start()
        assert entered["holder"].wait(5)
        waiter = threading.Thread(target=critical, args=("waiter",))
        waiter.start()
        time.sleep(0.1)  # the waiter is queued on the holder's lock
        leave["holder"].set()
        holder.join(5)
        assert entered["waiter"].wait(5)
        late = threading.Thread(target=critical, args=("late",))
        late.start()
        time.sleep(0.1)  # with a fresh lock, the late caller would be inside now
        leave["waiter"].set()
        for t in (waiter, late):
            t.join(5)
        assert overlap == []
        assert len(locks) == 0

    def test_an_exception_inside_still_releases_and_evicts(self):
        locks = KeyedLock()
        try:
            with locks.hold("k"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert len(locks) == 0
        with locks.hold("k"):  # not left locked
            pass


class TestAsyncKeyedLock:
    def test_an_entry_lives_only_while_its_key_is_held_or_awaited(self):
        locks = AsyncKeyedLock()
        order = []

        async def scenario():
            release = asyncio.Event()

            async def holder():
                async with locks.hold("k"):
                    order.append("holder")
                    await release.wait()

            async def waiter(name):
                async with locks.hold("k"):
                    order.append(name)

            tasks = [asyncio.ensure_future(holder())]
            await asyncio.sleep(0)
            tasks += [asyncio.ensure_future(waiter(f"w{n}")) for n in range(3)]
            await asyncio.sleep(0.01)
            assert len(locks) == 1 and order == ["holder"]
            release.set()
            await asyncio.gather(*tasks)

        asyncio.run(scenario())
        assert order == ["holder", "w0", "w1", "w2"]
        assert len(locks) == 0

    def test_a_cancelled_waiter_leaves_no_entry(self):
        locks = AsyncKeyedLock()

        async def scenario():
            release = asyncio.Event()

            async def holder():
                async with locks.hold("k"):
                    await release.wait()

            async def waiter():
                async with locks.hold("k"):
                    pass

            h = asyncio.ensure_future(holder())
            await asyncio.sleep(0)
            w = asyncio.ensure_future(waiter())
            await asyncio.sleep(0.01)
            w.cancel()
            await asyncio.gather(w, return_exceptions=True)
            release.set()
            await h

        asyncio.run(scenario())
        assert len(locks) == 0
