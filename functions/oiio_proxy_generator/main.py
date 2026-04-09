"""VAST DataEngine handler for oiio-proxy-generator.

Generates JPEG thumbnails and JPEG proxy frames from image files (EXR, DPX,
TIFF, PNG, JPEG) using OpenImageIO. No ffmpeg dependency.

For scene-referred sources (EXR, DPX, float TIFF):
  - Applies oiiotool colorconvert (linear->sRGB for thumb, linear->Rec709 for proxy)

For display-referred sources (PNG, JPEG, 8-bit TIFF):
  - Resize only, no color transform needed

Output path convention:
  Source:    s3://bucket/renders/shot_010/beauty.0001.exr
  Thumbnail: s3://bucket/renders/shot_010/.proxies/beauty.0001_thumb.jpg
  Proxy:     s3://bucket/renders/shot_010/.proxies/beauty.0001_proxy.jpg

Video files (.mov, .mp4, etc.) are handled by a separate video-proxy-generator function.
Sequence MP4 assembly is handled by the sequence-assembler function.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

__version__ = "3.0.0"

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    ClientError = Exception

from oiio_processor import OiioProcessor, OiioError
from ocio_transform import OcioTransform, ColorspaceDetectionError
from publisher import publish_proxy_generated
from vast_db_persistence import (
    persist_proxy_to_vast_database,
    _create_vastdb_session,
    ensure_database_tables,
)

# Image formats this function handles
SUPPORTED_EXTENSIONS = {".exr", ".dpx", ".tif", ".tiff", ".png", ".jpg", ".jpeg"}

# Scene-referred formats that need color transform before JPEG output
SCENE_REFERRED_EXTENSIONS = {".exr", ".dpx"}

# Global state -- initialized once in init(), reused for all requests
s3_client = None
vastdb_session = None
_tables_verified = False


def init(ctx):
    """One-time initialization when the function container starts."""
    global s3_client, vastdb_session, _tables_verified

    ctx.logger.info("=" * 80)
    ctx.logger.info("INITIALIZING OIIO-PROXY-GENERATOR %s", __version__)
    ctx.logger.info("=" * 80)

    s3_endpoint = os.environ.get("S3_ENDPOINT", "")
    s3_access_key = os.environ.get("S3_ACCESS_KEY", "")
    s3_secret_key = os.environ.get("S3_SECRET_KEY", "")

    ctx.logger.info("S3_ENDPOINT: %s", s3_endpoint or "(NOT SET)")

    if not s3_endpoint or not s3_access_key or not s3_secret_key:
        ctx.logger.warning("S3 credentials incomplete - S3 operations will fail")

    if boto3 is not None:
        from botocore.config import Config
        s3_config = Config(
            max_pool_connections=25,
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=5,
            read_timeout=30,
        )
        s3_client = boto3.client(
            "s3",
            endpoint_url=s3_endpoint,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
            config=s3_config,
        )
        ctx.logger.info("S3 client created")
    else:
        ctx.logger.error("boto3 not available")

    try:
        vastdb_session = _create_vastdb_session(ctx=ctx)
        if vastdb_session:
            ensure_database_tables(vastdb_session)
            _tables_verified = True
            ctx.logger.info("VastDB tables verified")
        else:
            ctx.logger.warning("VastDB not configured - persistence will be skipped")
    except Exception as exc:
        ctx.logger.error("VastDB init failed (will retry per-event): %s", exc)

    ctx.logger.info("oiiotool: %s", "available" if _check_tool("oiiotool") else "NOT AVAILABLE")
    ctx.logger.info("OIIO-PROXY-GENERATOR initialized successfully")
    ctx.logger.info("=" * 80)


def handler(ctx, event):
    """Primary DataEngine function handler."""
    ctx.logger.info("=" * 80)
    ctx.logger.info("Event ID: %s, Type: %s", event.id, event.type)

    # Extract file location from event
    s3_bucket, s3_key = _extract_location(ctx, event)
    if not s3_bucket or not s3_key:
        return _error_result("Missing S3 bucket/key - cannot locate source file")

    # Validate extension (also prevents loops from .proxies/ outputs)
    if not _is_supported_extension(s3_key):
        ctx.logger.info("Skipping unsupported file: %s", s3_key)
        return _error_result(f"Unsupported file extension: {s3_key}")

    # Skip proxy outputs (defense-in-depth against trigger loops)
    if "/.proxies/" in s3_key:
        ctx.logger.info("Skipping proxy output: %s", s3_key)
        return _error_result("Skipping proxy output file")

    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"

    source_path = None
    thumb_path = None
    proxy_path = None
    try:
        start_time = time.monotonic()
        asset_id = _derive_asset_id(s3_key)
        thumb_s3_key = _derive_output_key(s3_key, "_thumb.jpg")
        proxy_s3_key = _derive_output_key(s3_key, "_proxy.jpg")

        # Download source from S3
        source_path, s3_file_info = _download_from_s3(ctx, s3_bucket, s3_key)
        source_size = s3_file_info["size_bytes"]
        ctx.logger.info("Downloaded %s (%d bytes)", s3_key, source_size)

        # Temp output paths
        thumb_path = tempfile.mktemp(suffix="_thumb.jpg", prefix=f"{asset_id}_")
        proxy_path = tempfile.mktemp(suffix="_proxy.jpg", prefix=f"{asset_id}_")

        # Determine source colorspace for transform
        ext = os.path.splitext(s3_key)[1].lower()
        needs_colorconvert = _needs_color_transform(ext, source_path)

        # Detect source colorspace (for scene-referred formats)
        source_colorspace = None
        if needs_colorconvert:
            transform = OcioTransform(
                config_path=os.environ.get("OCIO_CONFIG_PATH"),
                dev_mode=dev_mode,
            )
            source_colorspace = transform.detect_colorspace(source_path)
            ctx.logger.info("Detected colorspace: %s", source_colorspace)

        # Single oiiotool invocation: read once, produce both outputs
        # Uses --dup/--pop stack ops, zero intermediates, one OCIO load
        processor = OiioProcessor()
        processor.generate_both(
            source=source_path,
            thumb_output=thumb_path,
            proxy_output=proxy_path,
            source_colorspace=source_colorspace,
        )
        ctx.logger.info("Thumbnail generated (%d bytes)", Path(thumb_path).stat().st_size)
        ctx.logger.info("Proxy generated (%d bytes)", Path(proxy_path).stat().st_size)

        thumb_size = Path(thumb_path).stat().st_size
        proxy_size = Path(proxy_path).stat().st_size

        # Upload outputs to S3
        if s3_client and not dev_mode:
            _upload_to_s3(ctx, s3_bucket, thumb_s3_key, thumb_path, media_type="thumbnail")
            _upload_to_s3(ctx, s3_bucket, proxy_s3_key, proxy_path, media_type="proxy")

        elapsed = time.monotonic() - start_time

        detected_colorspace = source_colorspace or "display-referred"

        # Persist to VastDB
        persistence_result = persist_proxy_to_vast_database(
            s3_key=s3_key,
            s3_bucket=s3_bucket,
            asset_id=asset_id,
            thumbnail_s3_key=thumb_s3_key,
            proxy_s3_key=proxy_s3_key,
            thumbnail_size_bytes=thumb_size,
            proxy_size_bytes=proxy_size,
            source_size_bytes=source_size,
            source_colorspace=detected_colorspace,
            processing_time_seconds=round(elapsed, 2),
            mtime=s3_file_info.get("mtime", ""),
            vastdb_session=vastdb_session,
            ctx=ctx,
        )

        # Publish Kafka event
        kafka_broker = os.environ.get("KAFKA_BROKER", "vastbroker:9092")
        kafka_topic = os.environ.get("KAFKA_TOPIC", "spaceharbor.proxy")
        publish_proxy_generated(
            asset_id=asset_id,
            thumbnail_uri=f"s3://{s3_bucket}/{thumb_s3_key}",
            proxy_uri=f"s3://{s3_bucket}/{proxy_s3_key}",
            thumbnail_size_bytes=thumb_size,
            proxy_size_bytes=proxy_size,
            source_size_bytes=source_size,
            broker=kafka_broker,
            topic=kafka_topic,
            dev_mode=dev_mode,
        )

        ctx.logger.info("=" * 80)
        ctx.logger.info("PROXY GENERATION COMPLETE:")
        ctx.logger.info("  Source: s3://%s/%s (%d bytes)", s3_bucket, s3_key, source_size)
        ctx.logger.info("  Thumbnail: %s (%d bytes)", thumb_s3_key, thumb_size)
        ctx.logger.info("  Proxy: %s (%d bytes)", proxy_s3_key, proxy_size)
        ctx.logger.info("  Color: %s, transform: %s", detected_colorspace, "yes" if needs_colorconvert else "no")
        ctx.logger.info("  Time: %.2fs, Persistence: %s", elapsed, persistence_result.get("status"))
        ctx.logger.info("=" * 80)

        return {
            "status": "success",
            "asset_id": asset_id,
            "source_key": s3_key,
            "thumbnail_key": thumb_s3_key,
            "proxy_key": proxy_s3_key,
            "thumbnail_size_bytes": thumb_size,
            "proxy_size_bytes": proxy_size,
            "source_size_bytes": source_size,
            "colorspace": detected_colorspace,
            "processing_time_seconds": round(elapsed, 2),
            "persistence": persistence_result,
        }

    except (OiioError, ColorspaceDetectionError) as exc:
        ctx.logger.error("Processing failed: %s", exc)
        return _error_result(f"Processing failed: {exc}")

    except Exception as exc:
        ctx.logger.error("Proxy generation failed: %s", exc)
        ctx.logger.exception(exc)
        return _error_result(f"Proxy generation failed: {exc}")

    finally:
        for path in [source_path, thumb_path, proxy_path]:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_location(ctx, event) -> tuple:
    """Extract S3 bucket and key from VastEvent."""
    s3_bucket = None
    s3_key = None

    if event.type == "Element":
        try:
            element_event = event.as_element_event()
            s3_bucket = element_event.bucket
            s3_key = element_event.object_key
            ctx.logger.info("Element: s3://%s/%s", s3_bucket, s3_key)
        except Exception as exc:
            ctx.logger.warning("Failed to extract Element properties: %s", exc)

    if not s3_bucket or not s3_key:
        event_data = event.get_data() if hasattr(event, "get_data") else {}
        s3_bucket = event_data.get("s3_bucket")
        s3_key = event_data.get("s3_key")

    if not s3_bucket or not s3_key:
        ctx.logger.error("Missing S3 bucket/key in event")

    return s3_bucket, s3_key


def _is_supported_extension(s3_key: str) -> bool:
    ext = os.path.splitext(s3_key)[1].lower()
    return ext in SUPPORTED_EXTENSIONS


def _needs_color_transform(ext: str, source_path: str) -> bool:
    """Determine if source needs colorconvert before JPEG output.

    Scene-referred formats (EXR, DPX) always need transform.
    TIFF depends on bit depth — float = scene-referred, uint8 = display-referred.
    PNG and JPEG are display-referred, skip transform.
    """
    if ext in SCENE_REFERRED_EXTENSIONS:
        return True
    if ext in (".tif", ".tiff"):
        # Check if float (scene-referred) or uint8 (display-referred)
        return _is_float_tiff(source_path)
    return False


def _is_float_tiff(source_path: str) -> bool:
    """Check if a TIFF file is floating-point (scene-referred)."""
    import shutil
    if not shutil.which("oiiotool"):
        return False
    try:
        result = subprocess.run(
            ["oiiotool", "--info", source_path],
            capture_output=True, text=True, timeout=10,
        )
        # oiiotool --info output contains format type like "float" or "uint8"
        return "float" in result.stdout.lower()
    except Exception:
        return False


def _derive_asset_id(s3_key: str) -> str:
    import hashlib
    return hashlib.md5(s3_key.encode()).hexdigest()[:16]


def _derive_output_key(source_key: str, suffix: str) -> str:
    """Example: renders/shot_010/beauty.0001.exr -> renders/shot_010/.proxies/beauty.0001_thumb.jpg"""
    parent = os.path.dirname(source_key)
    stem = os.path.splitext(os.path.basename(source_key))[0]
    return f"{parent}/.proxies/{stem}{suffix}"


def _download_from_s3(ctx, bucket: str, key: str) -> tuple:
    if s3_client is None:
        raise RuntimeError("S3 client not initialized")

    ext = os.path.splitext(key)[1]
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_path = tmp.name
    tmp.close()

    ctx.logger.info("Downloading s3://%s/%s", bucket, key)
    s3_client.download_file(bucket, key, tmp_path)

    file_size = os.path.getsize(tmp_path)
    mtime = ""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        if head.get("LastModified"):
            mtime = head["LastModified"].isoformat()
    except Exception:
        pass

    return tmp_path, {"size_bytes": file_size, "mtime": mtime}


def _upload_to_s3(ctx, bucket: str, key: str, local_path: str,
                  media_type: str = "unknown") -> None:
    if s3_client is None:
        ctx.logger.warning("S3 client not initialized, skipping upload")
        return

    content_type = "image/jpeg"  # All outputs are JPEG

    ctx.logger.info("Uploading s3://%s/%s (type=%s, content=%s)",
                     bucket, key, media_type, content_type)
    s3_client.upload_file(
        local_path, bucket, key,
        ExtraArgs={
            "ContentType": content_type,
            "Tagging": f"media_type={media_type}&generator=oiio-proxy-generator&version={__version__}",
        },
    )


def _check_tool(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _error_result(message: str) -> Dict[str, Any]:
    return {"status": "error", "error": message}


# Need subprocess for _is_float_tiff
import subprocess
