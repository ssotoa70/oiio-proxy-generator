"""OpenImageIO processor for thumbnail and proxy generation.

Generates JPEG thumbnails and JPEG proxy frames using a single oiiotool
invocation per source file. Uses --dup/--pop stack operations to produce
both outputs from one EXR read, one OCIO config load, zero intermediates.

Proxy sizing strategy (Option C - smart native):
- Thumbnail: always 256x256 fit (preserves aspect ratio)
- Proxy: min(source_resolution, 1920x1080). Never upscales. Downscales 4K+ to HD.

Future enhancement (not yet implemented):
- Option B: Three-tier output (thumbnail + HD proxy + full-resolution preview)
  - Thumbnail: 256x256 for grid views
  - Proxy: 1920x1080 for quick review
  - Preview: full source resolution for detailed inspection/QC
  - Expected cost: +15-20% processing time per file

For scene-referred sources, caller passes colorspace (e.g., "linear").
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

    def read_dimensions(self, source: str) -> tuple[int, int]:
        """Read source image dimensions via oiiotool --info.

        Returns (width, height). Returns (0, 0) if dimensions cannot be read.
        """
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")
        try:
            result = subprocess.run(
                [self.oiiotool_bin, "--info", source],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return (0, 0)

        # oiiotool --info first line format: "path : WIDTH x HEIGHT, N channel, ..."
        import re
        match = re.search(r'(\d+)\s*x\s*(\d+)', result.stdout)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        return (0, 0)

    def generate_both(
        self,
        source: str,
        thumb_output: str,
        proxy_output: str,
        source_colorspace: str | None = None,
        thumb_width: int = 256,
        thumb_height: int = 256,
        proxy_max_width: int = 1920,
        proxy_max_height: int = 1080,
    ) -> tuple[int, int]:
        """Generate thumbnail and proxy in a single oiiotool invocation.

        Uses --dup/--pop to process the source once, producing both outputs
        with zero intermediate files.

        Proxy sizing: min(source_size, proxy_max). Never upscales.

        Args:
            source: Path to source image (EXR, DPX, TIFF, PNG, JPEG)
            thumb_output: Path for JPEG thumbnail output
            proxy_output: Path for JPEG proxy output
            source_colorspace: Source colorspace for conversion to sRGB.
                               None = skip colorconvert (display-referred input).
            thumb_width, thumb_height: Thumbnail dimensions (fit within)
            proxy_max_width, proxy_max_height: Proxy maximum dimensions.
                                               Proxy will be smaller if source is smaller.

        Returns:
            (proxy_width, proxy_height) actually used.
        """
        if not Path(source).exists():
            raise OiioError(f"Source file not found: {source}")

        # Read source dimensions to determine proxy size (don't upscale)
        src_w, src_h = self.read_dimensions(source)
        if src_w > 0 and src_h > 0:
            # Scale down to fit within max dimensions, preserving aspect ratio
            scale = min(proxy_max_width / src_w, proxy_max_height / src_h, 1.0)
            proxy_w = max(1, int(src_w * scale))
            proxy_h = max(1, int(src_h * scale))
        else:
            # Fallback if dimensions can't be read
            proxy_w = proxy_max_width
            proxy_h = proxy_max_height

        cmd = [
            self.oiiotool_bin,
            "--threads", str(self.threads),
            source,
            "--dup",
        ]

        # Thumbnail branch: colorconvert (if needed) + fit + JPEG
        if source_colorspace:
            cmd += ["--colorconvert", source_colorspace, "sRGB"]
        cmd += [
            "--fit:filter=triangle", f"{thumb_width}x{thumb_height}",
            "--compression", "jpeg:85",
            "-o", thumb_output,
            "--pop",
        ]

        # Proxy branch: colorconvert (if needed) + fit to computed size + JPEG
        if source_colorspace:
            cmd += ["--colorconvert", source_colorspace, "sRGB"]
        cmd += [
            "--fit:filter=lanczos3", f"{proxy_w}x{proxy_h}",
            "--compression", "jpeg:90",
            "-o", proxy_output,
        ]

        self._run(cmd)
        return (proxy_w, proxy_h)

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
