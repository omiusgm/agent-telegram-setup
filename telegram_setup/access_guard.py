"""Enforce the selected transcription destination before any provider call."""

import json
from typing import Optional, Union


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
