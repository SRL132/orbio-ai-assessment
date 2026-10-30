import json

import httpx
import pytest

from app.adapters.voice.elevenlabs import MAX_CHARS, ElevenLabsTTS


def test_synthesize_posts_text_and_returns_audio() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"})

    tts = ElevenLabsTTS("key", "voice123", transport=httpx.MockTransport(handler))
    audio = tts.synthesize("Hola " * 200)
    assert audio == b"ID3fake-mp3"
    request = seen[0]
    assert request.url.path == "/v1/text-to-speech/voice123"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == "key"
    body = json.loads(request.content)
    assert body["model_id"] == "eleven_multilingual_v2"
    assert len(body["text"]) == MAX_CHARS


def test_errors_raise() -> None:
    tts = ElevenLabsTTS(
        "key", "v", transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={}))
    )
    with pytest.raises(httpx.HTTPStatusError):
        tts.synthesize("hola")


def test_voice_id_is_required() -> None:
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        ElevenLabsTTS("key", "")
