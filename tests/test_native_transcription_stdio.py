"""Synthetic real MCP framing and default-off checks, with no Telegram login."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.client import text_payload
from telegram_setup.gateway import create_gateway
from telegram_setup.permissions import exposed_tools, save_settings
from telegram_setup.sensitive_peer import BOTFATHER_ID
from telegram_setup import shared
from test_agent_gateway import RegistryFixture


class NativeTranscriptionMCPTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    async def test_default_gateway_does_not_expose_voice_tools(self):
        catalog = {tool.name for tool in await create_gateway(self.registry, 'a').list_tools()}
        self.assertFalse(catalog & {'transcribe_telegram_voice', 'get_telegram_voice_transcript'})
        with self.assertRaises(ValueError): create_gateway(self.registry, None, enable_native_transcription=True)

    def test_backend_flag_defaults_off_without_enabling_external_transcription(self):
        settings = {'schema_version': 1, 'enabled': ['read'], 'transcription_engine': None}
        with patch.dict(os.environ, {'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION': '0'}):
            disabled = exposed_tools(settings)
        with patch.dict(os.environ, {'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION': '1'}):
            enabled = exposed_tools(settings)
        self.assertNotIn('agent_transcribe_telegram_voice', disabled)
        self.assertIn('agent_transcribe_telegram_voice', enabled)
        self.assertIn('agent_get_telegram_voice_transcript', enabled)
        self.assertIn('transcribe_voice', enabled.rsplit('-', 1)[-1].split(','))  # Old multi-engine tool stays disabled.
        with patch.dict(os.environ, {'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION': 'yes'}):
            with self.assertRaises(ValueError): exposed_tools(settings)

    def test_shared_idle_waits_for_pending_but_honours_changed_permissions(self):
        settings = {'schema_version':1,'enabled':['read'],'transcription_engine':None}
        with patch('telegram_setup.permissions.load_settings', return_value=settings), \
             patch('telegram_setup.permissions.native_telegram_transcription_enabled', return_value=True), \
             patch('telegram_setup.owned_inventory.has_pending_inventory', return_value=False), \
             patch('telegram_setup.native_transcription.has_pending_native_transcription', return_value=True):
            self.assertTrue(shared.should_keep_alive(settings))
        with patch('telegram_setup.permissions.load_settings', return_value={**settings, 'enabled':['read','download']}), \
             patch('telegram_setup.native_transcription.has_pending_native_transcription', return_value=True):
            self.assertFalse(shared.should_keep_alive(settings))

    async def test_real_stdio_scope_identity_readback_and_no_engine_override(self):
        script = Path(__file__).with_name('offline_native_transcription.py')
        params = StdioServerParameters(command=sys.executable,
            args=[str(script), str(self.registry.path), str(Path(self.temporary.name) / 'voice-cache')])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize(); catalog = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertFalse(catalog['transcribe_telegram_voice'].annotations.readOnlyHint)
                    self.assertTrue(catalog['get_telegram_voice_transcript'].annotations.readOnlyHint)
                    status = text_payload(await session.call_tool('telegram_status', {}))
                    self.assertTrue(status['native_transcription_backend_ready']); self.assertFalse(status['read_only'])
                    args = {'chat_id': 11, 'message_id': 5, 'request_id': 'voice-stdio-001'}
                    result = text_payload(await session.call_tool('transcribe_telegram_voice', args))
                    self.assertEqual((result['status'], result['text']), ('complete', 'synthetic final'))
                    replay = text_payload(await session.call_tool('transcribe_telegram_voice',
                                              dict(args, request_id='voice-stdio-002')))
                    self.assertTrue(replay['cached']); self.assertEqual(replay['text'], result['text'])
                    cached = text_payload(await session.call_tool('get_telegram_voice_transcript',
                                              {'chat_id': 11, 'message_id': 5}))
                    self.assertEqual(cached['status'], 'complete')
                    for extra in ({'engine':'groq'}, {'agent_id':'b'}, {'account':'other'}):
                        self.assertTrue((await session.call_tool('transcribe_telegram_voice', dict(args, **extra))).isError)
                    self.assertTrue((await session.call_tool('transcribe_telegram_voice', dict(args, chat_id=22))).isError)
                    self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)
                    self.assertTrue((await session.call_tool('transcribe_telegram_voice', dict(args, chat_id=BOTFATHER_ID))).isError)
                    self.data['agents']['a']['actions'].remove('history'); self.registry.save(self.data)
                    self.assertTrue((await session.call_tool('get_telegram_voice_transcript',
                                        {'chat_id':11, 'message_id':5})).isError)

    async def test_real_backend_catalog_switch_is_default_off(self):
        root = Path(__file__).resolve().parents[1]
        if not (root/'.upstream'/'main.py').is_file(): self.skipTest('pinned upstream not staged')
        for enabled in (False, True):
            with tempfile.TemporaryDirectory() as home:
                with patch.dict(os.environ, {'AGENT_TELEGRAM_HOME':home}):
                    save_settings({'schema_version':1,'enabled':['read'],'transcription_engine':None})
                env = {'AGENT_TELEGRAM_HOME':home, 'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION':'1' if enabled else '0'}
                params = StdioServerParameters(command=sys.executable,args=[str(root/'tests/offline_server.py')],cwd=str(root),env=env)
                with open(os.devnull,'w') as err:
                    async with stdio_client(params,errlog=err) as (reader,writer):
                        async with ClientSession(reader,writer) as session:
                            await session.initialize();names={t.name for t in (await session.list_tools()).tools}
                            self.assertEqual('agent_transcribe_telegram_voice' in names, enabled)
                            self.assertEqual('agent_get_telegram_voice_transcript' in names, enabled)
                            self.assertNotIn('transcribe_voice', names)  # No upstream ASR engine enabled.


if __name__ == '__main__': unittest.main()
