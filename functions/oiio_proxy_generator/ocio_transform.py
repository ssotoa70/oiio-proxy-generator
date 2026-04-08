"""Color space transformation using oiiotool CLI.

Supports two modes:
1. OCIO mode (OCIO_CONFIG_PATH set and valid): Full OCIO transform with ACES
   colorspace names (ACEScg, ARRI LogC, scene_linear, etc.)
2. Built-in mode (no OCIO config): Uses oiiotool's internal color management
   which supports: linear, sRGB, Rec709

For review proxies in a MAM application, built-in mode is sufficient.
Artists who need color-accurate grading reference the source EXR.
"""

import subprocess
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("oiio-proxy-generator")


class ColorspaceDetectionError(Exception):
    pass


# oiiotool built-in colorspace names (work without OCIO config)
_BUILTIN_COLORSPACE_MAP = {
    "linear": "linear",
    "scene_linear": "linear",
    "srgb": "sRGB",
    "rec709": "Rec709",
    "rec.709": "Rec709",
}

# OCIO/ACES colorspace names (require OCIO config to be set)
_OCIO_COLORSPACE_MAP = {
    "logc": "ARRI LogC",
    "logc3": "ARRI LogC",
    "logc4": "ARRI LogC4",
    "acescg": "ACEScg",
    "aces": "ACEScg",
}


@dataclass
class OcioTransform:
    config_path: str | None
    dev_mode: bool = False

    def __post_init__(self):
        if self.config_path is None:
            self.config_path = os.environ.get("OCIO_CONFIG_PATH", "")
        # Verify config exists; if not, use oiiotool built-in color management
        if self.config_path and not Path(self.config_path).exists():
            log.warning("OCIO config not found at %s -- using oiiotool built-in transforms",
                        self.config_path)
            self.config_path = ""
        if self.config_path:
            log.info("OCIO mode: config=%s", self.config_path)
        else:
            log.info("Color mode: oiiotool built-in (linear, sRGB, Rec709)")

    def apply(self, source: str, target_colorspace: str = "sRGB") -> str:
        """Apply color transform. In dev mode, returns source unchanged.

        Returns path to the transformed file (or source if no transform needed).
        """
        if self.dev_mode:
            log.info("[DEV] Color transform skipped for %s", source)
            return source

        if not Path(source).exists():
            raise FileNotFoundError(f"Source file not found: {source}")

        source_cs = self.detect_colorspace(source)
        if source_cs == target_colorspace:
            log.info("No transform needed: source is already %s", target_colorspace)
            return source

        # Generate output path for intermediate
        base, ext = os.path.splitext(source)
        output = f"{base}__{target_colorspace}{ext}"
        self._run_colorconvert(source, output, source_cs, target_colorspace)
        return output

    def detect_colorspace(self, source: str) -> str:
        """Detect source colorspace from EXR metadata attributes."""
        metadata = self._read_exr_metadata(source)

        # Priority 1: explicit colorspace attribute
        if cs_attr := metadata.get("colorspace"):
            return self._normalize_colorspace(str(cs_attr))

        # Priority 2: oiio:ColorSpace attribute
        if cs_attr := metadata.get("oiio:colorspace"):
            return self._normalize_colorspace(str(cs_attr))

        # Priority 3: chromaticities heuristic
        if chroma := metadata.get("chromaticities", ""):
            chroma_lower = str(chroma).lower()
            if "aces" in chroma_lower:
                return "ACEScg" if self.config_path else "linear"
            if "rec709" in chroma_lower or "rec.709" in chroma_lower:
                return "Rec709"

        # Default: linear (works with both OCIO and built-in modes)
        return "linear"

    def _normalize_colorspace(self, raw: str) -> str:
        """Normalize colorspace name to one oiiotool understands."""
        key = raw.lower().strip()
        # Try built-in names first (always available)
        if key in _BUILTIN_COLORSPACE_MAP:
            return _BUILTIN_COLORSPACE_MAP[key]
        # Try OCIO names (only if config is loaded)
        if self.config_path and key in _OCIO_COLORSPACE_MAP:
            return _OCIO_COLORSPACE_MAP[key]
        # If no OCIO config and name looks like an ACES name, fall back to linear
        if not self.config_path and key in _OCIO_COLORSPACE_MAP:
            log.warning("OCIO colorspace '%s' not available without config, using 'linear'", raw)
            return "linear"
        return raw

    def _read_exr_metadata(self, source: str) -> dict:
        """Read EXR metadata using oiiotool --info -v."""
        if not shutil.which("oiiotool"):
            return {}
        timeout = int(os.environ.get("OIIO_TIMEOUT", "300"))
        try:
            result = subprocess.run(
                ["oiiotool", "--info", "-v", source],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return {}
        metadata = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                metadata[key.strip().lower()] = value.strip()
        return metadata

    def _run_colorconvert(self, source: str, output: str, from_cs: str, to_cs: str) -> None:
        """Run oiiotool --colorconvert."""
        env = os.environ.copy()
        if self.config_path:
            env["OCIO"] = self.config_path
        else:
            # Ensure no stale OCIO env var interferes
            env.pop("OCIO", None)

        cmd = [
            "oiiotool", source,
            "--colorconvert", from_cs, to_cs,
            "-o", output,
        ]
        timeout = int(os.environ.get("OIIO_TIMEOUT", "300"))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ColorspaceDetectionError(
                f"oiiotool colorconvert timed out after {timeout}s"
            )
        except OSError as exc:
            raise ColorspaceDetectionError(f"Failed to execute oiiotool: {exc}")
        if result.returncode != 0:
            raise ColorspaceDetectionError(
                f"oiiotool colorconvert failed: {result.stderr}"
            )
        log.info("Color transform: %s -> %s -> %s", from_cs, to_cs, output)
