"""VAST DataBase persistence for oiio-proxy-generator.

Writes proxy generation results to the proxy_outputs table in the same
exr_metadata schema used by exr-inspector. Both functions share file_id
computation so results can be JOINed on file_id.

Database path:
  bucket (VAST_DB_BUCKET) -> schema (VAST_DB_SCHEMA) -> table (proxy_outputs)

file_id computation (must match exr-inspector):
  file_id = SHA256(s3_key + mtime + MD5(s3_key))[:16]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("oiio-proxy-generator")

try:
    import pyarrow as pa
except ImportError:
    pa = None

try:
    import vastdb
except ImportError:
    vastdb = None

DEFAULT_VASTDB_ENDPOINT = ""
DEFAULT_VASTDB_BUCKET = os.environ.get("VAST_DB_BUCKET", "sergio-db")
DEFAULT_SCHEMA_NAME = os.environ.get("VAST_DB_SCHEMA", "exr_metadata_2")
DEFAULT_TABLE_NAME = "proxy_outputs"


# ============================================================================
# Table Schema
# ============================================================================

_PROXY_OUTPUTS_SCHEMA = None

def _get_schema() -> "pa.Schema":
    """Lazy-load schema to avoid import errors when pyarrow is unavailable."""
    global _PROXY_OUTPUTS_SCHEMA
    if _PROXY_OUTPUTS_SCHEMA is not None:
        return _PROXY_OUTPUTS_SCHEMA

    _PROXY_OUTPUTS_SCHEMA = pa.schema([
        ("proxy_id", pa.string()),              # UUID for this proxy record
        ("file_id", pa.string()),               # FK -> files.file_id (same computation as exr-inspector)
        ("s3_key", pa.string()),                # Source S3 object key
        ("s3_bucket", pa.string()),             # Source S3 bucket
        ("asset_id", pa.string()),              # Derived asset identifier
        ("thumbnail_s3_key", pa.string()),      # S3 key for thumbnail output
        ("proxy_s3_key", pa.string()),          # S3 key for proxy output
        ("thumbnail_size_bytes", pa.int64()),   # Thumbnail file size
        ("proxy_size_bytes", pa.int64()),       # Proxy file size
        ("source_size_bytes", pa.int64()),      # Original source file size
        ("source_colorspace", pa.string()),     # Detected source color space
        ("thumb_colorspace", pa.string()),      # Target: sRGB
        ("proxy_colorspace", pa.string()),      # Target: Rec.709
        ("thumb_resolution", pa.string()),      # "256x256"
        ("proxy_resolution", pa.string()),      # "1920x1080"
        ("processing_time_seconds", pa.float64()),  # Total processing time
        ("generated_timestamp", pa.string()),   # ISO 8601 UTC
        ("generator_version", pa.string()),     # oiio-proxy-generator version
    ])
    return _PROXY_OUTPUTS_SCHEMA


# ============================================================================
# file_id computation (must match exr-inspector exactly)
# ============================================================================

def compute_file_id(s3_key: str, mtime: str = "") -> str:
    """Compute file_id matching exr-inspector's algorithm.

    file_id = SHA256(path + mtime + MD5(path))[:16]

    This ensures proxy_outputs rows can JOIN with exr-inspector's files table.
    """
    path_hash = hashlib.md5(s3_key.encode()).hexdigest()
    file_id = hashlib.sha256(
        f"{s3_key}{mtime}{path_hash}".encode()
    ).hexdigest()[:16]
    return file_id


# ============================================================================
# VastDB Session
# ============================================================================

def _create_vastdb_session(ctx=None, event=None) -> Optional[Any]:
    """Create VAST DataBase session from ctx.secrets or environment."""
    if vastdb is None:
        logger.warning("vastdb SDK not available; skipping persistence")
        return None

    endpoint = ""
    access_key = ""
    secret_key = ""

    # Priority 1: ctx.secrets (production DataEngine path)
    secret_name = os.environ.get("VAST_DB_SECRET_NAME", "vast-db")
    if ctx is not None:
        try:
            secrets = ctx.secrets[secret_name]
            endpoint = secrets.get("endpoint", "")
            access_key = secrets.get("access_key", "")
            secret_key = secrets.get("secret_key", "")
        except Exception:
            logger.debug("ctx.secrets not available, falling back to env")

    # Priority 2: Environment variables
    if not endpoint:
        endpoint = (os.environ.get("VAST_DB_ENDPOINT")
                    or os.environ.get("S3_ENDPOINT")
                    or DEFAULT_VASTDB_ENDPOINT)
    if not access_key:
        access_key = os.environ.get("VAST_DB_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY") or ""
    if not secret_key:
        secret_key = os.environ.get("VAST_DB_SECRET_KEY") or os.environ.get("S3_SECRET_KEY") or ""

    if not endpoint or not access_key or not secret_key:
        logger.warning("VastDB credentials incomplete - persistence disabled")
        return None

    session = vastdb.connect(
        endpoint=endpoint,
        access=access_key,
        secret=secret_key,
    )
    return session


# ============================================================================
# DDL: Get-or-Create Pattern
# ============================================================================

def _get_or_create_schema(bucket, schema_name: str):
    """Get existing schema or create it. Handles race conditions."""
    try:
        return bucket.schema(schema_name)
    except Exception:
        pass
    try:
        logger.info("Creating schema: %s", schema_name)
        return bucket.create_schema(schema_name)
    except Exception as exc:
        logger.warning("create_schema race condition (%s), retrying get", exc)
        return bucket.schema(schema_name)


def _get_or_create_table(schema, table_name: str, arrow_schema: "pa.Schema"):
    """Get existing table or create it. Handles race conditions."""
    try:
        return schema.table(table_name)
    except Exception:
        pass
    try:
        logger.info("Creating table: %s", table_name)
        return schema.create_table(table_name, arrow_schema)
    except Exception as exc:
        logger.warning("create_table race condition (%s), retrying get", exc)
        return schema.table(table_name)


def ensure_database_tables(session) -> None:
    """Ensure proxy_outputs table exists in VAST DataBase.

    Uses get-or-create pattern. Safe to call from multiple pods concurrently.
    The schema (exr_metadata_2) is shared with exr-inspector -- this function
    only creates the proxy_outputs table, leaving exr-inspector's tables untouched.
    """
    if pa is None or session is None:
        return

    bucket_name = os.environ.get("VAST_DB_BUCKET", DEFAULT_VASTDB_BUCKET)
    schema_name = os.environ.get("VAST_DB_SCHEMA", DEFAULT_SCHEMA_NAME)

    with session.transaction() as tx:
        bucket = tx.bucket(bucket_name)
        schema = _get_or_create_schema(bucket, schema_name)
        _get_or_create_table(schema, DEFAULT_TABLE_NAME, _get_schema())

    logger.info("Database table verified: %s/%s/%s",
                bucket_name, schema_name, DEFAULT_TABLE_NAME)


# ============================================================================
# Persistence
# ============================================================================

def persist_proxy_to_vast_database(
    s3_key: str,
    s3_bucket: str,
    asset_id: str,
    thumbnail_s3_key: str,
    proxy_s3_key: str,
    thumbnail_size_bytes: int = 0,
    proxy_size_bytes: int = 0,
    source_size_bytes: int = 0,
    source_colorspace: str = "unknown",
    processing_time_seconds: float = 0.0,
    mtime: str = "",
    vastdb_session=None,
    ctx=None,
) -> Dict[str, Any]:
    """Persist proxy generation results to VAST DataBase.

    Computes file_id matching exr-inspector's algorithm so results can be
    JOINed across tables.

    Args:
        s3_key: Source file S3 key
        s3_bucket: Source file S3 bucket
        asset_id: Derived asset identifier
        thumbnail_s3_key: S3 key for generated thumbnail
        proxy_s3_key: S3 key for generated proxy
        thumbnail_size_bytes: Thumbnail file size
        proxy_size_bytes: Proxy file size
        source_size_bytes: Source file size
        source_colorspace: Detected source color space
        processing_time_seconds: Total processing time
        mtime: Source file modification time (ISO 8601)
        vastdb_session: Reusable VastDB session from init()
        ctx: VAST function context (for logging)

    Returns:
        Dict with status, file_id, proxy_id
    """
    if pa is None:
        return {"status": "skipped", "message": "pyarrow not available"}

    session = vastdb_session
    if session is None:
        session = _create_vastdb_session(ctx=ctx)
    if session is None:
        return {"status": "skipped", "message": "VastDB not configured"}

    try:
        import uuid

        file_id = compute_file_id(s3_key, mtime)
        proxy_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        from main import __version__

        row = pa.table(
            {
                "proxy_id": [proxy_id],
                "file_id": [file_id],
                "s3_key": [s3_key],
                "s3_bucket": [s3_bucket],
                "asset_id": [asset_id],
                "thumbnail_s3_key": [thumbnail_s3_key],
                "proxy_s3_key": [proxy_s3_key],
                "thumbnail_size_bytes": [thumbnail_size_bytes],
                "proxy_size_bytes": [proxy_size_bytes],
                "source_size_bytes": [source_size_bytes],
                "source_colorspace": [source_colorspace],
                "thumb_colorspace": ["sRGB"],
                "proxy_colorspace": ["Rec709"],
                "thumb_resolution": ["256x256"],
                "proxy_resolution": ["1920x1080"],
                "processing_time_seconds": [processing_time_seconds],
                "generated_timestamp": [now],
                "generator_version": [__version__],
            },
            schema=_get_schema(),
        )

        bucket_name = os.environ.get("VAST_DB_BUCKET", DEFAULT_VASTDB_BUCKET)
        schema_name = os.environ.get("VAST_DB_SCHEMA", DEFAULT_SCHEMA_NAME)

        with session.transaction() as tx:
            table = (tx.bucket(bucket_name)
                      .schema(schema_name)
                      .table(DEFAULT_TABLE_NAME))
            table.insert(row)

        logger.info("Persisted proxy_outputs: file_id=%s, proxy_id=%s", file_id, proxy_id)

        return {
            "status": "success",
            "file_id": file_id,
            "proxy_id": proxy_id,
            "inserted": True,
        }

    except Exception as exc:
        logger.error("VastDB persistence failed: %s", exc)
        return {"status": "error", "error": str(exc)}
