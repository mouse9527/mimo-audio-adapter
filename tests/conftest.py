import base64
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Strip inherited proxy vars: httpx would otherwise route mocked calls through
# a SOCKS proxy and fail before respx can intercept them.
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)

os.environ.setdefault("MIMO_BASE_URL", "https://mimo.test/v1")
os.environ.setdefault("DEFAULT_TTS_VOICE", "冰糖")

MIMO_URL = "https://mimo.test/v1/chat/completions"


def wav_bytes(n_samples: int = 240) -> bytes:
    pcm = b"\x00\x01" * n_samples
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def mp3_bytes() -> bytes:
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 64


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture
def auth():
    return {"Authorization": "Bearer upstream-key-from-litellm"}
