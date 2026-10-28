"""Scoped peer cards and avatar reads. Reuses the one existing Telegram client."""
import asyncio
import re
import time
from typing import Optional, Union

from telethon import utils
from telethon.tl import functions, types
from telethon.tl.custom.file import File

from .agents import AccessDenied, check_action
from .photos import image_preview
from .saved_files import BoundedBytesIO


def peer_policy(registry, agent, action, peer=None):
    profile = registry.profile(agent)
    check_action(profile, action)
    if peer is None: return profile
    if type(peer) is int and peer != 0:
        # Explicit allowed contacts are valid profile peers as well as allowed dialogs.
        scope = dict(profile, chat_ids=list(set(profile.get('chat_ids', [])) |
                     {p for p in profile.get('contact_ids', []) if p > 0}))
        check_action(scope, action, peer)
    elif isinstance(peer, str) and (peer == 'me' or re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}', peer)):
        if not profile.get('all_chats'): raise AccessDenied('username_lookup_requires_full_peer_scope')
    else:
        raise ValueError('numeric_peer_or_exact_username_required')
    return profile


def page_args(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('invalid_avatar_page')


def text(value, maximum=1024):
    return value[:maximum] if isinstance(value, str) else None


class PeerBackend:
    def __init__(self, client, registry, store):
        self.client, self.registry, self.store = client, registry, store

    async def resolve(self, agent, action, peer):
        peer_policy(self.registry, agent, action, peer)
        entity = await asyncio.wait_for(self.client.get_entity(peer), 20)
        numeric = utils.get_peer_id(entity)
        if type(peer) is int and numeric != peer: raise ValueError('peer_identity_mismatch')
        peer_policy(self.registry, agent, action, numeric)
        if not isinstance(entity, (types.User, types.Chat, types.Channel)):
            raise ValueError('unsupported_peer_type')
        return entity, numeric

    async def full(self, entity):
        if isinstance(entity, types.User): request = functions.users.GetFullUserRequest(entity)
        elif isinstance(entity, types.Channel): request = functions.channels.GetFullChannelRequest(entity)
        else: request = functions.messages.GetFullChatRequest(entity.id)
        return await asyncio.wait_for(self.client(request, flood_sleep_threshold=0), 20)

    async def profile(self, agent, peer):
        entity, numeric = await self.resolve(agent, 'read-profiles', peer)
        reply = await self.full(entity)
        profile = peer_policy(self.registry, agent, 'read-profiles', numeric)
        user = isinstance(entity, types.User)
        details = reply.full_user if user else reply.full_chat
        result = {'peer_id': numeric, 'type': 'user' if user else 'channel' if getattr(entity,'broadcast',False) else 'group',
                  'username': text(getattr(entity, 'username', None), 64),
                  'about': text(getattr(details, 'about', None)), 'untrusted_content': True,
                  'current_avatar_id': getattr(getattr(entity, 'photo', None), 'photo_id', None)}
        if user:
            result.update(first_name=text(entity.first_name, 128), last_name=text(entity.last_name, 128),
                bot=bool(entity.bot), verified=bool(entity.verified), premium=bool(entity.premium),
                contact=bool(entity.contact), mutual_contact=bool(entity.mutual_contact),
                common_chats_count=getattr(details, 'common_chats_count', None),
                personal_channel_id=getattr(details, 'personal_channel_id', None))
            if profile.get('include_phones'): result['phone'] = text(getattr(entity, 'phone', None), 64)
        else:
            result.update(title=text(entity.title, 256), creator=bool(entity.creator),
                          participants_count=getattr(details, 'participants_count', None))
        return result

    async def references(self, entity, *, offset, limit, photo_id=None):
        if isinstance(entity, types.User):
            reply = await asyncio.wait_for(self.client(functions.photos.GetUserPhotosRequest(
                user_id=entity, offset=-1 if photo_id is not None else offset,
                max_id=photo_id or 0, limit=1 if photo_id is not None else limit), flood_sleep_threshold=0), 20)
            photos = list(reply.photos)[:limit]
            total = getattr(reply, 'count', None)
            return photos, len(photos), total
        # A group's current avatar is available without scanning message history.
        full = await self.full(entity)
        current = getattr(full.full_chat, 'chat_photo', None)
        if photo_id is None or getattr(current, 'id', None) == photo_id:
            return ([current] if isinstance(current, types.Photo) and offset == 0 else []), 1, 1
        # No broad history fallback for arbitrary old group photo IDs.
        return [], 0, 1

    async def avatars(self, agent, peer, offset=0, limit=10):
        page_args(offset, limit)
        entity, numeric = await self.resolve(agent, 'read-avatars', peer)
        photos, consumed, total = await self.references(entity, offset=offset, limit=limit)
        peer_policy(self.registry, agent, 'read-avatars', numeric)
        current = getattr(getattr(entity, 'photo', None), 'photo_id', None)
        rows = [{'photo_id':p.id, 'is_current':p.id==current,
                 'date':p.date.isoformat() if getattr(p, 'date', None) else None}
                for p in photos if isinstance(p, types.Photo)]
        more = offset+consumed < total if type(total) is int else consumed == limit
        return {'peer_id':numeric,'avatars':rows,'next_offset':offset+consumed if more and consumed else None,
                'history_supported':isinstance(entity, types.User),'untrusted_content':True}

    async def avatar(self, agent, peer, photo_id=None):
        if photo_id is not None and (type(photo_id) is not int or photo_id <= 0):
            raise ValueError('positive_photo_id_required')
        entity, numeric = await self.resolve(agent, 'read-avatars', peer)
        photos, _, _ = await self.references(entity, offset=0, limit=25, photo_id=photo_id)
        current = getattr(getattr(entity, 'photo', None), 'photo_id', None)
        photo = next((p for p in photos if isinstance(p, types.Photo) and
                      p.id == (photo_id if photo_id is not None else current)), None)
        if photo is None: raise ValueError('avatar_missing_or_not_visible')
        source_id = photo.id
        for attempt in range(2):
            size = File(photo).size
            if type(size) is not int or not 1 <= size <= self.store.maximum:
                raise ValueError('avatar_size_unavailable_or_exceeded')
            target = BoundedBytesIO(self.store.maximum)
            def progress(received, total):
                if received > size or total > size: raise ValueError('avatar_size_exceeded')
            try:
                await asyncio.wait_for(self.client.download_media(photo,file=target,progress_callback=progress),45)
                break
            except Exception as exc:
                if type(exc).__name__ not in ('FileReferenceExpiredError','FileReferenceInvalidError') or attempt: raise
                peer_policy(self.registry,agent,'read-avatars',numeric)
                photos,_,_=await self.references(entity,offset=0,limit=1,photo_id=source_id)
                photo=next((p for p in photos if isinstance(p,types.Photo) and p.id==source_id),None)
                if photo is None:raise ValueError('avatar_reference_missing') from None
        data=target.getvalue()
        if len(data)!=size:raise ValueError('avatar_partial_read')
        image_preview(data,'image/jpeg')
        peer_policy(self.registry,agent,'read-avatars',numeric)
        result=self.store.put(agent,data,'avatar.jpg','image/jpeg',kind='read-avatar',
                              peer_id=numeric,photo_id=source_id)
        peer_policy(self.registry,agent,'read-avatars',numeric)
        return dict(result,downloaded_to_server=True,verified_original_sha=True)

    @staticmethod
    def attachment_identity(message,numeric,message_id):
        if (not message or message.id!=message_id or message.chat_id!=numeric
                or getattr(message,'noforwards',False)):
            raise ValueError('attachment_missing_private_or_protected')
        photo=isinstance(getattr(message,'media',None),types.MessageMediaPhoto)
        media=message.photo if photo else getattr(message,'document',None)
        if not media:raise ValueError('document_or_photo_required')
        file=message.file
        size=getattr(file,'size',None)
        if type(size) is not int or size<=0:raise ValueError('attachment_size_unavailable')
        mime='image/jpeg' if photo else (getattr(file,'mime_type',None) or 'application/octet-stream')
        return media.id,size,mime,photo

    async def attachment(self,agent,peer,message_id):
        if type(message_id) is not int or message_id<=0:raise ValueError('positive_message_id_required')
        entity,numeric=await self.resolve(agent,'read-attachments',peer)
        message=await asyncio.wait_for(self.client.get_messages(entity,ids=message_id),20)
        identity=self.attachment_identity(message,numeric,message_id)
        _,size,mime,photo=identity
        if size>self.store.maximum:raise ValueError('attachment_size_exceeded')
        for attempt in range(2):
            target=BoundedBytesIO(self.store.maximum)
            def progress(received,total):
                if received>size or total>size:raise ValueError('attachment_size_exceeded')
            try:
                await asyncio.wait_for(self.client.download_media(message,file=target,progress_callback=progress),35)
                break
            except Exception as exc:
                if type(exc).__name__ not in ('FileReferenceExpiredError','FileReferenceInvalidError') or attempt:raise
                peer_policy(self.registry,agent,'read-attachments',numeric)
                message=await asyncio.wait_for(self.client.get_messages(entity,ids=message_id),20)
                if self.attachment_identity(message,numeric,message_id)!=identity:raise ValueError('attachment_source_changed') from None
        data=target.getvalue()
        if len(data)!=size:raise ValueError('attachment_partial_read')
        if mime in ('image/jpeg','image/png','image/webp'):image_preview(data,mime)
        if mime=='application/pdf' and not data.startswith(b'%PDF-'):raise ValueError('attachment_pdf_header_invalid')
        peer_policy(self.registry,agent,'read-attachments',numeric)
        current=await asyncio.wait_for(self.client.get_messages(entity,ids=message_id),20)
        if self.attachment_identity(current,numeric,message_id)!=identity:raise ValueError('attachment_source_changed')
        name='photo.jpg' if photo else getattr(message.file,'name',None) or 'attachment.bin'
        name=re.sub(r'[/\\\x00-\x1f\x7f]','_',name)[:120]
        if name in ('.','..'):name='attachment.bin'
        result=self.store.put(agent,data,name,mime,kind='read-attachment',chat_id=numeric,message_id=message_id)
        peer_policy(self.registry,agent,'read-attachments',numeric)
        return dict(result,peer_id=numeric,downloaded_to_server=True,verified_original_sha=True)


def register_peer_profiles(runtime):
    from .agents import AgentRegistry
    from .config import private_dir
    from .file_transport import StagedFiles
    registry=AgentRegistry(private_dir()/'agents.json')
    store=StagedFiles(private_dir()/'files/agent-staging')
    lock=asyncio.Lock();cooldown={}
    async def invoke(agent,action,peer,account,method,*args):
        peer_policy(registry,agent,action,peer)
        async with lock:
            peer_policy(registry,agent,action,peer)
            key=account or '';remaining=cooldown.get(key,0)-time.monotonic()
            if remaining>0:return {'error':'telegram_cooldown','retry_after_seconds':int(remaining)+1}
            try:
                return await asyncio.wait_for(getattr(PeerBackend(runtime.get_client(account),registry,store),method)(agent,peer,*args),50)
            except Exception as exc:
                wait=getattr(exc,'seconds',None)
                if type(wait) is int and wait>0:
                    cooldown[key]=time.monotonic()+wait
                    return {'error':'telegram_cooldown','retry_after_seconds':wait}
                return {'error':'peer_read_unavailable'}
    annotation=runtime.ToolAnnotations(readOnlyHint=True,openWorldHint=True)
    @runtime.mcp.tool(annotations=annotation)
    @runtime.with_account(readonly=True)
    async def read_agent_peer_profile(agent_id:str,peer:Union[int,str],account:Optional[str]=None)->dict:
        """Read a permitted peer card; no account/session/access hash is returned."""
        return await invoke(agent_id,'read-profiles',peer,account,'profile')
    @runtime.mcp.tool(annotations=annotation)
    @runtime.with_account(readonly=True)
    async def list_agent_avatars(agent_id:str,peer:Union[int,str],offset:int=0,limit:int=10,account:Optional[str]=None)->dict:
        """User avatar history, or the current group/channel avatar; metadata only."""
        return await invoke(agent_id,'read-avatars',peer,account,'avatars',offset,limit)
    @runtime.mcp.tool(annotations=annotation)
    @runtime.with_account(readonly=True)
    async def download_agent_avatar(agent_id:str,peer:Union[int,str],photo_id:Optional[int]=None,account:Optional[str]=None)->dict:
        """One visible avatar into bounded private staging, no paths or writes."""
        return await invoke(agent_id,'read-avatars',peer,account,'avatar',photo_id)
    @runtime.mcp.tool(annotations=annotation)
    @runtime.with_account(readonly=True)
    async def download_agent_attachment(agent_id:str,peer:Union[int,str],message_id:int,account:Optional[str]=None)->dict:
        """Actual document/photo bytes from one permitted message into bounded staging."""
        return await invoke(agent_id,'read-attachments',peer,account,'attachment',message_id)
