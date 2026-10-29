"""Synthetic stdio gateway test: real MCP framing, no Telegram or network."""
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original = socket.socket.connect
def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}: raise AssertionError('internet forbidden')
    return original(self, address)
socket.socket.connect = no_internet

from mcp.types import CallToolResult, TextContent
from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import UpstreamBridge, create_gateway
from telegram_setup.sensitive_peer import BOTFATHER_ID
from test_sensitive_peer import TOKEN

class FakeSession:
    async def call_tool(self, name, args):
        if name == 'get_me': value = {'type': 'user', 'id': 101}
        elif name == 'read_history_page':
            value = {'chat_id': args['chat_id'], 'messages': [{'id': 1, 'text': 'ordinary fixture'}],
                     'has_more': False, 'next_after_id': 1}
        elif name == 'read_account_search_page':
            value = {'messages': [{'chat_id': BOTFATHER_ID, 'id': 1, 'text': 'private fixture'},
                                  {'chat_id': 11, 'id': 2, 'text': 'https://api.telegram.org/bot' + TOKEN}],
                     'verified_chat_ids': [BOTFATHER_ID, 11], 'search_engine': 'account_global_v2',
                     'complete': True, 'scan_finished': True, 'partial': False, 'next_cursor': None}
        else: raise AssertionError('unexpected fixture operation')
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(value))])

@asynccontextmanager
async def upstream(): yield UpstreamBridge(FakeSession())

create_gateway(AgentRegistry(sys.argv[1]), 'a', upstream, enable_search=True).run(transport='stdio')
