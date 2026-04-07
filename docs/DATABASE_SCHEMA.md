# Database Schema Reference

oiio-proxy-generator persists proxy generation results to the `proxy_outputs` table in VAST DataBase. The table resides in the shared `exr_metadata_2` schema, enabling cross-function queries with exr-inspector results.

## Schema Overview

The proxy_outputs table stores one row per generated proxy set (thumbnail + proxy video). The schema is auto-created on first invocation using the `vastdb` Python SDK with PyArrow.

```
$VAST_DB_BUCKET/
  exr_metadata_2/
    files            -- exr-inspector: one row per EXR file
    parts            -- exr-inspector: one row per subimage
    channels         -- exr-inspector: one row per channel/AOV
    attributes       -- exr-inspector: one row per EXR attribute
    proxy_outputs    -- oiio-proxy-generator: one row per proxy generation
```

## proxy_outputs Table

One row per proxy generation event, regardless of retry count. Includes output locations, file sizes, color space information, and timing metrics.

| Column | Type | Description |
|--------|------|-------------|
| `proxy_id` | STRING | UUID v4 for this proxy record (primary key) |
| `file_id` | STRING | Foreign key to exr-inspector's files.file_id. Enables JOIN on shared file_id. |
| `s3_key` | STRING | Source file S3 object key (e.g., `renders/shot_001/beauty.0001.exr`) |
| `s3_bucket` | STRING | Source S3 bucket name |
| `asset_id` | STRING | Derived asset identifier (MD5 hash of s3_key, first 16 chars) |
| `thumbnail_s3_key` | STRING | Output S3 key for generated thumbnail (e.g., `renders/shot_001/.proxies/beauty.0001_thumb.jpg`) |
| `proxy_s3_key` | STRING | Output S3 key for generated proxy video (e.g., `renders/shot_001/.proxies/beauty.0001_proxy.mp4`) |
| `thumbnail_size_bytes` | INT64 | JPEG thumbnail file size in bytes |
| `proxy_size_bytes` | INT64 | H.264 MP4 proxy file size in bytes |
| `source_size_bytes` | INT64 | Source media file size in bytes |
| `source_colorspace` | STRING | Detected source color space (e.g., `ACEScg`, `Rec.709`, `scene_linear`) |
| `thumb_colorspace` | STRING | Target color space for thumbnail (always `sRGB`) |
| `proxy_colorspace` | STRING | Target color space for proxy (always `Rec.709`) |
| `thumb_resolution` | STRING | Thumbnail output resolution (always `256x256`) |
| `proxy_resolution` | STRING | Proxy output resolution (always `1920x1080`) |
| `processing_time_seconds` | FLOAT64 | Total processing time in seconds (download + OCIO + resize + encode + upload) |
| `generated_timestamp` | STRING | ISO 8601 UTC timestamp of generation |
| `generator_version` | STRING | Version of oiio-proxy-generator that created this record |

## file_id Computation

The `file_id` column must match exr-inspector's computation exactly to enable reliable JOINs across tables.

**Algorithm:**

```python
import hashlib

def compute_file_id(s3_key: str, mtime: str = "") -> str:
    """Compute file_id matching exr-inspector exactly.
    
    file_id = SHA256(path + mtime + MD5(path))[:16]
    """
    path_hash = hashlib.md5(s3_key.encode()).hexdigest()
    file_id = hashlib.sha256(
        f"{s3_key}{mtime}{path_hash}".encode()
    ).hexdigest()[:16]
    return file_id
```

**Example:**

```python
# For s3_key = "renders/shot_001/beauty.0001.exr" and mtime = "2025-02-06T14:30:00Z"
path_hash = md5("renders/shot_001/beauty.0001.exr") = "abc123..."
file_id = sha256("renders/shot_001/beauty.0001.exrZ14:30:00Zabc123...") = "def456..."[:16] = "def456abc123def4"
```

## Relationship with exr-inspector

Both functions share the same `file_id` computation and schema namespace, enabling rich cross-functional queries:

| Lookup | Query |
|--------|-------|
| File metadata + proxy outputs | `SELECT e.*, p.thumbnail_s3_key, p.proxy_s3_key FROM files e LEFT JOIN proxy_outputs p ON e.file_id = p.file_id` |
| Deep EXR files with proxies | `SELECT e.file_path, p.proxy_s3_key FROM files e JOIN proxy_outputs p ON e.file_id = p.file_id WHERE e.is_deep = true` |
| Processing time breakdown | `SELECT e.file_path, e.size_bytes, p.processing_time_seconds FROM files e JOIN proxy_outputs p ON e.file_id = p.file_id WHERE p.processing_time_seconds > 30` |
| Color space distribution | `SELECT p.source_colorspace, COUNT(*) as count FROM proxy_outputs p GROUP BY p.source_colorspace` |

## Auto-Provisioning

The proxy_outputs table is created automatically on first invocation using a **get-or-create** pattern:

```python
# DDL runs in a separate transaction from inserts
with session.transaction() as tx:
    bucket = tx.bucket(bucket_name)
    schema = _get_or_create_schema(bucket, schema_name)
    _get_or_create_table(schema, "proxy_outputs", arrow_schema)
```

The `create_schema` and `create_table` calls are NOT idempotent in the vastdb SDK. The get-or-create pattern wraps them in try/except to handle concurrent first-run scenarios safely.

**Note:** The database bucket must pre-exist as a Database-enabled view. The SDK cannot create buckets.

## Query Examples (VastDB SDK)

Connect to VAST DataBase and run queries:

```python
from vastdb import connect

session = connect(
    endpoint="http://$DATA_VIP",
    access="$S3_ACCESS_KEY",
    secret="$S3_SECRET_KEY"
)

bucket_name = "sergio-db"
schema_name = "exr_metadata_2"
```

### Recent proxy generations

```python
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    
    results = table.select("""
    SELECT s3_key, thumbnail_s3_key, processing_time_seconds, generated_timestamp
    FROM proxy_outputs
    ORDER BY generated_timestamp DESC
    LIMIT 10
    """)
    
    for row in results:
        print(f"{row['s3_key']}: {row['processing_time_seconds']:.2f}s")
```

### Largest generated proxies

```python
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    
    results = table.select("""
    SELECT s3_key, source_size_bytes, proxy_size_bytes,
           CAST(proxy_size_bytes AS FLOAT) / source_size_bytes AS compression_ratio
    FROM proxy_outputs
    ORDER BY proxy_size_bytes DESC
    LIMIT 20
    """)
    
    for row in results:
        print(f"{row['s3_key']}: {row['proxy_size_bytes'] / (1024*1024):.1f}MB "
              f"({row['compression_ratio']:.2%} of source)")
```

### Color space distribution

```python
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    
    results = table.select("""
    SELECT source_colorspace, COUNT(*) as count,
           CAST(SUM(proxy_size_bytes) AS FLOAT) / (1024*1024*1024) as total_gb
    FROM proxy_outputs
    GROUP BY source_colorspace
    ORDER BY count DESC
    """)
    
    for row in results:
        print(f"{row['source_colorspace']}: {row['count']} files, {row['total_gb']:.1f}GB")
```

### Average processing time by file size bucket

```python
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    
    results = table.select("""
    SELECT
      CASE
        WHEN source_size_bytes < 100*1024*1024 THEN '<100MB'
        WHEN source_size_bytes < 500*1024*1024 THEN '100-500MB'
        WHEN source_size_bytes < 2*1024*1024*1024 THEN '500MB-2GB'
        ELSE '>2GB'
      END as size_bucket,
      COUNT(*) as count,
      AVG(processing_time_seconds) as avg_time,
      MAX(processing_time_seconds) as max_time
    FROM proxy_outputs
    GROUP BY size_bucket
    ORDER BY source_size_bytes
    """)
    
    for row in results:
        print(f"{row['size_bucket']}: {row['count']} files, "
              f"avg {row['avg_time']:.1f}s, max {row['max_time']:.1f}s")
```

### Files proxied by date

```python
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    
    results = table.select("""
    SELECT DATE(generated_timestamp) as date, COUNT(*) as count
    FROM proxy_outputs
    GROUP BY DATE(generated_timestamp)
    ORDER BY date DESC
    LIMIT 30
    """)
    
    for row in results:
        print(f"{row['date']}: {row['count']} files")
```

### JOIN with exr-inspector file metadata

```python
with session.transaction() as tx:
    schema = tx.bucket(bucket_name).schema(schema_name)
    
    # Execute JOIN query
    results = schema.query("""
    SELECT
      e.file_path,
      e.multipart_count,
      e.is_deep,
      e.size_bytes,
      p.thumbnail_s3_key,
      p.proxy_s3_key,
      p.processing_time_seconds
    FROM files e
    JOIN proxy_outputs p ON e.file_id = p.file_id
    WHERE e.is_deep = true
    ORDER BY e.size_bytes DESC
    LIMIT 50
    """)
    
    for row in results:
        print(f"{row['file_path']}: {row['size_bytes'] / (1024*1024):.1f}MB, "
              f"multipart={row['multipart_count']}, "
              f"processing={row['processing_time_seconds']:.1f}s")
```

## Performance Considerations

### Indexing

For optimal query performance on large tables, create indexes:

```sql
-- Index the foreign key for fast JOINs with exr-inspector
CREATE INDEX idx_proxy_outputs_file_id ON proxy_outputs (file_id);

-- Index timestamps for range queries
CREATE INDEX idx_proxy_outputs_timestamp ON proxy_outputs (generated_timestamp);

-- Index color space for grouping queries
CREATE INDEX idx_proxy_outputs_colorspace ON proxy_outputs (source_colorspace);
```

### Partitioning

For very large tables (>10M rows), partition by date:

```sql
-- Create parent table with daily partitions
CREATE TABLE proxy_outputs_partitioned
PARTITION BY RANGE (DATE(generated_timestamp)) AS
SELECT * FROM proxy_outputs;

-- Drop old partitions
ALTER TABLE proxy_outputs_partitioned DROP PARTITION
FOR ('2024-01-01');
```

## Data Retention

Proxy metadata rows accumulate over time. Consider:

- **Retention policy:** How long to keep proxy records (e.g., 1 year)
- **Archive strategy:** Export old records to S3 Parquet for long-term analysis
- **Purge script:** Automated cleanup of records older than N days

Example archive script:

```python
from vastdb import connect
from datetime import datetime, timedelta

session = connect(...)
days_old = 365
cutoff_date = datetime.utcnow() - timedelta(days=days_old)

with session.transaction() as tx:
    table = tx.bucket("sergio-db").schema("exr_metadata_2").table("proxy_outputs")
    
    # Export old records to Parquet
    table.execute(f"""
    COPY (SELECT * FROM proxy_outputs
          WHERE generated_timestamp < '{cutoff_date.isoformat()}')
    TO PARQUET 's3://backups/proxy_outputs_archive_{cutoff_date.date()}.parquet'
    """)
    
    # Delete archived records
    table.execute(f"""
    DELETE FROM proxy_outputs
    WHERE generated_timestamp < '{cutoff_date.isoformat()}'
    """)
    
    print(f"Archived and deleted records before {cutoff_date.date()}")
```
