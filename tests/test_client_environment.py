"""A non-default installation must survive both real MCP subprocess hops."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_setup import client, gateway


FIXTURE_SERVER = '''
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

home = os.environ.get('AGENT_TELEGRAM_HOME', '')
assert home and (Path(home) / 'synthetic-installation').is_file()
assert os.environ.get('AGENT_TELEGRAM_MANAGED_BACKEND') == '1'
assert os.environ.get('AGENT_TELEGRAM_NATIVE_TRANSCRIPTION') == '1'
assert os.environ.get('AGENT_TELEGRAM_SOCKS_HOST') == '127.0.0.1'
assert 'TELEGRAM_STRING_SESSION' not in os.environ
assert 'TELEGRAM_API_HASH' not in os.environ
app = FastMCP('environment-fixture', log_level='CRITICAL')
annotation = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

@app.tool(annotations=annotation)
def get_me() -> dict:
    return {'type': 'user', 'id': 12345}

def empty() -> dict:
    return {}

for name in ('list_chats', 'get_chat', 'read_dialog_page', 'list_contacts',
             'read_history_page', 'search_messages', 'get_messages'):
    app.add_tool(empty, name=name, annotations=annotation)
app.run()
'''


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_and_gateway_reach_the_selected_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'separate-private-home'
            home.mkdir()
            (home / 'synthetic-installation').touch()
            (root / 'server.py').write_text(FIXTURE_SERVER)
            selected = {'AGENT_TELEGRAM_HOME': str(home),
                        'AGENT_TELEGRAM_MANAGED_BACKEND': '1',
                        'AGENT_TELEGRAM_NATIVE_TRANSCRIPTION': '1',
                        'AGENT_TELEGRAM_SOCKS_HOST': '127.0.0.1',
                        'TELEGRAM_STRING_SESSION': 'synthetic-must-not-inherit',
                        'TELEGRAM_API_HASH': 'synthetic-must-not-inherit'}
            with patch.dict(os.environ, selected), patch.object(client, 'ROOT', root):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    await client.run(argparse.Namespace(action='status', timeout=10))
                self.assertEqual(json.loads(output.getvalue())['connection_probe'], 'passed')
                async with gateway.upstream_connection() as bridge:
                    self.assertEqual(await bridge.call('get_me', {}), {'type': 'user', 'id': 12345})


if __name__ == '__main__':
    unittest.main()
