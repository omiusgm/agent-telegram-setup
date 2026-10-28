"""Synthetic profile/avatars over actual stdio MCP; no Internet."""
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
original_connect=socket.socket.connect
def offline(self,address):
    if self.family in (socket.AF_INET,socket.AF_INET6):raise AssertionError('Offline profile test attempted network')
    return original_connect(self,address)
socket.socket.connect=offline
from telegram_setup.agents import AgentRegistry
from telegram_setup.file_transport import StagedFiles
from telegram_setup.gateway import create_gateway
from telegram_setup.peer_profiles import PeerBackend
from test_peer_profiles import PeerClient,PeerBridge

registry=AgentRegistry(sys.argv[1]);folder=Path(sys.argv[2]);store=StagedFiles(folder/'staged')
attachments=len(sys.argv)>3 and sys.argv[3]=='attachments'
if attachments:
    from test_attachments import AttachmentClient
    client=AttachmentClient()
else:client=PeerClient()
bridge=PeerBridge(PeerBackend(client,registry,store))
if attachments:bridge.available_tools=set(bridge.available_tools)|{'download_agent_attachment'}
@asynccontextmanager
async def upstream():yield bridge
create_gateway(registry,'a',upstream,enable_profiles=True,enable_attachments=attachments,file_store=store).run(transport='stdio')
