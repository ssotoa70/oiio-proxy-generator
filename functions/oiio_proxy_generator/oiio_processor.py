"""OpenImageIO processor for thumbnail and proxy generation.

Wraps oiiotool CLI for image resize operations and ffmpeg for H.264 encoding.

Thumbnail pipeline: oiiotool resize -> JPEG (quality 85)
Proxy pipeline:     oiiotool resize -> PNG intermediate -> ffmpeg H.264 MP4
                    with -movflags +faststart for browser streaming
"""

import os
import subprocess
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass

log = logging.getLogger("oiio-proxy-generator")


class OiioError(Exception):
    pass


@dataclass
class OiioProcessor:
    oiiotool_bin: str = "oiiotool"

    def generate_thumbnail(self, source: str, output: str, width: int = 256, height: int = 256) -> None:
        """Generate a JPEG thumbnail from an EXR/DPX source."""
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        cmd = [
            self.oiiotool_bin,
            source,
            "--resize", f"{width}x{height}",
            "--compression", "jpeg:85",
            "-o", output,
        ]
        self._run(cmd)

    def generate_proxy(self, source: str, output: str, width: int = 1920, height: int = 1080) -> None:
        """Generate an H.264 proxy MP4 from an EXR/DPX source.

        Two-step process:
        1. oiiotool resizes to PNG intermediate
        2. ffmpeg encodes to H.264 with -movflags +faststart for browser streaming
        """
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        if not shutil.which("ffmpeg"):
            raise OiioError("ffmpeg not found in PATH -- required for proxy encoding")

        # Step 1: oiiotool resize to PNG intermediate
        intermediate = output.replace(".mp4", "_intermediate.png")
        resize_cmd = [
            self.oiiotool_bin,
            source,
            "--resize", f"{width}x{height}",
            "-o", intermediate,
        ]
        self._run(resize_cmd)

        # Step 2: ffmpeg encode to H.264 MP4
        # -movflags +faststart: moves moov atom to start for browser streaming
        # -pix_fmt yuv420p: required for browser compatibility
        # -preset fast: balance between speed and compression
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", intermediate,
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output,
        ]
        self._run(ffmpeg_cmd)
        Path(intermediate).unlink(missing_ok=True)

    def _run(self, cmd: list[str]) -> None:
        timeout = int(os.environ.get("OIIO_TIMEOUT", "300"))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise OiioError(f"Command timed out after {timeout}s: {cmd[0]}")
        except OSError as exc:
            raise OiioError(f"Failed to execute {cmd[0]}: {exc}")
        if result.returncode != 0:
            raise OiioError(f"{cmd[0]} failed: {result.stderr}")
