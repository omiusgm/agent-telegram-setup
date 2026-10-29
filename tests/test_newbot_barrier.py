"""Shared backend quiescence without touching Telegram."""
import asyncio
from types import SimpleNamespace
import unittest

from mcp.server.fastmcp.exceptions import ToolError
from telegram_setup.newbot_backend import SharedCallBarrier


class BarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_call_blocks_newbot_and_new_calls_block_while_active(self):
        start, release = asyncio.Event(), asyncio.Event()
        async def original(name, arguments):
            if name == 'other':
                start.set(); await release.wait(); return 'other_done'
            if name == 'create':
                barrier.enter()
                try:
                    start.set(); await release.wait(); return 'created'
                finally: barrier.exit()
        manager = SimpleNamespace(call_tool=original)
        mcp = SimpleNamespace(_tool_manager=manager)
        barrier = SharedCallBarrier(); barrier.install(mcp)
        other = asyncio.create_task(manager.call_tool('other', {})); await start.wait()
        with self.assertRaises(PermissionError): await manager.call_tool('create', {})
        release.set(); self.assertEqual(await other, 'other_done')
        start.clear(); release.clear()
        creation = asyncio.create_task(manager.call_tool('create', {})); await start.wait()
        self.assertTrue(barrier.active)
        with self.assertRaises(ToolError): await manager.call_tool('other', {})
        release.set(); self.assertEqual(await creation, 'created')
        self.assertEqual(barrier.running, 0); self.assertFalse(barrier.active)

    async def test_cancellation_releases_barrier_and_counter(self):
        started = asyncio.Event()
        async def original(name, args):
            barrier.enter()
            try: started.set(); await asyncio.Event().wait()
            finally: barrier.exit()
        manager = SimpleNamespace(call_tool=original)
        barrier = SharedCallBarrier(); barrier.install(SimpleNamespace(_tool_manager=manager))
        task = asyncio.create_task(manager.call_tool('create', {})); await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(barrier.running, 0); self.assertFalse(barrier.active)
