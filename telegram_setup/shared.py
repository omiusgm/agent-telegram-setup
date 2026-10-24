"""One session holder, multiple independent MCP sessions, no TCP listener."""

import argparse
import sys

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import types
from mcp.shared.message import SessionMessage

from .config import connection_lock
from .relay import socket_path
from .server import prepare

IDLE_SECONDS = 10
MAX_LINE_BYTES = 4 * 1024**2


def should_keep_alive(settings):
    """An explicit permission change permits idle shutdown and a clean restart."""
    from .owned_inventory import has_pending_inventory
    from .permissions import load_settings
    try:
        return load_settings() == settings and has_pending_inventory()
    except (OSError, ValueError, RuntimeError):
        return False


async def serve_socket(mcp, path, *, idle_seconds=IDLE_SECONDS, keep_alive=None):
    # Caller holds the account lock. A second backend cannot unlink a live socket.
    path.unlink(missing_ok=True)
    listener = await anyio.create_unix_listener(str(path), mode=0o600)
    active = 0
    last_disconnect = anyio.current_time()

    async def connection(stream):
        nonlocal active, last_disconnect
        active += 1
        inbound, reader = anyio.create_memory_object_stream(0)
        writer, outbound = anyio.create_memory_object_stream(0)

        async def read():
            try:
                buffered = BufferedByteReceiveStream(stream)
                async with inbound:
                    while True:
                        line = await buffered.receive_until(b'\n', MAX_LINE_BYTES)
                        await inbound.send(SessionMessage(types.JSONRPCMessage.model_validate_json(line)))
            except (anyio.EndOfStream, anyio.IncompleteRead, anyio.ClosedResourceError,
                    anyio.BrokenResourceError):
                pass

        async def write():
            async with outbound:
                async for item in outbound:
                    await stream.send((item.message.model_dump_json(
                        by_alias=True, exclude_none=True) + '\n').encode())

        try:
            async with stream, anyio.create_task_group() as group:
                group.start_soon(read)
                group.start_soon(write)
                try:
                    await mcp._mcp_server.run(reader, writer,
                        mcp._mcp_server.create_initialization_options())
                finally:
                    group.cancel_scope.cancel()
        except Exception as exc:
            # Protocol failures belong to this MCP session, never the account process.
            print('Local MCP client closed: ' + type(exc).__name__, file=sys.stderr)
        finally:
            await reader.aclose()
            await writer.aclose()
            active -= 1
            last_disconnect = anyio.current_time()

    try:
        async with listener, anyio.create_task_group() as group:
            group.start_soon(listener.serve, connection)
            while True:
                await anyio.sleep(min(0.2, idle_seconds))
                if (not active and anyio.current_time() - last_disconnect >= idle_seconds
                        and not (keep_alive is not None and keep_alive())):
                    group.cancel_scope.cancel()
                    break
    finally:
        path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--persistent', action='store_true',
                        help='Keep the shared backend alive under a Linux service manager')
    options = parser.parse_args(argv)
    try:
        with connection_lock():
            runner = prepare()

            async def shared_transport(_transport):
                await serve_socket(runner.mcp, socket_path(),
                    keep_alive=lambda: options.persistent or should_keep_alive(runner._telegram_setup_permissions))

            runner._serve = shared_transport
            runner.main()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print('Shared MCP stopped: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
