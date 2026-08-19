"""ffmpeg helpers.

Video notes (кружки) and forwarded videos carry speech that Scribe can read,
but only after the audio track is separated. ffmpeg ships in the image; a
missing binary or a broken container is reported, never raised.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 300


async def extract_audio(source: Path, destination: Path) -> bool:
    """Write ``source``'s audio track to ``destination``. False on any failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "64k",
            str(destination),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError):
        log.exception("ffmpeg is not available")
        return False

    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        log.error("ffmpeg timed out on %s", source.name)
        return False

    if process.returncode != 0:
        log.error(
            "ffmpeg failed on %s: %s",
            source.name,
            stderr.decode(errors="replace")[-400:],
        )
        return False
    return destination.is_file() and destination.stat().st_size > 0
