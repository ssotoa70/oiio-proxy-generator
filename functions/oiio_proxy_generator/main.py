"""VAST DataEngine handler for oiio-proxy-generator.

Triggered by Element.ObjectCreated events on a VAST S3 bucket. Generates
thumbnails and proxies using OpenImageIO with OCIO color transforms, and
persists results to VAST DataBase.

Supports two I/O modes controlled by the NFS_MOUNT_PATH env var:

  NFS mode (NFS_MOUNT_PATH set):
    Source read directly from NFS mount -- no S3 download.
    Outputs written to NFS -- no S3 upload.
    OCIO intermediates also on NFS. Fastest path.

  S3 mode (NFS_MOUNT_PATH unset):
    Source downloaded via boto3 to /tmp.
    Outputs uploaded back to S3 after generation.
    Requires ephemeral disk for all intermediates.

Event flow:
  ElementTrigger -> VastEvent with elementpath -> bucket/object_key
  NFS_MOUNT_PATH -> derive local path: {NFS_MOUNT_PATH}/{bucket}/{key}
  S3 credentials -> fallback when NFS not available
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

__version__ = "1.0.0"

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

SUPPORTED_EXTENSIONS = {".exr", ".dpx", ".tif", ".tiff"}

# Global state -- initialized once in init(), reused for all requests
s3_client = None
vastdb_session = None
_tables_verified = False


def init(ctx):
    """One-time initialization when the function container starts.

    Sets up S3 client, VastDB session, and verifies database tables.
    All three are created once and reused for every request.
    """
    global s3_client, vastdb_session, _tables_verified

    ctx.logger.info("=" * 80)
    ctx.logger.info("INITIALIZING OIIO-PROXY-GENERATOR %s", __version__)
    ctx.logger.info("=" * 80)

    # --- S3 client ---
    s3_endpoint = os.environ.get("S3_ENDPOINT", "")
    s3_access_key = os.environ.get("S3_ACCESS_KEY", "")
    s3_secret_key = os.environ.get("S3_SECRET_KEY", "")

    ctx.logger.info("S3_ENDPOINT: %s", s3_endpoint or "(NOT SET)")
    ctx.logger.info("S3_ACCESS_KEY: %s...%s (len=%d)",
                     s3_access_key[:4], s3_access_key[-4:] if len(s3_access_key) > 8 else "***",
                     len(s3_access_key))

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
        ctx.logger.info("S3 client created (pool=25, retries=adaptive)")
    else:
        ctx.logger.error("boto3 not available")

    # --- VastDB session (reused for all events) ---
    try:
        vastdb_session = _create_vastdb_session(ctx=ctx)
        if vastdb_session:
            ctx.logger.info("VastDB session created")
            ensure_database_tables(vastdb_session)
            _tables_verified = True
            ctx.logger.info("Database tables verified")
        else:
            ctx.logger.warning("VastDB not configured - persistence will be skipped")
    except Exception as exc:
        ctx.logger.error("VastDB init failed (will retry per-event): %s", exc)

    # --- I/O mode ---
    nfs_mount = os.environ.get("NFS_MOUNT_PATH", "")
    if nfs_mount:
        ctx.logger.info("I/O mode: NFS (mount=%s)", nfs_mount)
    else:
        ctx.logger.info("I/O mode: S3 (set NFS_MOUNT_PATH to use direct NFS)")

    ctx.logger.info("oiiotool: %s", "available" if _check_tool("oiiotool") else "NOT AVAILABLE")
    ctx.logger.info("ffmpeg: %s", "available" if _check_tool("ffmpeg") else "NOT AVAILABLE")
    ctx.logger.info("OIIO-PROXY-GENERATOR initialized successfully")
    ctx.logger.info("=" * 80)


def handler(ctx, event):
    """Primary DataEngine function handler.

    Receives VastEvent objects from DataEngine element triggers.
    For Element events, extracts bucket/key from the elementpath extension.
    Downloads the file via the global S3 client, generates thumbnail and proxy,
    and persists results to VAST DataBase.

    Args:
        ctx: VAST function context with logger
        event: VastEvent object (ElementTriggerVastEvent, etc.)
    """
    ctx.logger.info("=" * 80)
    ctx.logger.info("Processing new proxy generation request")

    # Log event metadata
    ctx.logger.info("Event ID: %s", event.id)
    ctx.logger.info("Event Type: %s", event.type)
    ctx.logger.info("Event Subtype: %s", event.subtype if event.subtype else "None")

    # Extract file location from event
    s3_bucket = None
    s3_key = None

    if event.type == "Element":
        try:
            element_event = event.as_element_event()
            s3_bucket = element_event.bucket
            s3_key = element_event.object_key

            ctx.logger.info("Element event - Trigger: %s, ID: %s",
                            event.trigger, event.trigger_id)
            ctx.logger.info("Element path: %s",
                            element_event.extensions.get("elementpath"))
            ctx.logger.info("Bucket: %s, Key: %s", s3_bucket, s3_key)
        except Exception as exc:
            ctx.logger.warning("Failed to extract Element properties: %s", exc)

    # Fallback: check data payload
    if not s3_bucket or not s3_key:
        event_data = event.get_data() if hasattr(event, "get_data") else {}
        ctx.logger.info("Using data payload: %s", json.dumps(event_data, indent=2))
        s3_bucket = event_data.get("s3_bucket")
        s3_key = event_data.get("s3_key")

    if not s3_bucket or not s3_key:
        ctx.logger.error("Missing S3 bucket/key in event")
        return _error_result("Missing S3 bucket/key - cannot locate source file")

    # Validate extension
    if not _is_supported_extension(s3_key):
        ctx.logger.info("Skipping unsupported file: %s", s3_key)
        return _error_result(f"Unsupported file extension: {s3_key}")

    # Resolve I/O mode: NFS direct (with S3 fallback) or S3 only
    nfs_mount = os.environ.get("NFS_MOUNT_PATH", "")
    use_nfs = False
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"

    # Process the file
    source_path = None
    thumb_path = None
    proxy_path = None
    _s3_downloaded = False  # Track whether we need to clean up a downloaded file
    try:
        start_time = time.monotonic()
        asset_id = _derive_asset_id(s3_key)
        thumb_s3_key = _derive_output_key(s3_key, "_thumb.jpg")
        proxy_s3_key = _derive_output_key(s3_key, "_proxy.mp4")

        # Try NFS first if configured, fall back to S3 if unavailable
        if nfs_mount:
            nfs_source = _nfs_path(nfs_mount, s3_bucket, s3_key)
            if Path(nfs_source).exists():
                use_nfs = True
                source_path = nfs_source
                source_size = Path(source_path).stat().st_size

                # Output paths on NFS (visible via S3 immediately)
                thumb_path = _nfs_path(nfs_mount, s3_bucket, thumb_s3_key)
                proxy_path = _nfs_path(nfs_mount, s3_bucket, proxy_s3_key)
                Path(thumb_path).parent.mkdir(parents=True, exist_ok=True)

                ctx.logger.info("NFS mode: source=%s (%d bytes)", source_path, source_size)
            else:
                ctx.logger.warning(
                    "NFS path not accessible: %s -- falling back to S3", nfs_source)

        if not use_nfs:
            # S3 mode: download to /tmp, upload results after
            source_path, s3_file_info = _download_from_s3(ctx, s3_bucket, s3_key)
            _s3_downloaded = True
            source_size = s3_file_info["size_bytes"]
            ctx.logger.info("S3 mode: downloaded %s (%d bytes)", s3_key, source_size)

            thumb_path = tempfile.mktemp(suffix="_thumb.jpg", prefix=f"{asset_id}_")
            proxy_path = tempfile.mktemp(suffix="_proxy.mp4", prefix=f"{asset_id}_")

        # Configure OCIO and OIIO
        processor = OiioProcessor()
        transform = OcioTransform(
            config_path=os.environ.get("OCIO_CONFIG_PATH"),
            dev_mode=dev_mode,
        )

        # Step 1: OCIO transform to sRGB -> generate thumbnail
        transformed_for_thumb = transform.apply(source_path, target_colorspace="sRGB")
        processor.generate_thumbnail(transformed_for_thumb, thumb_path, width=256, height=256)
        ctx.logger.info("Thumbnail generated: %s", thumb_path)

        # Step 2: OCIO transform to Rec.709 -> generate proxy
        transformed_for_proxy = transform.apply(source_path, target_colorspace="Rec.709")
        processor.generate_proxy(transformed_for_proxy, proxy_path, width=1920, height=1080)
        ctx.logger.info("Proxy generated: %s", proxy_path)

        # Measure output sizes
        thumb_size = Path(thumb_path).stat().st_size if Path(thumb_path).exists() else 0
        proxy_size = Path(proxy_path).stat().st_size if Path(proxy_path).exists() else 0

        # S3 mode only: upload outputs
        if not use_nfs and s3_client and not dev_mode:
            _upload_to_s3(ctx, s3_bucket, thumb_s3_key, thumb_path)
            _upload_to_s3(ctx, s3_bucket, proxy_s3_key, proxy_path)

        elapsed = time.monotonic() - start_time

        # Detect color space for metadata
        detected_colorspace = "unknown"
        try:
            detected_colorspace = transform.detect_colorspace(source_path)
        except Exception:
            pass

        # Persist to VAST DataBase
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

        io_mode = "NFS" if use_nfs else "S3"
        ctx.logger.info("=" * 80)
        ctx.logger.info("PROXY GENERATION RESULTS (%s mode):", io_mode)
        ctx.logger.info("  Source: s3://%s/%s (%d bytes)", s3_bucket, s3_key, source_size)
        ctx.logger.info("  Thumbnail: %s (%d bytes)", thumb_s3_key, thumb_size)
        ctx.logger.info("  Proxy: %s (%d bytes)", proxy_s3_key, proxy_size)
        ctx.logger.info("  Color space: %s", detected_colorspace)
        ctx.logger.info("  Processing time: %.2fs", elapsed)
        ctx.logger.info("  Persistence: %s", persistence_result.get("status"))
        ctx.logger.info("=" * 80)

        return {
            "status": "success",
            "io_mode": io_mode,
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
        # Clean up temporary files (S3 mode only -- NFS outputs are permanent)
        if _s3_downloaded and source_path and os.path.exists(source_path):
            try:
                os.unlink(source_path)
            except OSError:
                pass
        if not use_nfs:
            for path in [thumb_path, proxy_path]:
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        # Clean up OCIO intermediate files (both modes)
        if source_path:
            for suffix in ["__sRGB.exr", "__Rec_709.exr"]:
                intermediate = source_path.replace(".exr", suffix)
                if os.path.exists(intermediate):
                    try:
                        os.unlink(intermediate)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nfs_path(nfs_mount: str, bucket: str, key: str) -> str:
    """Derive local NFS path from S3 bucket/key.

    VAST exposes the same data via S3 and NFS. An object at
    s3://bucket/path/file.exr is accessible at {NFS_MOUNT_PATH}/bucket/path/file.exr
    """
    return os.path.join(nfs_mount, bucket, key)


def _is_supported_extension(s3_key: str) -> bool:
    ext = os.path.splitext(s3_key)[1].lower()
    return ext in SUPPORTED_EXTENSIONS


def _derive_asset_id(s3_key: str) -> str:
    """Derive a stable asset ID from the S3 key.

    Uses the filename without extension and frame number as the base.
    """
    import hashlib
    return hashlib.md5(s3_key.encode()).hexdigest()[:16]


def _derive_output_key(source_key: str, suffix: str) -> str:
    """Derive output S3 key from source key.

    Example: renders/shot_010/beauty.0001.exr -> renders/shot_010/.proxies/beauty.0001_thumb.jpg
    """
    parent = os.path.dirname(source_key)
    stem = os.path.splitext(os.path.basename(source_key))[0]
    return f"{parent}/.proxies/{stem}{suffix}"


def _download_from_s3(ctx, bucket: str, key: str) -> tuple:
    """Download file from S3 to a temporary location. Returns (local_path, file_info)."""
    if s3_client is None:
        raise RuntimeError("S3 client not initialized")

    ext = os.path.splitext(key)[1]
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_path = tmp.name
    tmp.close()

    ctx.logger.info("Downloading s3://%s/%s", bucket, key)
    s3_client.download_file(bucket, key, tmp_path)

    # Get file size
    file_size = os.path.getsize(tmp_path)

    # Get S3 metadata for mtime
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        mtime = head.get("LastModified", "").isoformat() if head.get("LastModified") else ""
    except Exception:
        mtime = ""

    return tmp_path, {"size_bytes": file_size, "mtime": mtime}


def _upload_to_s3(ctx, bucket: str, key: str, local_path: str) -> None:
    """Upload a local file to S3."""
    if s3_client is None:
        ctx.logger.warning("S3 client not initialized, skipping upload")
        return
    ctx.logger.info("Uploading to s3://%s/%s", bucket, key)
    s3_client.upload_file(local_path, bucket, key)


def _check_tool(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _error_result(message: str) -> Dict[str, Any]:
    return {"status": "error", "error": message}
