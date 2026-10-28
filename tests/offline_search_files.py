"""Actual stdio MCP against synthetic Telegram only, with Internet blocked."""
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_connect = socket.socket.connect


def offline_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError('Offline search/files server attempted Internet')
    return original_connect(self, address)


socket.socket.connect = offline_connect
from telethon.tl.types import InputPeerSelf
from telegram_setup.agents import AgentRegistry
from telegram_setup.file_transport import StagedFiles
from telegram_setup.gateway import create_gateway
from telegram_setup.saved_files import SavedFileBackend
from telegram_setup.saved_text import SendReceipts
from test_saved_files import BackendBridge, FileClient
from test_scoped_search import SearchUpstream
from telegram_setup.account_search import AccountSearchBackend
from test_account_search import FakeMTProto, fake_runtime, converter

registry = AgentRegistry(sys.argv[1])
folder = Path(sys.argv[2])
store = StagedFiles(folder / 'staged')
backend = SavedFileBackend(FileClient(), registry, store, SendReceipts(folder / 'backend-receipts'),
                           self_peer=InputPeerSelf())
bridge = BackendBridge(backend)
search = SearchUpstream()
account_search = AccountSearchBackend(fake_runtime(FakeMTProto()), registry,
                                      folder / 'account-cursors', converter)
original_call = bridge.call


async def combined(name, args):
    if name == 'read_account_search_page':
        return await account_search.page(args['agent_id'], args['query'], args.get('cursor'), args['limit'])
    if name in ('read_search_page', 'search_community_metadata', 'read_dialog_page'):
        return await search.call(name, args)
    return await original_call(name, args)


bridge.call = combined


@asynccontextmanager
async def upstream(): yield bridge


create_gateway(registry, 'a', upstream, enable_saved_text=True, enable_search=True,
               enable_saved_files=True, file_store=store, receipt_dir=folder / 'gateway-receipts').run(transport='stdio')
