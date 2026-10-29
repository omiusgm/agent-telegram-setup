"""Private, durable request claims: never resend an uncertain write."""
import json
import hashlib
import os
from .saved_text import SendReceipts


class PollReceipts(SendReceipts):
    def __init__(self, folder, *, account_wide_guard=False):
        super().__init__(folder)
        self.account_wide_guard = account_wide_guard

    def guard_path(self, agent, fingerprint):
        scope = 'account' if self.account_wide_guard else agent
        key = hashlib.sha256((scope + '\0' + fingerprint).encode()).hexdigest()
        return self.folder / ('pending-' + key + '.json')

    @staticmethod
    def pending_result(pending, agent, request_id):
        result = {**pending, 'request_id':request_id, 'status':'uncertain_do_not_retry',
                  'readback_verified':False}
        if pending.get('owner_agent') == agent:
            result['original_request_id'] = pending['request_id']
        result.pop('owner_agent', None)
        return result

    def existing(self, agent, request_id, fingerprint):
        old = super().existing(agent, request_id, fingerprint)
        if old is not None: return old
        try:
            pending = self.read(self.guard_path(agent, fingerprint), fingerprint)
        except FileNotFoundError:
            return None
        return self.pending_result(pending, agent, request_id)

    def begin(self, agent, request_id, fingerprint, chat_id, operation):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = self.existing(agent, request_id, fingerprint)
        if old is not None: return self.path_for(agent, request_id), old
        path = self.path_for(agent, request_id)
        value = {'fingerprint': fingerprint, 'request_id': request_id, 'owner_agent': agent, 'chat_id': chat_id,
                 'operation': operation, 'status': 'uncertain_do_not_retry',
                 'readback_verified': False}
        guard = self.guard_path(agent, fingerprint)
        try:
            fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pending = self.read(guard, fingerprint)
            return path, self.pending_result(pending, agent, request_id)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        self.sync_folder()
        # If a crash occurs after the guard but before this file, the guard is
        # still enough to block a fresh request ID with the same payload.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            # No external call has occurred; release only this new fingerprint
            # guard, then verify the existing request's original payload.
            guard.unlink(missing_ok=True); self.sync_folder()
            return path, self.read(path, fingerprint)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        self.sync_folder()
        return path, None

    def clear_guard(self, agent, fingerprint, receipt):
        if receipt.get('status') in ('delivered_verified', 'closed_verified', 'rejected_not_sent'):
            self.guard_path(agent, fingerprint).unlink(missing_ok=True)
            self.sync_folder()
