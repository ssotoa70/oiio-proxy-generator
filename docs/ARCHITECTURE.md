# Architecture

## Overview

oiio-proxy-generator is a stateless serverless function that runs on VAST DataEngine. It processes high-resolution media files (EXR, DPX, TIF) as they are ingested into a VAST S3 bucket, applying color space transforms via OpenColorIO and generating thumbnail and proxy outputs suitable for web preview and VFX review workflows.

## Event Flow

```
                    VAST S3 Bucket
                         |
                  [EXR/DPX uploaded]
                         |
                         v
              DataEngine Element Trigger
            (ElementCreated, suffix: .exr/.dpx)
                         |
                    [CloudEvent]
                         |
                         v
          oiio-proxy-generator container
         +-----------------------------------+
         |  init(ctx)                        |
         |    - Create S3 client (boto3)     |
         |    - Create VastDB session        |
         |    - Verify database tables       |
         |    - Validate oiiotool/ffmpeg     |
         +-----------------------------------+
         |  handler(ctx, event)              |
         |    1. Parse VastEvent             |
         |    2. Extract bucket/key          |
         |    3. S3 download (full file)     |
         |    4. Detect OCIO colorspace      |
         |    5. OCIO transform (sRGB)       |
         |    6. Generate thumbnail (OIIO)   |
         |    7. OCIO transform (Rec.709)    |
         |    8. Generate proxy (ffmpeg)     |
         |    9. S3 upload outputs           |
         |    10. Persist to VAST DataBase   |
         |    11. Publish Kafka event        |
         |    12. Cleanup temp files         |
         +-----------------------------------+
              |              |         |
              v              v         v
          VAST S3        VastDB    Kafka Broker
        (upload)      (metadata)  (spaceharbor.proxy)
```

## Handler Lifecycle

### `init(ctx)` -- Container Startup

Called once when the container starts. Creates global clients that are reused for all subsequent requests.

**S3 Client:**
```python
s3_client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    config=Config(
        max_pool_connections=25,
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=5,
        read_timeout=30,
    )
)
```

**VastDB Session:**
```python
vastdb_session = _create_vastdb_session(ctx)
ensure_database_tables(vastdb_session)  # Create proxy_outputs table if missing
```

**Tool Availability Checks:**
- Verify `oiiotool` in PATH (required for resize operations)
- Verify `ffmpeg` in PATH (required for H.264 encoding)

### `handler(ctx, event)` -- Per-Request Processing

1. **Parse event** -- Receives a `VastEvent` object. For Element triggers, calls `event.as_element_event()` to extract `bucket` and `object_key` from the `elementpath` extension.

2. **Validate extension** -- Checks file suffix against `{.exr, .dpx, .tif, .tiff}` (case-insensitive). Unsupported files return early without error.

3. **Download source file** -- Issues S3 GET request for full file (not just headers). Stores to ephemeral temp file. Captures file size and modification time.

4. **Detect color space** -- Calls `oiiotool --info` to read EXR/DPX metadata. Maps attribute values to OCIO colorspace names (ACEScg, ARRI LogC, Rec.709, scene_linear, etc.).

5. **Apply OCIO transforms** -- For thumbnail: transform source to sRGB. For proxy: transform source to Rec.709. Uses `oiiotool --colorconvert` with OCIO_CONFIG_PATH environment variable.

6. **Generate thumbnail** -- Uses `oiiotool --resize 256x256` to downscale. Outputs JPEG with quality 85. Result: small preview image (~50KB).

7. **Generate proxy** -- Uses `oiiotool --resize 1920x1080` to create PNG intermediate, then `ffmpeg` with libx264 codec to encode H.264 MP4. Result: compressed video (~5-50MB depending on bitrate).

8. **Upload outputs** -- Uses S3 client to upload thumbnail and proxy to derived S3 keys under `.proxies/` subdirectory (e.g., `renders/shot_001/.proxies/beauty.0001_thumb.jpg`).

9. **Persist metadata** -- Inserts row into VAST DataBase `proxy_outputs` table. Computes `file_id` matching exr-inspector's algorithm for future JOINs. Includes timestamps, processing duration, colorspace info, and file sizes.

10. **Publish Kafka event** -- Publishes `ProxyGeneratedEvent` to `spaceharbor.proxy` topic with asset_id, thumbnail/proxy URIs, and sizes.

11. **Cleanup** -- Deletes temporary files (downloaded source, intermediate PNG, transformed EXR files) in a `finally` block.

## Event Model

VAST DataEngine wraps events in `VastEvent` objects (not raw S3 notification dicts):

```python
# Element events (file operations)
if event.type == "Element":
    element_event = event.as_element_event()
    bucket = element_event.bucket          # From elementpath
    key = element_event.object_key         # From elementpath
    mtime = element_event.mtime            # Modification time (if available)

# Fallback for other event types
event_data = event.get_data()
bucket = event_data.get("s3_bucket")
key = event_data.get("s3_key")
```

The `elementpath` extension contains the full S3 path (e.g., `renders/shot_001/beauty.0001.exr`), which the runtime splits into bucket and key.

## Processing Pipeline

### Color Space Transforms (OCIO)

The `OcioTransform` class handles color space detection and conversion:

1. **Detection** -- Reads EXR header attributes via `oiiotool --info`:
   - Priority 1: explicit `colorspace` attribute (e.g., "ACEScg")
   - Priority 2: `chromaticities` heuristic (e.g., ACES chromaticity values)
   - Default: `scene_linear` (safe assumption for untagged EXR)

2. **Normalization** -- Maps raw attribute values to OCIO colorspace names via `_COLORSPACE_MAP`:
   - `logc`, `logc3` -> `ARRI LogC`
   - `logc4` -> `ARRI LogC4`
   - `acescg`, `aces` -> `ACEScg`
   - `srgb` -> `sRGB`
   - `rec709`, `rec.709` -> `Rec.709`

3. **Transformation** -- Uses `oiiotool --colorconvert` with OCIO config:
   ```bash
   OCIO=/usr/share/color/opencolorio/aces_1.3/config.ocio \
   oiiotool source.exr \
     --colorconvert scene_linear sRGB \
     -o source__sRGB.exr
   ```

### Thumbnail Generation (OIIO)

The `OiioProcessor.generate_thumbnail()` method:

1. Takes color-transformed EXR as input
2. Resizes to 256x256 via `oiiotool --resize`
3. Encodes as JPEG with quality 85
4. Output: ~50KB JPEG file

Command:
```bash
oiiotool transformed_srgb.exr \
  --resize 256x256 \
  --compression jpeg:85 \
  -o output_thumb.jpg
```

### Proxy Generation (OIIO + ffmpeg)

The `OiioProcessor.generate_proxy()` method uses a two-step pipeline:

1. **OIIO resize** -- Takes color-transformed EXR, resizes to 1920x1080 as PNG intermediate:
   ```bash
   oiiotool transformed_rec709.exr \
     --resize 1920x1080 \
     --compression png \
     -o intermediate.png
   ```

2. **ffmpeg encode** -- Encodes PNG to H.264 MP4 with fast preset:
   ```bash
   ffmpeg -y -i intermediate.png \
     -c:v libx264 \
     -preset fast \
     -crf 23 \
     -pix_fmt yuv420p \
     output_proxy.mp4
   ```

Output: ~5-50MB MP4 file (depending on source resolution and bitrate).

**Rationale for two-step:** ffmpeg cannot directly consume EXR files with color profiles. OIIO handles EXR parsing and geometry transforms, ffmpeg handles efficient video encoding.

## Database Integration

### file_id Computation

The `file_id` is deterministic and must match exr-inspector's algorithm:

```python
file_id = SHA256(s3_key + mtime + MD5(s3_key))[:16]
```

This enables reliable JOINs between `proxy_outputs` and exr-inspector's `files` table on shared `file_id`.

### Persistence Pattern

```python
# 1. Compute file_id
file_id = compute_file_id(s3_key, mtime)

# 2. Create PyArrow table
row = pa.table({
    "proxy_id": [uuid4()],
    "file_id": [file_id],
    "s3_key": [s3_key],
    ... (17 other columns)
})

# 3. Insert into VAST DataBase (auto-creates table on first run)
with session.transaction() as tx:
    table = tx.bucket(bucket_name).schema(schema_name).table("proxy_outputs")
    table.insert(row)
```

No upsert logic: each invocation creates a new row. Retries generate duplicate rows (acceptable for audit trail).

## Kafka Publishing

The `publisher.py` module publishes completion events:

```python
event = ProxyGeneratedEvent(
    asset_id=asset_id,
    thumbnail_uri=f"s3://{bucket}/{thumb_key}",
    proxy_uri=f"s3://{bucket}/{proxy_key}",
    thumbnail_size_bytes=thumb_size,
    proxy_size_bytes=proxy_size,
    source_size_bytes=source_size,
    timestamp=datetime.utcnow().isoformat(),
)

producer = Producer({"bootstrap.servers": broker})
producer.produce(
    topic=topic,
    key=asset_id.encode(),
    value=json.dumps(event.to_dict()).encode(),
)
```

Topic: `spaceharbor.proxy` (configurable via `KAFKA_TOPIC` env var)
Broker: `vastbroker:9092` (default, configurable via `KAFKA_BROKER`)

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Full file download (not range GET)** | Thumbnails and proxies require full image data, not just headers. Unlike exr-inspector, cannot extract meaningful output from first 256KB. |
| **OCIO for color transforms** | Ensures colorimetric accuracy for VFX workflows. OIIO alone cannot convert between arbitrary color spaces without OCIO. |
| **Two-step proxy encoding (OIIO + ffmpeg)** | OIIO handles EXR parsing and geometry; ffmpeg provides efficient, standard H.264 encoding. ffmpeg cannot directly consume EXR with color profiles. |
| **JPEG thumbnail (not PNG)** | JPEG is smaller (~50KB) and faster to generate than PNG. Sufficient quality for web preview. |
| **H.264 MP4 proxy (not ProRes or DNxHR)** | H.264 is ubiquitous, highly compressed, and universally playable. ProRes/DNxHR are larger and less suitable for remote playback. |
| **Resize target: 256x256 + 1920x1080** | Standard web thumbnail + HD proxy for editing and streaming. Covers 99% of VFX workflows. |
| **Global S3 and VastDB clients** | Matches VAST DataEngine best practice. Created once in `init()`, reused per-request. Connection pooling and retry logic configured at init time. |
| **Environment variables for credentials** | Consistent with working DataEngine functions. Credentials injected via pipeline config (encrypted at rest by VAST). |
| **Deterministic file_id** | Enables consistent cross-function queries. Same computation as exr-inspector ensures schema compatibility. |
| **Auto-provisioning proxy_outputs table** | Function is self-contained; no manual schema setup required. Get-or-create pattern safely handles concurrent first-run scenarios. |
| **LD_LIBRARY_PATH Dockerfile.fix** | CNB buildpack exec.d mechanism doesn't set library paths correctly on all platforms. Manual override ensures oiiotool and ffmpeg find their dependencies. |

## Parallel Pipeline with exr-inspector

Both functions operate on the same `ElementCreated` event stream:

```
ElementCreated event (.exr file)
  |
  +-- exr-inspector ---------> files, parts, channels, attributes
  |
  +-- oiio-proxy-generator -> proxy_outputs
  |
  (Both write to exr_metadata_2 schema, shared file_id)
```

**Benefits:**
- Single S3 trigger watches the bucket
- No re-triggering or polling
- Parallel processing: both functions run concurrently
- Consistent metadata via shared `file_id`
- Rich cross-functional queries via SQL JOINs

**Configuration:**
- Same element trigger (e.g., `media-processing`)
- Separate pipelines (one for each function)
- Both pipelines subscribe to same trigger
- DataEngine distributes events to both in parallel

## Scalability

The function is designed for high-throughput processing (hundreds of files per minute):

| Metric | Per Pod | 100 Concurrent Pods |
|--------|---------|---------------------|
| S3 download (10MB file) | ~500ms | ~1m |
| Processing (thumbnail + proxy) | ~15-30s | 25-50m |
| S3 uploads (outputs) | ~1s | ~100s |
| VastDB insert | ~100ms | ~10s |
| Kafka publish | ~50ms | ~5s |
| Total per pod | ~20-35s | 25-55m |

Key scaling factors:
- **DataEngine/Knative** autoscales pods based on event backpressure
- **VAST Event Broker** durably queues events (Kafka-compatible, no data loss)
- **S3 connection pooling** (25 concurrent connections per pod)
- **VAST DataBase** handles concurrent inserts without row-level locking
- **Kafka batching** (confluent-kafka producer flushes on completion)

Configure in pipeline deployment:
- `Concurrency (min)`: 10 pods
- `Concurrency (max)`: 200 pods
- `Method of Delivery`: unordered (critical)
- `Ephemeral Disk`: 2Gi (for intermediate files)
- `Timeout`: 300s (5 minutes)
- `Memory`: 2Gi (OIIO/ffmpeg are memory-intensive)

## Monitoring and Observability

Structured logging via `ctx.logger`:

```python
ctx.logger.info("Downloaded %s (%d bytes)", s3_key, source_size)
ctx.logger.info("Thumbnail generated: %s", thumb_path)
ctx.logger.info("Proxy generated: %s", proxy_path)
ctx.logger.info("PROXY GENERATION RESULTS:")
ctx.logger.info("  Processing time: %.2fs", elapsed)
ctx.logger.info("  Persistence: %s", persistence_result.get("status"))
```

Kafka events can be consumed for monitoring:

```bash
kafka-console-consumer --bootstrap-server vastbroker:9092 \
  --topic spaceharbor.proxy \
  --from-beginning
```

VAST DataBase queries for metrics:

```sql
SELECT
  DATE(generated_timestamp) as date,
  COUNT(*) as count,
  AVG(processing_time_seconds) as avg_time
FROM proxy_outputs
GROUP BY DATE(generated_timestamp)
ORDER BY date DESC;
```
