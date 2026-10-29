"""Per-agent poll facade: exact targets, durable claims and independent readback."""
from pathlib import Path
import time

from .agents import AccessDenied, check_poll_target
from .poll_common import fingerprint, numeric_chat, positive_message, valid_request_id, validate_poll
from .poll_receipts import PollReceipts
from .sensitive_peer import deny_sensitive_peer, is_sensitive_peer


class PollGateway:
    def __init__(self, api, receipt_dir):
        self.api = api
        self.create_receipts = PollReceipts(Path(receipt_dir) / 'poll-create')
        self.close_receipts = PollReceipts(Path(receipt_dir) / 'poll-close')

    def _cooldown(self, result):
        if isinstance(result, dict):
            wait = result.get('retry_after_seconds')
            if type(wait) in (int, float) and wait > 0:
                self.api.retry_at = time.monotonic() + int(wait)

    def _ready(self, name):
        available = getattr(self.api.upstream, 'available_tools', None)
        return available is None or name in available

    async def _owner(self):
        owner = await self.api.upstream.call('get_me', {})
        self._cooldown(owner)
        if (not isinstance(owner, dict) or owner.get('type') != 'user'
                or type(owner.get('id')) is not int or owner['id'] <= 0):
            raise AccessDenied('owner_identity_unavailable')
        return owner['id']

    def _policy(self, agent, action, chat_id, owner_id, *, write):
        deny_sensitive_peer(chat_id)
        check_poll_target(self.api.registry.profile(agent), action, chat_id, owner_id, write=write)

    def _wait(self):
        remain = self.api.retry_at - time.monotonic()
        return int(remain) + 1 if remain > 0 else 0

    async def destinations(self, agent):
        profile = self.api.registry.profile(agent)
        if not {'create-polls','close-own-polls'} & set(profile.get('actions', [])):
            raise AccessDenied('action_not_permitted')
        async with self.api.lock:
            profile = self.api.registry.profile(agent)
            if not {'create-polls','close-own-polls'} & set(profile.get('actions', [])):
                raise AccessDenied('action_not_permitted')
            if self._wait(): return {'error':'telegram_cooldown','retry_after_seconds':self._wait()}
            saved_allowed = profile.get('poll_write_saved', False) or profile.get('poll_write_all_chats', False)
            owner = await self._owner() if saved_allowed else None
            profile = self.api.registry.profile(agent)
            if not {'create-polls','close-own-polls'} & set(profile.get('actions', [])):
                raise AccessDenied('action_not_permitted')
            saved_allowed = profile.get('poll_write_saved', False) or profile.get('poll_write_all_chats', False)
            return {'saved_chat_id': owner if saved_allowed else None,
                    'all_readable_chats': profile.get('poll_write_all_chats', False),
                    'group_chat_ids': [i for i in profile.get('poll_write_chat_ids', []) if not is_sensitive_peer(i)]}

    async def read(self, agent, chat_id, message_id):
        numeric_chat(chat_id); positive_message(message_id)
        self.api.policy(agent, 'read-polls')
        async with self.api.lock:
            self.api.policy(agent, 'read-polls')
            if self._wait(): return {'error':'telegram_cooldown','retry_after_seconds':self._wait()}
            if not self._ready('agent_read_poll'): return {'error':'poll_read_backend_not_enabled'}
            owner = await self._owner()
            self._policy(agent, 'read-polls', chat_id, owner, write=False)
            result = await self.api.upstream.call('agent_read_poll', {'chat_id':chat_id,'message_id':message_id})
            self._cooldown(result)
            self._policy(agent, 'read-polls', chat_id, owner, write=False)
            if not isinstance(result, dict): return {'error':'poll_read_failed'}
            if result.get('error'):
                return ({'error':'telegram_cooldown','retry_after_seconds':result['retry_after_seconds']}
                        if type(result.get('retry_after_seconds')) is int else {'error':'poll_read_failed'})
            if result.get('chat_id') != chat_id or result.get('message_id') != message_id:
                return {'error':'poll_read_identity_mismatch'}
            return dict(result, untrusted_content=True)

    async def create(self, agent, chat_id, question, options, request_id, anonymous=True,
                     multiple_choice=False, quiz_correct_index=None):
        numeric_chat(chat_id); valid_request_id(request_id)
        value = validate_poll(question, options, anonymous, multiple_choice, quiz_correct_index)
        self.api.policy(agent, 'create-polls')
        digest = fingerprint('create', chat_id=chat_id, **value)
        async with self.api.lock:
            self.api.policy(agent, 'create-polls')
            if self._wait(): return {'error':'telegram_cooldown','sent':False,'retry_after_seconds':self._wait()}
            owner = await self._owner()
            self._policy(agent, 'create-polls', chat_id, owner, write=True)
            old = self.create_receipts.existing(agent, request_id, digest)
            if old is not None: return {k:v for k,v in old.items() if k != 'fingerprint'}
            if not all(self._ready(n) for n in ('agent_create_poll','agent_read_poll')):
                return {'error':'poll_backend_not_enabled','sent':False}
            path, old = self.create_receipts.begin(agent, request_id, digest, chat_id, 'create')
            if old is not None: return {k:v for k,v in old.items() if k != 'fingerprint'}
            receipt = {'request_id':request_id,'chat_id':chat_id,'operation':'create',
                       'status':'uncertain_do_not_retry','readback_verified':False}
            try:
                self._policy(agent, 'create-polls', chat_id, owner, write=True)
                result = await self.api.upstream.call('agent_create_poll',
                    {'chat_id':chat_id,'question':question,'options':options,'request_id':request_id,
                     'agent_id':agent,'anonymous':anonymous,'multiple_choice':multiple_choice,
                     'quiz_correct_index':quiz_correct_index})
                self._cooldown(result)
                if isinstance(result,dict) and type(result.get('retry_after_seconds')) is int:
                    receipt['retry_after_seconds'] = result['retry_after_seconds']
                if (isinstance(result, dict) and result.get('sent') is False
                        and result.get('error') in ('public_votes_not_allowed_in_channel','telegram_cooldown','poll_target_not_available','poll_private_peer_not_supported')):
                    receipt.update(status='rejected_not_sent', error=result['error'])
                if isinstance(result, dict) and type(result.get('message_id')) is int and result['message_id'] > 0:
                    message_id = result['message_id']; receipt['message_id'] = message_id
                    self._policy(agent, 'create-polls', chat_id, owner, write=True)
                    snap = await self.api.upstream.call('agent_read_poll', {'chat_id':chat_id,'message_id':message_id})
                    self._cooldown(snap)
                    if (result.get('status') == 'delivered_verified' and result.get('readback_verified') is True
                            and isinstance(snap, dict) and snap.get('chat_id') == chat_id
                            and snap.get('message_id') == message_id and snap.get('question') == question
                            and [x.get('text') for x in snap.get('options', []) if isinstance(x, dict)] == options
                            and snap.get('anonymous') is anonymous and snap.get('multiple_choice') is multiple_choice
                            and snap.get('quiz') is (quiz_correct_index is not None) and snap.get('closed') is False
                            and snap.get('poll_id') == result.get('poll_id')):
                        receipt.update(status='delivered_verified',readback_verified=True,poll_id=snap['poll_id'])
                        if quiz_correct_index is not None:
                            visible = [i for i, item in enumerate(snap['options']) if item.get('correct') is True]
                            receipt['quiz_correct_answer_server_verified'] = visible == [quiz_correct_index]
                            if visible and visible != [quiz_correct_index]:
                                receipt.update(status='readback_mismatch_do_not_retry',readback_verified=False)
            except Exception:
                pass
            self.create_receipts.finish(path, digest, receipt)
            self.create_receipts.clear_guard(agent, digest, receipt)
            self._policy(agent, 'create-polls', chat_id, owner, write=True)
            return receipt

    async def close(self, agent, chat_id, message_id, request_id):
        numeric_chat(chat_id); positive_message(message_id); valid_request_id(request_id)
        self.api.policy(agent, 'close-own-polls')
        digest = fingerprint('close', chat_id=chat_id, message_id=message_id)
        async with self.api.lock:
            self.api.policy(agent, 'close-own-polls')
            if self._wait(): return {'error':'telegram_cooldown','closed':False,'retry_after_seconds':self._wait()}
            owner = await self._owner()
            self._policy(agent, 'close-own-polls', chat_id, owner, write=True)
            old = self.close_receipts.existing(agent, request_id, digest)
            if old is not None: return {k:v for k,v in old.items() if k != 'fingerprint'}
            if not all(self._ready(n) for n in ('agent_close_own_poll','agent_read_poll')):
                return {'error':'poll_backend_not_enabled','closed':False}
            # Prove ownership before claiming or dispatching a closing operation.
            before = await self.api.upstream.call('agent_read_poll', {'chat_id':chat_id,'message_id':message_id})
            self._cooldown(before)
            if isinstance(before,dict) and type(before.get('retry_after_seconds')) is int:
                return {'error':'telegram_cooldown','closed':False,'retry_after_seconds':before['retry_after_seconds']}
            if (not isinstance(before,dict) or before.get('chat_id') != chat_id
                    or before.get('message_id') != message_id or before.get('owner_created') is not True):
                return {'error':'poll_not_verified_as_own','closed':False}
            self._policy(agent, 'close-own-polls', chat_id, owner, write=True)
            path, old = self.close_receipts.begin(agent, request_id, digest, chat_id, 'close')
            if old is not None: return {k:v for k,v in old.items() if k != 'fingerprint'}
            receipt = {'request_id':request_id,'chat_id':chat_id,'message_id':message_id,'operation':'close',
                       'status':'uncertain_do_not_retry','readback_verified':False}
            try:
                self._policy(agent, 'close-own-polls', chat_id, owner, write=True)
                result = await self.api.upstream.call('agent_close_own_poll',
                    {'chat_id':chat_id,'message_id':message_id,'request_id':request_id,'agent_id':agent})
                self._cooldown(result)
                if isinstance(result,dict) and type(result.get('retry_after_seconds')) is int:
                    receipt['retry_after_seconds'] = result['retry_after_seconds']
                if (isinstance(result,dict) and result.get('closed') is False
                        and result.get('error') in ('poll_not_owned_by_account','telegram_cooldown','poll_target_not_available')):
                    receipt.update(status='rejected_not_sent', error=result['error'])
                self._policy(agent, 'close-own-polls', chat_id, owner, write=True)
                snap = await self.api.upstream.call('agent_read_poll', {'chat_id':chat_id,'message_id':message_id})
                self._cooldown(snap)
                if (isinstance(result,dict) and result.get('status') == 'closed_verified'
                        and result.get('readback_verified') is True and isinstance(snap,dict)
                        and snap.get('chat_id') == chat_id and snap.get('message_id') == message_id
                        and snap.get('poll_id') == before.get('poll_id') and snap.get('closed') is True):
                    receipt.update(status='closed_verified',readback_verified=True,poll_id=snap['poll_id'])
            except Exception:
                pass
            self.close_receipts.finish(path, digest, receipt)
            self.close_receipts.clear_guard(agent, digest, receipt)
            self._policy(agent, 'close-own-polls', chat_id, owner, write=True)
            return receipt
