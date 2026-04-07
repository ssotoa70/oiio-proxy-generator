"""Tests for VAST DataBase persistence layer.

Tests schema creation, file_id computation, and PyArrow table construction
without requiring a live VAST cluster.
"""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone

import pytest

from vast_db_persistence import (
    compute_file_id,
    _get_schema,
    persist_proxy_to_vast_database,
    ensure_database_tables,
    _get_or_create_schema,
    _get_or_create_table,
    DEFAULT_TABLE_NAME,
)


class TestComputeFileId:
    """file_id computation must match exr-inspector exactly."""

    def test_deterministic(self):
        """Same inputs produce same file_id."""
        id1 = compute_file_id("renders/shot_010/beauty.0001.exr", "2026-04-01T00:00:00Z")
        id2 = compute_file_id("renders/shot_010/beauty.0001.exr", "2026-04-01T00:00:00Z")
        assert id1 == id2

    def test_different_paths_different_ids(self):
        id1 = compute_file_id("renders/shot_010/beauty.0001.exr")
        id2 = compute_file_id("renders/shot_020/beauty.0001.exr")
        assert id1 != id2

    def test_different_mtime_different_ids(self):
        id1 = compute_file_id("renders/beauty.0001.exr", "2026-04-01T00:00:00Z")
        id2 = compute_file_id("renders/beauty.0001.exr", "2026-04-02T00:00:00Z")
        assert id1 != id2

    def test_matches_exr_inspector_algorithm(self):
        """Verify algorithm matches: SHA256(path + mtime + MD5(path))[:16]"""
        s3_key = "renders/shot_010/beauty.0001.exr"
        mtime = "2026-04-01T00:00:00Z"

        path_hash = hashlib.md5(s3_key.encode()).hexdigest()
        expected = hashlib.sha256(
            f"{s3_key}{mtime}{path_hash}".encode()
        ).hexdigest()[:16]

        assert compute_file_id(s3_key, mtime) == expected

    def test_length_16_hex(self):
        fid = compute_file_id("test.exr")
        assert len(fid) == 16
        assert all(c in "0123456789abcdef" for c in fid)

    def test_empty_mtime_still_works(self):
        fid = compute_file_id("test.exr", "")
        assert len(fid) == 16


class TestSchema:
    """proxy_outputs table schema validation."""

    def test_schema_has_required_fields(self):
        schema = _get_schema()
        field_names = [f.name for f in schema]
        required = [
            "proxy_id", "file_id", "s3_key", "s3_bucket", "asset_id",
            "thumbnail_s3_key", "proxy_s3_key",
            "thumbnail_size_bytes", "proxy_size_bytes", "source_size_bytes",
            "source_colorspace", "thumb_colorspace", "proxy_colorspace",
            "thumb_resolution", "proxy_resolution",
            "processing_time_seconds", "generated_timestamp", "generator_version",
        ]
        for name in required:
            assert name in field_names, f"Missing field: {name}"

    def test_schema_field_count(self):
        schema = _get_schema()
        assert len(schema) == 18

    def test_file_id_is_string(self):
        import pyarrow as pa
        schema = _get_schema()
        file_id_field = schema.field("file_id")
        assert file_id_field.type == pa.string()

    def test_size_fields_are_int64(self):
        import pyarrow as pa
        schema = _get_schema()
        for name in ["thumbnail_size_bytes", "proxy_size_bytes", "source_size_bytes"]:
            assert schema.field(name).type == pa.int64()


class TestGetOrCreate:
    """DDL get-or-create pattern handles race conditions."""

    def test_get_or_create_schema_existing(self):
        bucket = MagicMock()
        bucket.schema.return_value = "existing_schema"
        result = _get_or_create_schema(bucket, "test_schema")
        assert result == "existing_schema"

    def test_get_or_create_schema_creates_new(self):
        bucket = MagicMock()
        bucket.schema.side_effect = Exception("not found")
        bucket.create_schema.return_value = "new_schema"
        result = _get_or_create_schema(bucket, "test_schema")
        assert result == "new_schema"

    def test_get_or_create_schema_race_condition(self):
        bucket = MagicMock()
        bucket.schema.side_effect = [Exception("not found"), "raced_schema"]
        bucket.create_schema.side_effect = Exception("already exists")
        result = _get_or_create_schema(bucket, "test_schema")
        assert result == "raced_schema"

    def test_get_or_create_table_existing(self):
        schema = MagicMock()
        schema.table.return_value = "existing_table"
        result = _get_or_create_table(schema, "test_table", MagicMock())
        assert result == "existing_table"

    def test_get_or_create_table_creates_new(self):
        schema = MagicMock()
        schema.table.side_effect = Exception("not found")
        schema.create_table.return_value = "new_table"
        result = _get_or_create_table(schema, "test_table", MagicMock())
        assert result == "new_table"


class TestPersistProxy:
    """Integration tests for persist_proxy_to_vast_database."""

    def test_skipped_when_no_session(self):
        result = persist_proxy_to_vast_database(
            s3_key="test.exr",
            s3_bucket="test-bucket",
            asset_id="abc123",
            thumbnail_s3_key="test_thumb.jpg",
            proxy_s3_key="test_proxy.mp4",
            vastdb_session=None,
        )
        assert result["status"] == "skipped"

    @patch("vast_db_persistence.vastdb")
    def test_successful_persistence(self, mock_vastdb):
        mock_session = MagicMock()
        mock_tx = MagicMock()
        mock_table = MagicMock()

        mock_session.transaction.return_value.__enter__ = MagicMock(return_value=mock_tx)
        mock_session.transaction.return_value.__exit__ = MagicMock(return_value=False)
        mock_tx.bucket.return_value.schema.return_value.table.return_value = mock_table

        result = persist_proxy_to_vast_database(
            s3_key="renders/shot_010/beauty.0001.exr",
            s3_bucket="test-bucket",
            asset_id="abc123",
            thumbnail_s3_key="renders/shot_010/.proxies/beauty.0001_thumb.jpg",
            proxy_s3_key="renders/shot_010/.proxies/beauty.0001_proxy.mp4",
            thumbnail_size_bytes=45000,
            proxy_size_bytes=2500000,
            source_size_bytes=98000000,
            source_colorspace="ACEScg",
            processing_time_seconds=12.5,
            vastdb_session=mock_session,
        )

        assert result["status"] == "success"
        assert result["file_id"] is not None
        assert result["proxy_id"] is not None
        assert result["inserted"] is True
        mock_table.insert.assert_called_once()

    @patch("vast_db_persistence.vastdb")
    def test_persistence_error_returns_error_status(self, mock_vastdb):
        mock_session = MagicMock()
        mock_session.transaction.side_effect = Exception("connection failed")

        result = persist_proxy_to_vast_database(
            s3_key="test.exr",
            s3_bucket="test-bucket",
            asset_id="abc123",
            thumbnail_s3_key="test_thumb.jpg",
            proxy_s3_key="test_proxy.mp4",
            vastdb_session=mock_session,
        )

        assert result["status"] == "error"
        assert "connection failed" in result["error"]
