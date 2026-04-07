# Deployment Guide

This guide covers building, deploying, and configuring oiio-proxy-generator on VAST DataEngine.

## Prerequisites

- **vastde CLI** v5.4.1+ installed and configured
- **Docker** running with `"min-api-version": "1.38"` in daemon config
- **VAST Cluster** 5.4+ with DataEngine enabled
- **Container registry** connected to your DataEngine tenant
- **S3 bucket** (source view) for media file ingestion
- **Database-enabled bucket** for VAST DataBase persistence

## Step 1: Configure vastde CLI

```bash
vastde config init \
  --vms-url $VMS_URL \
  --tenant $TENANT_NAME \
  --username $USERNAME \
  --password $PASSWORD \
  --builder-image-url $BUILDER_IMAGE_URL
```

Verify configuration:

```bash
vastde config view
vastde functions list
vastde buckets list
```

## Step 2: Build the Function Image

```bash
# From the repository root
vastde functions build oiio-proxy-generator \
  --target functions/oiio_proxy_generator \
  --pull-policy never
```

The build uses Cloud Native Buildpacks (CNB) to create a container image with:
- Python 3.12 runtime
- OpenImageIO and OpenEXR system libraries (via Aptfile)
- ffmpeg for H.264 encoding
- boto3, pyarrow, vastdb, confluent-kafka Python dependencies
- VAST runtime SDK

### Apply LD_LIBRARY_PATH Fix

CNB buildpack images require an additional layer to set library paths correctly:

```bash
docker build --platform linux/amd64 --no-cache \
  -t $REGISTRY_HOST/oiio-proxy-generator:latest \
  -f Dockerfile.fix .
```

## Step 3: Push to Container Registry

```bash
docker push $REGISTRY_HOST/oiio-proxy-generator:latest
```

## Step 4: Create the Function

### Via vastde CLI

```bash
vastde functions create \
  --name oiio-proxy-generator \
  --description "Thumbnail and proxy generation with color space transforms" \
  --container-registry $REGISTRY_NAME \
  --artifact-source oiio-proxy-generator \
  --image-tag latest
```

### Via VMS UI

1. Navigate to **Manage Elements** -> **Functions** -> **Create New Function**
2. Fill in:
   - **Name:** `oiio-proxy-generator`
   - **Container Registry:** Select your registry
   - **Artifact Source:** `oiio-proxy-generator`
   - **Image Tag:** `latest`
3. Ensure **"Make local revision"** is **unchecked**
4. Click **Create**

## Step 5: Create the Element Trigger

The trigger watches for new EXR, DPX, TIF, and TIFF files:

```bash
vastde triggers create \
  --type Element \
  --name media-trigger \
  --description "Watch for new media files" \
  --source-bucket $SOURCE_BUCKET \
  --events "ObjectCreated:*" \
  --name-suffix ".exr,.dpx,.tif,.tiff"
```

### Sharing a Trigger with exr-inspector

If you want both `exr-inspector` and `oiio-proxy-generator` to trigger on the same ElementCreated event, use the same trigger with a **wildcard suffix** that includes both functions' supported extensions:

```bash
vastde triggers create \
  --type Element \
  --name media-processing \
  --description "Trigger both metadata extraction and proxy generation" \
  --source-bucket $SOURCE_BUCKET \
  --events "ObjectCreated:*" \
  --name-suffix ".exr,.dpx"
```

Then configure separate pipelines (Step 6) that each function subscribes to this shared trigger.

**Benefit:** Single trigger watches the bucket, both functions process files in parallel without re-triggering.

## Step 6: Create and Deploy the Pipeline

### Via VMS UI (Recommended)

1. **Manage Elements** -> **Pipelines** -> **Create New Pipeline**
2. **Name:** `oiio-proxy-pipeline`
3. **Add environment variables** (see [Configuration](CONFIGURATION.md)):

   | Variable | Value |
   |----------|-------|
   | `S3_ENDPOINT` | `http://$DATA_VIP` |
   | `S3_ACCESS_KEY` | `$S3_ACCESS_KEY` |
   | `S3_SECRET_KEY` | `$S3_SECRET_KEY` |
   | `VAST_DB_BUCKET` | `$DATABASE_BUCKET` |
   | `VAST_DB_SCHEMA` | `exr_metadata_2` |
   | `KAFKA_BROKER` | `vastbroker:9092` |
   | `KAFKA_TOPIC` | `spaceharbor.proxy` |
   | `OCIO_CONFIG_PATH` | `/usr/share/color/opencolorio/aces_1.3/config.ocio` |
   | `OIIO_TIMEOUT` | `300` |

4. **Add function deployment:** Select `oiio-proxy-generator`
5. **Link trigger:** Connect `media-trigger` -> `oiio-proxy-generator`
6. Click **Create Pipeline**
7. **Deploy** the pipeline

### Concurrency and Performance

Configure in the pipeline deployment settings:
- **Concurrency (min):** 10
- **Concurrency (max):** 200
- **Method of Delivery:** unordered (critical for parallel processing)
- **Ephemeral Disk:** 2Gi (allows for intermediate files during transform)
- **Timeout:** 300s (5 minutes)
- **Memory:** 2Gi recommended (OIIO/ffmpeg are memory-intensive)

## Step 7: Verify Deployment

```bash
# Check pipeline status
vastde pipelines list

# Tail logs
vastde logs tail oiio-proxy-pipeline

# Check function status
vastde functions get oiio-proxy-generator -o json
```

## Step 8: Test

Upload a test media file to the source bucket:

```bash
aws s3 cp sample.exr s3://$SOURCE_BUCKET/ \
  --endpoint-url http://$DATA_VIP
```

Monitor the logs:

```bash
vastde logs get oiio-proxy-pipeline --since 5m
```

Verify outputs in S3:

```bash
aws s3 ls s3://$SOURCE_BUCKET/.proxies/ --endpoint-url http://$DATA_VIP
```

Query VAST DataBase:

```bash
# Connect to VAST DataBase (requires vastdb CLI)
# List recent proxy generation records
SELECT s3_key, thumbnail_s3_key, proxy_s3_key, processing_time_seconds
FROM exr_metadata_2.proxy_outputs
ORDER BY generated_timestamp DESC
LIMIT 10;
```

## Step 9: Verify Database Integration

Check that proxy generation metadata was persisted:

```bash
# Test via VastDB Python SDK
python3 << 'EOF'
from vastdb import connect

session = connect(
    endpoint="http://$DATA_VIP",
    access="$S3_ACCESS_KEY",
    secret="$S3_SECRET_KEY"
)

with session.transaction() as tx:
    bucket = tx.bucket("$DATABASE_BUCKET")
    schema = bucket.schema("exr_metadata_2")
    table = schema.table("proxy_outputs")
    
    # Count recent records
    count = table.select("SELECT COUNT(*) FROM proxy_outputs").scalar()
    print(f"Proxy records: {count}")
    
    # Show latest
    latest = table.select(
        "SELECT s3_key, processing_time_seconds FROM proxy_outputs "
        "ORDER BY generated_timestamp DESC LIMIT 5"
    )
    for row in latest:
        print(f"  {row['s3_key']}: {row['processing_time_seconds']:.2f}s")
EOF
```

## Updating the Function

After code changes:

```bash
# 1. Rebuild
vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator --pull-policy never

# 2. Apply LD_LIBRARY_PATH fix
docker build --platform linux/amd64 --no-cache \
  -t $REGISTRY_HOST/oiio-proxy-generator:latest -f Dockerfile.fix .

# 3. Push
docker push $REGISTRY_HOST/oiio-proxy-generator:latest

# 4. Update function revision (via CLI or VMS UI)
vastde functions update oiio-proxy-generator --image-tag latest

# 5. Redeploy pipeline
vastde pipelines deploy oiio-proxy-pipeline
```

## Docker Configuration Note

The `vastde` CLI embeds a Docker client that requires API version 1.38. Modern Docker Desktop (v4.40+) defaults to a minimum of 1.40. Add this to your Docker Engine configuration:

```json
{
  "min-api-version": "1.38"
}
```

In Docker Desktop: **Settings** -> **Docker Engine** -> edit JSON -> **Apply & restart**.

## Troubleshooting Deployment

### Build fails with "oiiotool not found"

The CNB build process fetches system packages from Aptfile. Ensure `libopenimageio-dev` is specified:

```bash
# Check functions/oiio_proxy_generator/Aptfile
cat functions/oiio_proxy_generator/Aptfile
```

If missing, add it and rebuild.

### Function creates but pipeline fails to start

Check environment variables in the pipeline config. Missing `S3_ENDPOINT` or `VAST_DB_BUCKET` will cause failures:

```bash
vastde pipelines get oiio-proxy-pipeline -o json | jq '.spec.env'
```

### Container registry not accessible

Verify registry hostname and credentials:

```bash
docker login $REGISTRY_HOST
docker push $REGISTRY_HOST/oiio-proxy-generator:latest
```

If push succeeds but VAST cannot pull, check VMS network connectivity to registry.

## Performance Baseline

Expected metrics on var201.selab.vastdata.com:

| Metric | Value |
|--------|-------|
| Build time | 5-10 minutes |
| First invocation (cold start) | 3-5 seconds |
| Thumbnail generation (256x256) | 1-2 seconds |
| Proxy generation (1920x1080 H.264) | 10-30 seconds |
| S3 download (10MB file) | <500ms |
| S3 upload (outputs) | <1s |
| VAST DataBase insert | <100ms |
| Kafka publish | <50ms |

End-to-end per file: 15-60 seconds depending on source resolution and bitrate.

## Next Steps

1. Configure [CONFIGURATION.md](CONFIGURATION.md) for your environment
2. Monitor logs and tune [concurrency settings](#step-6-create-and-deploy-the-pipeline)
3. Review [ARCHITECTURE.md](ARCHITECTURE.md) for design details
4. Consult [TROUBLESHOOTING.md](TROUBLESHOOTING.md) if issues arise
