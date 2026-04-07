# oiio-proxy-generator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-green.svg)](https://www.python.org/downloads/)
[![VAST DataEngine](https://img.shields.io/badge/VAST-DataEngine-blue.svg)](https://www.vastdata.com/)
[![OpenImageIO](https://img.shields.io/badge/OpenImageIO-oiiotool-orange.svg)](https://openimageio.readthedocs.io/)
[![OpenColorIO](https://img.shields.io/badge/OCIO-ACES_1.3-purple.svg)](https://opencolorio.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-H.264-red.svg)](https://ffmpeg.org/)
[![VAST DataBase](https://img.shields.io/badge/VAST-DataBase-green.svg)](https://www.vastdata.com/)
[![Kafka](https://img.shields.io/badge/Kafka-confluent--kafka-black.svg)](https://github.com/confluentinc/confluent-kafka-python)

**Serverless thumbnail and proxy generation for high-resolution media on VAST DataEngine.**

oiio-proxy-generator is a VAST DataEngine function that automatically generates thumbnails and H.264 proxies from EXR, DPX, and other high-resolution media files as they are ingested into a VAST S3 bucket. It applies color space transforms via OpenColorIO, persists generation metadata to VAST DataBase, and publishes completion events via Kafka for downstream consumption.

---

## How It Works

```
Media file uploaded to S3 bucket (.exr, .dpx, .tif, .tiff)
  --> VAST DataEngine ElementCreated trigger
    --> oiio-proxy-generator function container
      --> S3 download via boto3
      --> OCIO color space detection
      --> OpenImageIO resize (oiiotool)
      --> JPEG thumbnail generation (sRGB, 256x256)
      --> ffmpeg H.264 encoding (Rec.709, 1920x1080)
      --> S3 upload for outputs
      --> Persist metadata to VAST DataBase
      --> Publish Kafka event (spaceharbor.proxy topic)
      --> Return structured JSON result
```

**Performance:** Processes files concurrently with minimal ephemeral disk usage. S3 upload/download and OCIO transforms are streamed. Processing time: typically 5-30 seconds depending on file size and resolution.

## What It Generates

| Output | Format | Resolution | Color Space | Purpose |
|--------|--------|------------|-------------|---------|
| **Thumbnail** | JPEG | 256x256 | sRGB | Web preview, UI display |
| **Proxy** | H.264 MP4 | 1920x1080 | Rec.709 | VFX review, editing, streaming |

Metadata persisted to VAST DataBase:
- Detected source color space
- Thumbnail and proxy S3 locations
- File sizes (source, thumb, proxy)
- Processing duration
- Generation timestamp and version

## Project Structure

```
functions/oiio_proxy_generator/
  main.py                      # DataEngine handler (init + handler)
  vast_db_persistence.py       # VAST DataBase persistence, file_id computation
  oiio_processor.py            # OpenImageIO thumbnail/proxy generation
  ocio_transform.py            # OCIO color space detection and transforms
  publisher.py                 # Kafka event publishing
  requirements.txt             # Python dependencies
  Aptfile                      # System packages (libopenimageio-dev, ffmpeg, etc.)
Dockerfile.fix                 # LD_LIBRARY_PATH fix for CNB buildpack images
docs/
  DEPLOYMENT.md                # Build, deploy, and configure guide
  DATABASE_SCHEMA.md           # proxy_outputs table schema, queries
  ARCHITECTURE.md              # Event flow, module design, parallel pipeline
  CONFIGURATION.md             # Environment variables reference
  TROUBLESHOOTING.md           # Common issues and solutions
```

## Quick Start

```bash
# Clone
git clone https://github.com/ssotoa70/oiio-proxy-generator.git
cd oiio-proxy-generator

# Install dependencies (local development)
pip install -r functions/oiio_proxy_generator/requirements.txt

# Run tests (no VAST cluster required)
pytest functions/oiio_proxy_generator/test_vast_db_persistence.py -v

# Build container image
vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator --pull-policy never

# See docs/DEPLOYMENT.md for full deployment guide
```

## Documentation

| Document | Description |
|----------|-------------|
| [Deployment Guide](docs/DEPLOYMENT.md) | Build, push, create function, configure pipeline, manage triggers |
| [Database Schema](docs/DATABASE_SCHEMA.md) | proxy_outputs table definition, JOIN with exr-inspector, query examples |
| [Architecture](docs/ARCHITECTURE.md) | Event flow, module responsibilities, design decisions, parallel pipeline |
| [Configuration](docs/CONFIGURATION.md) | Environment variables, credentials, secrets, pipeline setup |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common issues, oiiotool/ffmpeg errors, OCIO config, database failures |

## Requirements

- **VAST Cluster** 5.4+ with DataEngine enabled
- **vastde CLI** v5.4.1+
- **Docker** with `min-api-version: "1.38"` (see [Troubleshooting](docs/TROUBLESHOOTING.md))
- **Python** 3.12 (container runtime)
- **S3 bucket** with DataEngine element trigger configured
- **Database-enabled bucket** for VAST DataBase persistence (optional, but recommended)

## Parallel Pipeline with exr-inspector

This function operates in parallel with **exr-inspector**, sharing the same VAST DataBase schema (`exr_metadata_2`). Both functions:

- Trigger on the same `ElementCreated` event (EXR files)
- Use identical `file_id` computation (SHA256-based)
- Persist to the same schema, different tables

This enables cross-functional queries via SQL JOINs:

```sql
SELECT e.file_path, e.multipart_count, p.thumbnail_s3_key, p.proxy_s3_key
FROM exr_metadata_2.files e
JOIN exr_metadata_2.proxy_outputs p ON e.file_id = p.file_id
WHERE e.is_deep = true
ORDER BY e.size_bytes DESC;
```

For shared trigger configuration, see [DEPLOYMENT.md](docs/DEPLOYMENT.md#step-5-create-the-element-trigger).

## License

[MIT](LICENSE)
