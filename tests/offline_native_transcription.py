"""Real stdio gateway over synthetic Telegram objects; Internet is forbidden."""
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original = socket.socket.connect
def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}: raise AssertionError('internet forbidden')
    return original(self, address)
socket.socket.connect = no_internet

from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
from telegram_setup.native_transcription import NativeVoiceService
from telegram_setup.native_voice_store import NativeVoiceStore
from test_native_transcription import FakeClient

registry = AgentRegistry(sys.argv[1]); client = FakeClient()
async def resolve(chat_id, _client): return NS(id=chat_id)
runtime = NS(resolve_entity=resolve, get_marked_id=lambda entity: entity.id,
             is_chat_allowlist_enabled=lambda: False)
native = NativeVoiceService(runtime, client, registry, 101, NativeVoiceStore(Path(sys.argv[2])))

class Bridge:
    available_tools = {'get_me', 'agent_transcribe_telegram_voice', 'agent_get_telegram_voice_transcript'}
    async def call(self, name, args):
        if name == 'get_me': return {'type': 'user', 'id': 101}
        if name == 'agent_transcribe_telegram_voice':
            return await native.start(args['agent_id'], args['chat_id'], args['message_id'], args['request_id'])
        if name == 'agent_get_telegram_voice_transcript':
            return await native.get(args['agent_id'], args['chat_id'], args['message_id'])
        raise AssertionError('unexpected fixture operation')

@asynccontextmanager
async def upstream():
    native.attach(); await native.activate(101); yield Bridge()

create_gateway(registry, 'a', upstream, enable_native_transcription=True).run(transport='stdio')
