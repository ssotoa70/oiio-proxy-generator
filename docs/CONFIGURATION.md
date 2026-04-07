# Configuration Reference

All configuration is via environment variables, set in the DataEngine pipeline or function deployment config.

## Required Environment Variables

### S3 Access (Source Bucket)

| Variable | Description | Example | Required |
|----------|-------------|---------|----------|
| `S3_ENDPOINT` | VAST S3 data VIP endpoint | `http://10.1.0.1` | Yes |
| `S3_ACCESS_KEY` | S3 access key for source bucket | `$S3_ACCESS_KEY` | Yes |
| `S3_SECRET_KEY` | S3 secret key for source bucket | `$S3_SECRET_KEY` | Yes |

These credentials are used to download media files from the S3 bucket and upload generated thumbnails and proxies. The S3 client is created once in `init()` and reused for all requests.

### VAST DataBase Persistence

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `VAST_DB_ENDPOINT` | VAST DataBase endpoint | Falls back to `S3_ENDPOINT` | No (optional) |
| `VAST_DB_ACCESS_KEY` | DataBase access key | Falls back to `S3_ACCESS_KEY` | No |
| `VAST_DB_SECRET_KEY` | DataBase secret key | Falls back to `S3_SECRET_KEY` | No |
| `VAST_DB_BUCKET` | Database-enabled bucket name | `sergio-db` | No |
| `VAST_DB_SCHEMA` | Schema name for metadata tables | `exr_metadata_2` | No |

If `VAST_DB_ENDPOINT` is not set, the function falls back to `S3_ENDPOINT`. This works when both S3 and DataBase are accessible via the same VIP.

### Kafka Event Publishing

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `KAFKA_BROKER` | Kafka broker endpoint | `vastbroker:9092` | No |
| `KAFKA_TOPIC` | Kafka topic for proxy.generated events | `spaceharbor.proxy` | No |

Events are published to the Kafka topic on successful proxy generation. Set `DEV_MODE=true` to skip publishing (useful for testing).

### OCIO Color Transforms

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `OCIO_CONFIG_PATH` | Path to OCIO config file (ACES 1.3) | `/usr/share/color/opencolorio/aces_1.3/config.ocio` | No |

The OCIO config provides color space definitions for transforms (ACEScg, ARRI LogC, Rec.709, sRGB, etc.). The default path assumes the ACES 1.3 config is installed via system packages.

### Processing Timeouts

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `OIIO_TIMEOUT` | Timeout for oiiotool and ffmpeg in seconds | `300` | No |

If any oiiotool or ffmpeg command takes longer than this, the handler returns an error. Increase for very large or high-resolution files.

### Development Mode

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DEV_MODE` | Disable S3 uploads and Kafka publishing (for testing) | `false` | No |

When `true`:
- OCIO transforms are skipped (files returned unchanged)
- S3 uploads are skipped (outputs stay in ephemeral disk)
- Kafka events are logged but not published
- VastDB persistence still runs
- Useful for local testing and debugging

## Pipeline Configuration

### Via VMS UI (Recommended)

When creating or editing a pipeline, add environment variables in the **Environment Variables** section:

```
S3_ENDPOINT              = http://$DATA_VIP
S3_ACCESS_KEY            = $S3_ACCESS_KEY
S3_SECRET_KEY            = $S3_SECRET_KEY
VAST_DB_BUCKET           = sergio-db
VAST_DB_SCHEMA           = exr_metadata_2
KAFKA_BROKER             = vastbroker:9092
KAFKA_TOPIC              = spaceharbor.proxy
OCIO_CONFIG_PATH         = /usr/share/color/opencolorio/aces_1.3/config.ocio
OIIO_TIMEOUT             = 300
```

Steps:
1. Navigate to **Manage Elements** -> **Pipelines** -> **Edit** [pipeline name]
2. Scroll to **Environment Variables**
3. Click **Add variable** for each required variable
4. Enter name and value
5. Click **Save**
6. Click **Deploy**

### Via config.yaml (Local Testing)

For local development with `vastde functions localrun`:

```yaml
pipeline:
  name: oiio-proxy-dev
  env:
    S3_ENDPOINT: "http://10.1.0.1"
    S3_ACCESS_KEY: "$S3_ACCESS_KEY"
    S3_SECRET_KEY: "$S3_SECRET_KEY"
    VAST_DB_BUCKET: "sergio-db"
    VAST_DB_SCHEMA: "exr_metadata_2"
    DEV_MODE: "true"  # Skip uploads for testing
```

Run:
```bash
vastde functions localrun oiio-proxy-generator --config config.yaml
```

## Trigger Configuration

The element trigger watches for new media files:

| Setting | Value |
|---------|-------|
| **Trigger Type** | Element |
| **Event Type** | ElementCreated (ObjectCreated:*) |
| **Source Type** | S3 |
| **Source Bucket** | Your S3 ingestion bucket |
| **Suffix Filter** | `.exr,.dpx,.tif,.tiff` |

To share a trigger with exr-inspector, use both functions' extensions:

```bash
vastde triggers create \
  --name media-processing \
  --source-bucket renders \
  --name-suffix ".exr,.dpx"
```

Both pipelines can subscribe to this shared trigger.

## Credentials Security

- Credentials are loaded **once** during `init()`, never per-request
- Secret values are **masked** in init logs (first 4 and last 4 chars only)
- Events **never** contain credentials, only file locations
- Use **separate credentials** for S3 (read+write) and DataBase (write) in production
- Store credentials in pipeline environment variables (encrypted at rest by VAST)
- For secrets management, use VAST's secret store (if available)

Example secure setup:

```bash
# Create separate S3 user for reads
aws iam create-user --user-name oiio-reader
aws iam attach-user-policy --user-name oiio-reader \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess

# Create separate DataBase user for writes
# (VAST DataBase specific -- consult VAST docs)

# Store credentials securely
vastde secrets create vast-db \
  --endpoint https://vast-db.example.com \
  --access-key $VASTDB_ACCESS_KEY \
  --secret-key $VASTDB_SECRET_KEY
```

## Color Space Support

The function detects and transforms from these color spaces:

| Detected Color Space | OCIO Name | Common Sources |
|----------------------|-----------|-----------------|
| ACES ACEScg | `ACEScg` | Nuke renders, CG software |
| ARRI LogC (v3) | `ARRI LogC` | ARRI cameras, DaVinci |
| ARRI LogC4 | `ARRI LogC4` | ARRI Alexa 35 |
| Rec.709 | `Rec.709` | Video cameras, broadcast |
| sRGB | `sRGB` | Web, consumer video |
| Scene Linear | `scene_linear` | Default for untagged EXR |

Transforms to:
- **Thumbnail:** sRGB (standard web color space)
- **Proxy:** Rec.709 (standard video color space)

If source color space is already the target, no transform is applied.

## OCIO Configuration

The function uses OCIO config files to define color space transforms. Common configs:

| Config | Path | Use Case |
|--------|------|----------|
| ACES 1.3 | `/usr/share/color/opencolorio/aces_1.3/config.ocio` | VFX, cinema (default) |
| Standard (generic) | `/usr/share/color/opencolorio/config.ocio` | General purpose |
| Custom | `/path/to/custom/config.ocio` | Studio-specific |

To use a custom OCIO config:

```bash
# Copy config to container image (Dockerfile)
COPY my_ocio_config.ocio /usr/share/color/custom.ocio

# Set environment variable in pipeline
OCIO_CONFIG_PATH=/usr/share/color/custom.ocio
```

## Performance Tuning

### Concurrency and Scaling

Configure in pipeline deployment:

```
Concurrency (min): 10         # Minimum pods to run
Concurrency (max): 200        # Maximum pods
Ephemeral Disk:   2Gi         # Temp file space
Timeout:          300s        # 5 minutes
Memory:           2Gi         # OIIO/ffmpeg intensive
CPU:              2 cores     # Parallel resize/encode
```

### Proxy Output Resolution

To generate different proxy resolutions, modify the pipeline or rebuild:

```python
# In oiio_processor.py, change dimensions:
processor.generate_proxy(transformed, proxy_path, width=3840, height=2160)  # 4K
processor.generate_proxy(transformed, proxy_path, width=1280, height=720)   # 720p
```

### Compression Quality

To trade quality for size, adjust ffmpeg CRF (0-51, lower=higher quality):

```python
# Current: CRF 23 (high quality)
# Faster encoding, larger file: CRF 28
# Slower encoding, smaller file: CRF 18

ffmpeg_cmd = [
    "ffmpeg", "-y",
    "-i", intermediate,
    "-c:v", "libx264",
    "-preset", "faster",  # fast, medium, slow
    "-crf", "28",         # Quality (default 23)
    "-pix_fmt", "yuv420p",
    output,
]
```

## Common Configurations

### Development/Testing

```
DEV_MODE=true
VAST_DB_BUCKET=test-db
VAST_DB_SCHEMA=exr_metadata_2_test
OIIO_TIMEOUT=600  # Slower machines
```

### Production

```
S3_ENDPOINT=http://10.1.0.1
S3_ACCESS_KEY=$(vault read -field=access_key secret/vast/s3)
S3_SECRET_KEY=$(vault read -field=secret_key secret/vast/s3)
VAST_DB_ENDPOINT=http://10.1.0.1
VAST_DB_ACCESS_KEY=$(vault read -field=access_key secret/vast/database)
VAST_DB_SECRET_KEY=$(vault read -field=secret_key secret/vast/database)
VAST_DB_BUCKET=production-db
VAST_DB_SCHEMA=exr_metadata_2
KAFKA_BROKER=vastbroker-prod:9092
KAFKA_TOPIC=spaceharbor.proxy
OCIO_CONFIG_PATH=/usr/share/color/opencolorio/aces_1.3/config.ocio
OIIO_TIMEOUT=300
```

### VFX Shops (with strict color management)

```
OCIO_CONFIG_PATH=/opt/studio/OCIO/my_studio_config.ocio
VAST_DB_SCHEMA=vfx_metadata
KAFKA_TOPIC=vfx.proxy_ready
OIIO_TIMEOUT=600  # Allows for complex OCIO transforms
```

## .env.example

For local development reference:

```bash
# S3 source bucket access
S3_ENDPOINT=http://10.1.0.1
S3_ACCESS_KEY=$S3_ACCESS_KEY
S3_SECRET_KEY=$S3_SECRET_KEY

# VAST DataBase persistence (optional, falls back to S3_* vars)
# VAST_DB_ENDPOINT=http://10.1.0.1
# VAST_DB_ACCESS_KEY=$VASTDB_ACCESS_KEY
# VAST_DB_SECRET_KEY=$VASTDB_SECRET_KEY
VAST_DB_BUCKET=sergio-db
VAST_DB_SCHEMA=exr_metadata_2

# Kafka event publishing
KAFKA_BROKER=vastbroker:9092
KAFKA_TOPIC=spaceharbor.proxy

# OCIO color space config
OCIO_CONFIG_PATH=/usr/share/color/opencolorio/aces_1.3/config.ocio

# Processing timeout (seconds)
OIIO_TIMEOUT=300

# Development mode (skip uploads, transforms, Kafka)
DEV_MODE=false
```

## Validation Checklist

Before deploying to production:

- [ ] `S3_ENDPOINT` is reachable from the cluster
- [ ] `S3_ACCESS_KEY` has read/write access to source and destination buckets
- [ ] `VAST_DB_BUCKET` exists and is database-enabled
- [ ] `VAST_DB_SCHEMA` is writeable by the database user
- [ ] `KAFKA_BROKER` is reachable and accepting connections
- [ ] `OCIO_CONFIG_PATH` exists in container image
- [ ] `OIIO_TIMEOUT` is sufficient for expected file sizes (test with largest file)
- [ ] Element trigger is configured with correct bucket and suffix filter
- [ ] Pipeline concurrency is tuned for cluster load
- [ ] Ephemeral disk is large enough for temp files (rule of thumb: 2x largest file)
- [ ] Memory allocation is >= 2Gi for ffmpeg encoding
- [ ] Test with sample files before enabling in production
