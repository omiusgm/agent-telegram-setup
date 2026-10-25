"""Private login worker. JSON uses pipes only; stdout must never reach logs."""
import asyncio
import base64
from datetime import datetime, timezone
import io
import json
import logging
import re
import select
import sys
import time

import qrcode
from telethon import errors

from .config import connection_lock, load_credentials, save_credentials
from .login import authorize


def emit(state, **values):
    print(json.dumps({'state': state, **values}), flush=True)


async def receive(field):
    line = await read_line(600)
    if not line or len(line) > 16384:
        raise RuntimeError('input_closed')
    data = json.loads(line)
    if not isinstance(data, dict) or not isinstance(data.get(field), str):
        raise ValueError('invalid_input')
    return data[field]


async def read_line(timeout):
    # A timed-out to_thread(readline) leaves a blocked executor thread alive.
    # select has its own deadline, so the worker can actually exit on timeout.
    ready, _, _ = await asyncio.to_thread(select.select, [sys.stdin], [], [], timeout)
    if not ready:
        raise RuntimeError('input_closed')
    return sys.stdin.readline(16385)


async def password_step(client):
    for attempt in range(3):
        emit('password', invalid=attempt > 0)
        password = await receive('password')
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            pass
        finally:
            password = None
    raise RuntimeError('password_attempts_exhausted')


async def qr_step(client):
    qr = await client.qr_login()
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        seconds = max(1, (qr.expires - datetime.now(timezone.utc)).total_seconds())
        waiter = asyncio.create_task(qr.wait(timeout=seconds))
        await asyncio.sleep(0)  # Install the Telegram update listener before showing a code.
        try:
            image = qrcode.make(qr.url, box_size=7, border=4)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            emit('qr', png=base64.b64encode(buffer.getvalue()).decode('ascii'), expires_in=int(seconds))
            await waiter
            return
        except asyncio.TimeoutError:
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            await password_step(client)
            return
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    raise RuntimeError('login_expired')


async def phone_step(client):
    emit('phone')
    phone = (await receive('phone')).strip()
    if not re.fullmatch(r'\+[0-9]{8,15}', phone):
        raise ValueError('invalid_phone')
    code = None
    try:
        await client.send_code_request(phone)
        emit('code')
        code = (await receive('code')).replace(' ', '')
        if not code.isdecimal() or not 4 <= len(code) <= 8:
            raise ValueError('invalid_code')
        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            await password_step(client)
    finally:
        phone = code = None


async def run():
    first = await read_line(30)
    if len(first) > 16384:
        raise ValueError('invalid_input')
    request = json.loads(first)
    if not isinstance(request, dict) or request.get('method') not in ('qr', 'phone'):
        raise ValueError('invalid_method')
    # The same lock protects Terminal, browser login and the shared backend.
    with connection_lock():
        data = load_credentials()
        if not data:
            api_id, api_hash = request.get('api_id', ''), request.get('api_hash', '')
            if (not isinstance(api_id, str) or not api_id.isdecimal() or int(api_id) <= 0
                    or not isinstance(api_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash)):
                raise ValueError('invalid_api_configuration')
            data = {'api_id': int(api_id), 'api_hash': api_hash, 'authorized': False}
            save_credentials(data)
        method = request['method']
        request.clear()
        emit('connecting')
        # Existing login owns the account check and credential/session storage.
        flow = qr_step if method == 'qr' else phone_step
        await authorize(data, login_flow=flow, announce=lambda _message: None)


def main():
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run())
        emit('authorized')
        return 0
    except errors.FloodWaitError as error:
        emit('error', code='flood_wait', retry_after_seconds=error.seconds)
    except errors.PasswordHashInvalidError:
        emit('error', code='password_invalid')
    except errors.AuthKeyUnregisteredError:
        emit('error', code='login_expired')
    except errors.PhoneCodeInvalidError:
        emit('error', code='code_invalid')
    except errors.PhoneCodeExpiredError:
        emit('error', code='code_expired')
    except RuntimeError as error:
        known = {'input_closed', 'password_attempts_exhausted', 'login_expired'}
        emit('error', code=str(error) if str(error) in known else 'session_busy_or_account_mismatch')
    except ValueError:
        emit('error', code='invalid_input')
    except Exception:
        emit('error', code='connection_failed')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
