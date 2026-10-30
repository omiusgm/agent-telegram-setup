"""Synthetic logger/barrier checks; no real session, BotFather, or network."""
import asyncio
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from telegram_setup.agents import AgentRegistry
from telegram_setup.client import text_payload
from telegram_setup.newbot_backend import register_newbot_backend,telethon_logging_summary
from telegram_setup.newbot_worker import logging_is_quiet
from telegram_setup.permissions import save_settings
from test_agent_gateway import RegistryFixture

SOURCE=Path(__file__).resolve().parents[1]

class LoggingUnitTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry();self.data['agents']['a']['actions'].append('create-owned-bot');self.registry.save(self.data)
        self.loggers={name:logging.getLogger(name) for name in ['telethon','telethon.fixture.explicit','telethon.fixture.strict']}
        self.old={name:logger.level for name,logger in self.loggers.items()};self.oldroot=logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO);self.loggers['telethon'].setLevel(logging.NOTSET)
        self.loggers['telethon.fixture.explicit'].setLevel(logging.DEBUG);self.loggers['telethon.fixture.strict'].setLevel(logging.ERROR)
        self.patch=patch('telegram_setup.newbot_backend.private_dir',return_value=Path(self.temporary.name));self.patch.start()

    def tearDown(self):
        self.patch.stop()
        for name,level in self.old.items():self.loggers[name].setLevel(level)
        logging.getLogger().setLevel(self.oldroot);self.teardown_registry()

    def runtime(self,enabled):
        mcp=FastMCP('synthetic');runtime=SimpleNamespace(mcp=mcp,ToolAnnotations=ToolAnnotations,
              with_account=lambda **_:lambda f:f,get_client=lambda _:object())
        register_newbot_backend(runtime,creation_enabled=enabled);return mcp

    async def test_enabled_raises_only_telethon_levels_and_keeps_guard(self):
        mcp=self.runtime(True)
        self.assertTrue(logging_is_quiet());self.assertEqual(logging.getLogger().getEffectiveLevel(),logging.INFO)
        self.assertEqual(self.loggers['telethon.fixture.explicit'].getEffectiveLevel(),logging.WARNING)
        self.assertEqual(self.loggers['telethon.fixture.strict'].getEffectiveLevel(),logging.ERROR)
        result=await mcp._tool_manager.call_tool('agent_newbot_preflight',{'agent_id':'a'})
        self.assertFalse(result['startup_logging_before']['quiet']);self.assertTrue(result['startup_logging_after']['quiet'])
        self.assertTrue(result['current_guard_quiet']);self.assertTrue(result['barrier_ready_for_one'])
        self.assertEqual(result['botfather_calls'],0);self.assertFalse(result['creation_attempted'])
        self.assertFalse((Path(self.temporary.name)/'newbot-claims').exists())

    async def test_disabled_does_not_change_logging(self):
        mcp=self.runtime(False);self.assertFalse(telethon_logging_summary()['quiet'])
        result=await mcp._tool_manager.call_tool('agent_newbot_preflight',{'agent_id':'a'})
        self.assertFalse(result['creation_enabled']);self.assertFalse(result['current_guard_quiet'])
        self.assertEqual(self.loggers['telethon.fixture.explicit'].level,logging.DEBUG)

    async def test_busy_barrier_and_later_debug_override_are_reported_without_action(self):
        mcp=self.runtime(True);started=asyncio.Event();release=asyncio.Event()
        @mcp.tool()
        async def synthetic_hold() -> dict:
            started.set();await release.wait();return {'ok':True}
        pending=asyncio.create_task(mcp._tool_manager.call_tool('synthetic_hold',{}));await started.wait()
        try:
            self.loggers['telethon.fixture.explicit'].setLevel(logging.DEBUG)
            result=await mcp._tool_manager.call_tool('agent_newbot_preflight',{'agent_id':'a'})
            self.assertFalse(result['barrier_ready_for_one']);self.assertEqual(result['barrier_running'],2)
            self.assertFalse(result['current_guard_quiet']);self.assertEqual(result['botfather_calls'],0)
        finally:release.set();await pending

    async def test_ungranted_agent_gets_fixed_denial(self):
        mcp=self.runtime(True)
        result=await mcp._tool_manager.call_tool('agent_newbot_preflight',{'agent_id':'b'})
        self.assertEqual(result,{'error':'newbot_preflight_not_authorized'})


class BackendStdioTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.setup_registry()
    def tearDown(self):self.teardown_registry()

    async def test_enabled_actual_stdio_exposes_redacted_diagnostic_off_does_not(self):
        self.data['agents']['a']['actions'].append('create-owned-bot');self.data['agents']['a']['bot_create_mode']='delegated'
        self.registry.save(self.data)
        for enabled in (False,True):
            selected=['read']+(['agent-bot-create'] if enabled else [])
            with patch.dict(os.environ,{'AGENT_TELEGRAM_HOME':self.temporary.name}):
                save_settings({'schema_version':1,'enabled':selected,'transcription_engine':None})
            env={'AGENT_TELEGRAM_HOME':self.temporary.name,'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION':'1'}
            params=StdioServerParameters(command=os.sys.executable,args=[str(SOURCE/'tests/offline_server.py')],cwd=str(SOURCE),env=env)
            with open(os.devnull,'w') as err:
                async with stdio_client(params,errlog=err) as (r,w):
                    async with ClientSession(r,w) as session:
                        await session.initialize();names={t.name for t in (await session.list_tools()).tools}
                        self.assertEqual('agent_newbot_preflight' in names,enabled)
                        self.assertIn('agent_transcribe_telegram_voice',names);self.assertNotIn('transcribe_voice',names)
                        if enabled:
                            response=await session.call_tool('agent_newbot_preflight',{'agent_id':'a'})
                            self.assertFalse(response.isError);result=text_payload(response)
                            self.assertTrue(result['policy_allowed']);self.assertTrue(result['creation_enabled'])
                            self.assertFalse(result['startup_logging_before']['quiet'])
                            self.assertTrue(result['startup_logging_after']['quiet']);self.assertTrue(result['current_guard_quiet'])
                            self.assertTrue(result['barrier_ready_for_one']);self.assertEqual(result['creation_mode'],'delegated')
                            self.assertEqual(result['botfather_calls'],0);self.assertFalse(result['creation_attempted'])
                            self.assertNotIn('token',str(result).lower())
        self.assertFalse((Path(self.temporary.name)/'newbot-claims').exists())
        self.assertFalse((Path(self.temporary.name)/'bot-tokens').exists())

if __name__=='__main__':unittest.main()
