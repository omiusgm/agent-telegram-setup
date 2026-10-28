"""Actual stdio transport, synthetic photo backend; Internet is blocked."""
import contextlib
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_connect = socket.socket.connect


def offline_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError('Offline photos attempted network')
    return original_connect(self, address)


socket.socket.connect = offline_connect
from telegram_setup.agents import AgentRegistry
from telegram_setup.file_transport import StagedFiles
from telegram_setup.gateway import create_gateway
from telegram_setup.photos import PhotoBackend
from test_photos import PhotoClient, PhotoBridge

registry = AgentRegistry(sys.argv[1])
folder = Path(sys.argv[2])
store = StagedFiles(folder / 'staged')
bridge = PhotoBridge(PhotoBackend(PhotoClient(), registry, store))


@contextlib.asynccontextmanager
async def upstream(): yield bridge


create_gateway(registry, 'a', upstream, enable_read_photo=True, enable_image_probe=True,
               receipt_dir=folder / 'receipts', file_store=store).run(transport='stdio')
