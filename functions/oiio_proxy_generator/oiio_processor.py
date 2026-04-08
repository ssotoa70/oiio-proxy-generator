"""OpenImageIO processor for thumbnail and proxy generation.

Generates JPEG thumbnails and JPEG proxy frames from EXR, DPX, TIFF, PNG,
and JPEG sources using oiiotool CLI. No ffmpeg dependency.

For display-referred sources (PNG, JPEG, 8-bit TIFF), color transform is
skipped and only resize is applied. For scene-referred sources (EXR, DPX,
float TIFF), the caller applies colorconvert before calling these methods.
"""

import os
import subprocess
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
        """Generate a JPEG thumbnail, fit within dimensions (preserves aspect ratio)."""
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        cmd = [
            self.oiiotool_bin,
            source,
            "--fit", f"{width}x{height}",
            "--compression", "jpeg:85",
            "-o", output,
        ]
        self._run(cmd)

    def generate_proxy(self, source: str, output: str, width: int = 1920, height: int = 1080) -> None:
        """Generate a JPEG proxy frame, fit within dimensions (preserves aspect ratio)."""
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        cmd = [
            self.oiiotool_bin,
            source,
            "--fit", f"{width}x{height}",
            "--compression", "jpeg:90",
            "-o", output,
        ]
        self._run(cmd)

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
