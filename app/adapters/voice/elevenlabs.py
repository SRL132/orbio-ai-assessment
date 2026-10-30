"""Optional server-side text-to-speech with ElevenLabs (docs: /v1/text-to-speech/{voice_id}).

Used by the voice page when ELEVENLABS_API_KEY is set; otherwise the browser's own
speechSynthesis speaks the replies. Only the agent's replies are sent here, never what the
candidate said, and no text is logged.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("app.tts")

BASE_URL = "https://api.elevenlabs.io"
MAX_CHARS = 600


class ElevenLabsTTS:
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not voice_id:
            raise ValueError("ELEVENLABS_VOICE_ID is not set (see .env.example)")
        self._voice_id = voice_id
        self._model_id = model_id
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={"xi-api-key": api_key, "Accept": "audio/mpeg"},
        )

    def synthesize(self, text: str) -> bytes:
        """MP3 audio for one agent reply. The multilingual model infers es/en from the text."""
        started = time.monotonic()
        response = self._client.post(
            f"/v1/text-to-speech/{self._voice_id}",
            params={"output_format": "mp3_44100_128"},
            json={"text": text[:MAX_CHARS], "model_id": self._model_id},
        )
        response.raise_for_status()
        logger.info(
            "tts provider=elevenlabs chars=%d bytes=%d latency_ms=%d",
            min(len(text), MAX_CHARS),
            len(response.content),
            (time.monotonic() - started) * 1000,
        )
        return response.content
