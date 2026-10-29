"""Small, cloud-facade-only protection for BotFather's secret-bearing chat.

This is an output filter, not host isolation.  It does not protect secrets in
other processes, files, images, forwarded messages, or differently encoded text.
"""
import re

BOTFATHER_ID = 93372553
GUARD_REVISION = 'botfather-output-v1'
_TOKEN = re.compile(r'(?<![0-9])[0-9]{5,15}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])')


def is_sensitive_peer(peer):
    if type(peer) is int:
        return peer == BOTFATHER_ID
    if isinstance(peer, str):
        value = peer.strip().casefold()
        if value.isdigit():
            return value == str(BOTFATHER_ID)
        return value.lstrip('@') == 'botfather'
    return False


def deny_sensitive_peer(peer):
    if is_sensitive_peer(peer):
        raise PermissionError('sensitive_chat_not_available')


def redact_tokens(value):
    if isinstance(value, str):
        return _TOKEN.sub('[REDACTED_BOT_TOKEN]', value)
    if isinstance(value, list):
        return [redact_tokens(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_tokens(item) for key, item in value.items()}
    return value


def sanitize_account_search(payload):
    """Reject unexpected successful shapes instead of returning unscreened rows."""
    if not isinstance(payload, dict):
        raise RuntimeError('invalid_account_search_guard_shape')
    if payload.get('error'):
        return redact_tokens(payload)
    rows, verified = payload.get('messages'), payload.get('verified_chat_ids')
    if (not isinstance(rows, list) or not isinstance(verified, list)
            or any(not isinstance(row, dict) or type(row.get('chat_id')) is not int for row in rows)
            or any(type(ident) is not int for ident in verified)):
        raise RuntimeError('invalid_account_search_guard_shape')
    result = dict(payload)
    result['messages'] = [row for row in rows if not is_sensitive_peer(row['chat_id'])]
    result['verified_chat_ids'] = [ident for ident in verified if not is_sensitive_peer(ident)]
    removed = len(rows) - len(result['messages'])
    if type(result.get('omitted_records')) is int:
        result['omitted_records'] += removed
    result['coverage'] = 'current_account_dialogs_except_sensitive_chat'
    return redact_tokens(result)
