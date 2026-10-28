"""Read-only fixed-byte probe. No Telegram, filesystem, URL fetch or credentials."""
import base64
import hashlib
import json
from typing import Literal

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from pydantic import BaseModel, ConfigDict

FIXTURE_NAME = 'telegram-mcp-file-probe.txt'
FIXTURE_BYTES = (b'Telegram MCP native resource probe\n'
                 b'Synthetic public fixture. No personal data.\n'
                 b'Compare this file byte-for-byte after Dot creates a new artifact.\n')
FIXTURE_URI = 'urn:telegram-mcp:synthetic-file-probe'


class ProbeMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    file_name: str
    mime_type: str
    size: int
    sha256: str
    resource_uri: str
    synthetic: Literal[True]
    telegram_calls: Literal[0]
    external_url_fetches: Literal[0]
    output_transport: Literal['standard_mcp_embedded_blob']
    dot_file_received_verified: Literal[False]


def probe_result():
    meta = {'file_name': FIXTURE_NAME, 'mime_type': 'text/plain', 'size': len(FIXTURE_BYTES),
            'sha256': hashlib.sha256(FIXTURE_BYTES).hexdigest(), 'resource_uri': FIXTURE_URI,
            'synthetic': True, 'telegram_calls': 0, 'external_url_fetches': 0,
            'output_transport': 'standard_mcp_embedded_blob', 'dot_file_received_verified': False}
    return CallToolResult(structuredContent=meta, content=[
        TextContent(type='text', text=json.dumps(meta)),
        EmbeddedResource(type='resource', resource=BlobResourceContents(uri=FIXTURE_URI,
            mimeType='text/plain', blob=base64.b64encode(FIXTURE_BYTES).decode()))])
