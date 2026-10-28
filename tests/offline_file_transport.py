"""Synthetic MCP byte probe only. Never register in the live Telegram gateway."""
import base64
import hashlib
import socket

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict

_connect = socket.socket.connect


def no_network(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError('Offline file probe attempted network access')
    return _connect(self, address)


socket.socket.connect = no_network
CHUNK_BYTES, FILE_BYTES = 16384, 65536
app = FastMCP('OFFLINE file transport contract', log_level='CRITICAL')
received = bytearray()
identity = None


class OpenAIFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    download_url: str
    file_id: str
    # Optional strings, not required nullable fields: the host schema declares
    # all four properties but only download_url/file_id must be required.
    mime_type: str = ''
    file_name: str = ''


class Chunk(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str
    offset: int
    total_size: int
    file_sha256: str
    chunk_sha256: str
    data_b64: str


@app.tool(meta={'openai/fileParams': ['file']})
async def probe_file_reference(file: OpenAIFile) -> dict:
    """Validate a synthetic file object. Does not fetch URLs or prove host injection."""
    if file.download_url != 'https://files.example.invalid/probe.bin' or file.file_id != 'file_fixture_demo':
        raise ValueError('synthetic_file_reference_required')
    return {'schema_accepted': True, 'native_host_injection_verified': False,
            'file_bytes_fetched': False, 'telegram_calls': 0}


@app.tool()
async def probe_write_chunk(payload: Chunk) -> dict:
    """Synthetic byte ingress; bounded, digest checked and retry stable."""
    global identity
    if (not 8 <= len(payload.request_id) <= 128 or payload.offset < 0
            or not 1 <= payload.total_size <= FILE_BYTES or len(payload.data_b64) > 21848):
        raise ValueError('invalid_chunk_boundary')
    data = base64.b64decode(payload.data_b64, validate=True)
    if not 1 <= len(data) <= CHUNK_BYTES or payload.offset+len(data) > payload.total_size:
        raise ValueError('chunk_too_large')
    if hashlib.sha256(data).hexdigest() != payload.chunk_sha256:
        raise ValueError('chunk_digest_mismatch')
    key = (payload.request_id, payload.total_size, payload.file_sha256)
    if identity is not None and key != identity:
        raise ValueError('transfer_identity_changed')
    if payload.offset > len(received):
        raise ValueError('noncontiguous_offset')
    if payload.offset < len(received):
        if bytes(received[payload.offset:payload.offset+len(data)]) != data:
            raise ValueError('retry_bytes_changed')
        return {'next_offset': len(received), 'replayed': True}
    identity = key
    received.extend(data)
    return {'next_offset': len(received), 'replayed': False}


@app.tool()
async def probe_read_chunk(offset: int, limit: int = CHUNK_BYTES) -> dict:
    """Synthetic egress via ordinary JSON; does not create a ChatGPT file handle."""
    if identity is None or len(received) != identity[1]:
        raise ValueError('upload_incomplete')
    digest = hashlib.sha256(received).hexdigest()
    if digest != identity[2]:
        raise ValueError('whole_file_digest_mismatch')
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= len(received) or not 1 <= limit <= CHUNK_BYTES:
        raise ValueError('invalid_read_boundary')
    data = bytes(received[offset:offset+limit])
    return {'offset': offset, 'next_offset': offset+len(data), 'eof': offset+len(data)==len(received),
            'data_b64': base64.b64encode(data).decode(), 'chunk_sha256': hashlib.sha256(data).hexdigest(),
            'file_sha256': digest, 'total_size': len(received), 'mime_type': 'application/octet-stream',
            'cloud_file_artifact_created': False, 'telegram_calls': 0}


if __name__ == '__main__':
    app.run(transport='stdio')
