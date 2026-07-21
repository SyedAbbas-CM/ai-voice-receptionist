from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.core.config import settings

from ..base import TTSProvider


class LocalPiperTTS(TTSProvider):
    """Piper local TTS. Expects `piper` binary on PATH and a .onnx voice model."""

    name = "local"

    def __init__(self) -> None:
        self.binary = settings.piper_binary or "piper"
        self.model_path = settings.piper_model_path

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not shutil.which(self.binary):
            raise RuntimeError(f"Piper binary '{self.binary}' not found on PATH")
        model = voice or self.model_path
        if not model:
            raise RuntimeError("PIPER_MODEL_PATH not set and no voice override provided")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = Path(tmp.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--model", model, "--output_file", str(out_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate(text.encode("utf-8"))
            if proc.returncode != 0:
                raise RuntimeError(f"piper failed: {stderr.decode(errors='replace')}")
            return out_path.read_bytes(), "audio/wav"
        finally:
            out_path.unlink(missing_ok=True)
