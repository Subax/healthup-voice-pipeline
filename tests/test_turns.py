import asyncio

from voice_pipeline.turns import TurnGuard


async def test_invalidate_cancels_only_dead_turn_tasks():
    g = TurnGuard()
    t1 = g.next_turn()
    a = g.track(t1, asyncio.create_task(asyncio.sleep(10)))
    dead = g.invalidate()
    t2 = g.current
    b = g.track(t2, asyncio.create_task(asyncio.sleep(10)))
    await asyncio.sleep(0)
    assert dead == t1 and not g.is_live(t1) and g.is_live(t2)
    assert a.cancelled()
    assert not b.done()
    b.cancel()
