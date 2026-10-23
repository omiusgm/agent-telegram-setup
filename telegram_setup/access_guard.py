"""Enforce the selected transcription destination before any provider call."""

import json
from functools import wraps
from typing import Optional, Union


def enforce_permission_snapshot(mcp, settings):
    """Refuse stale permissions in a backend that outlives its stdio clients."""
    from mcp.server.fastmcp.exceptions import ToolError
    from .permissions import load_settings, validate_settings

    expected = validate_settings(settings)
    original = mcp._tool_manager.call_tool

    @wraps(original)
    async def guarded(*args, **kwargs):
        try:
            current = load_settings()
        except (OSError, ValueError, RuntimeError):
            raise ToolError('Permissions unavailable; restart the shared backend after fixing the selection') from None
        if current != expected:
            raise ToolError('Permissions changed; restart the shared backend before further calls')
        return await original(*args, **kwargs)

    mcp._tool_manager.call_tool = guarded


def enforce_transcription_engine(mcp, settings):
    selected = settings["transcription_engine"]
    if selected is None:
        return
    original = mcp._tool_manager.get_tool("transcribe_voice")
    if original is None:
        raise RuntimeError("Закреплённый upstream не содержит transcribe_voice.")

    async def transcribe_voice(chat_id: Union[int, str], message_id: int,
                               engine: Optional[str] = None, account: Optional[str] = None) -> str:
        if engine is not None and engine.strip().lower() != selected:
            return json.dumps({"transcribed": False, "reason": "transcription_engine_not_permitted",
                               "allowed_engine": selected})
        return await original.fn(chat_id=chat_id, message_id=message_id, engine=selected, account=account)

    mcp.remove_tool("transcribe_voice")
    mcp.add_tool(transcribe_voice, name="transcribe_voice", title="Transcribe Voice",
                 description="Расшифровать выбранное голосовое или видеокружок. Разрешённый способ: " + selected
                             + ". Другой engine отклоняется до обращения к провайдеру.",
                 annotations=original.annotations)
