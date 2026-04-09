"""OpenImageIO processor for thumbnail and proxy generation.

Generates JPEG thumbnails and JPEG proxy frames using a single oiiotool
invocation per source file. Uses --dup/--pop stack operations to produce
both outputs from one EXR read, one OCIO config load, zero intermediates.

For scene-referred sources, caller passes colorspace (e.g., "linear", "sRGB").
For display-referred sources, caller passes None to skip colorconvert.
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
    threads: int = 4

    def generate_both(
        self,
        source: str,
        thumb_output: str,
        proxy_output: str,
        source_colorspace: str | None = None,
        thumb_width: int = 256,
        thumb_height: int = 256,
        proxy_width: int = 1920,
        proxy_height: int = 1080,
    ) -> None:
        """Generate thumbnail and proxy in a single oiiotool invocation.

        Uses --dup/--pop to process the source once, producing both outputs
        with zero intermediate files.

        Args:
            source: Path to source image (EXR, DPX, TIFF, PNG, JPEG)
            thumb_output: Path for JPEG thumbnail output
            proxy_output: Path for JPEG proxy output
            source_colorspace: Source colorspace for conversion to sRGB.
                               None = skip colorconvert (display-referred input).
                               e.g., "linear", "ACEScg" (if OCIO config available)
        """
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")

        cmd = [
            self.oiiotool_bin,
            "--threads", str(self.threads),
            source,
            "--dup",
        ]

        # Thumbnail branch: colorconvert (if needed) + resize + JPEG
        if source_colorspace:
            cmd += ["--colorconvert", source_colorspace, "sRGB"]
        cmd += [
            "--resize:filter=triangle", f"{thumb_width}x{thumb_height}",
            "--compression", "jpeg:85",
            "-o", thumb_output,
            "--pop",
        ]

        # Proxy branch: colorconvert (if needed) + resize + JPEG
        if source_colorspace:
            cmd += ["--colorconvert", source_colorspace, "sRGB"]
        cmd += [
            "--resize:filter=lanczos3", f"{proxy_width}x{proxy_height}",
            "--compression", "jpeg:90",
            "-o", proxy_output,
        ]

        self._run(cmd)

    def generate_thumbnail(self, source: str, output: str, width: int = 256, height: int = 256) -> None:
        """Generate a single JPEG thumbnail (fallback for simple cases)."""
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        cmd = [
            self.oiiotool_bin,
            "--threads", str(self.threads),
            source,
            "--fit", f"{width}x{height}",
            "--compression", "jpeg:85",
            "-o", output,
        ]
        self._run(cmd)

    def generate_proxy(self, source: str, output: str, width: int = 1920, height: int = 1080) -> None:
        """Generate a single JPEG proxy (fallback for simple cases)."""
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        cmd = [
            self.oiiotool_bin,
            "--threads", str(self.threads),
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
