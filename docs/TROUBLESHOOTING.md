# Troubleshooting Guide

This document provides solutions for common issues encountered when using oiio-proxy-generator with VAST DataEngine.

---

## Build and Deployment Issues

### Error: "oiiotool not found" or "ffmpeg not found"

**Symptom:** Function builds successfully but fails at runtime with "command not found".

**Root Cause:** System packages not installed via Aptfile or not available in build environment.

**Solutions:**

1. **Verify Aptfile contents:**
   ```bash
   cat functions/oiio_proxy_generator/Aptfile
   ```
   Should contain:
   ```
   libopenimageio-dev
   libopenexr-dev
   ffmpeg
   ```

2. **Check CNB buildpack log:**
   ```bash
   vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator --pull-policy never 2>&1 | grep -A5 "apt-buildpack"
   ```
   Look for successful installation of packages.

3. **Verify tools in running container:**
   ```bash
   # SSH into a running pod (if available)
   which oiiotool
   which ffmpeg
   
   # Check library paths
   ldd /usr/bin/oiiotool
   ```

4. **Rebuild with fresh cache:**
   ```bash
   docker system prune -a  # Remove dangling images
   vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator --pull-policy always --no-cache
   ```

---

### Error: "Docker API version error" or "min-api-version"

**Symptom:** `vastde` CLI fails with Docker API version mismatch.

**Root Cause:** Docker Desktop uses API 1.40+, but `vastde` CLI requires 1.38.

**Solutions:**

1. **Check current Docker API version:**
   ```bash
   docker version | grep "API version"
   ```

2. **Configure Docker daemon:**
   - **Docker Desktop:** Settings -> Docker Engine
   - Edit JSON config and add:
     ```json
     {
       "min-api-version": "1.38"
     }
     ```
   - Click Apply & Restart

3. **Verify fix:**
   ```bash
   vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator
   ```

---

### Error: "Container registry not accessible" or "image push failed"

**Symptom:** Docker push succeeds but VAST cannot pull the image.

**Root Cause:** Registry hostname not resolvable from VAST cluster, or authentication mismatch.

**Solutions:**

1. **Test registry accessibility from local machine:**
   ```bash
   docker login $REGISTRY_HOST
   docker push $REGISTRY_HOST/oiio-proxy-generator:latest
   ```

2. **Test from VAST cluster (if possible):**
   ```bash
   # SSH to a VAST node
   curl -I https://$REGISTRY_HOST/v2/
   ```

3. **Verify registry credentials in VAST:**
   - Go to **Manage Elements** -> **Container Registries**
   - Check hostname, username, and password
   - Test connection

4. **Check image tag:**
   ```bash
   docker images | grep oiio-proxy
   # Should show: $REGISTRY_HOST/oiio-proxy-generator:latest
   ```

---

## Function Execution Issues

### Error: "oiiotool: command timed out after 300s"

**Symptom:** Large files or complex transforms exceed the timeout.

**Root Cause:** `OIIO_TIMEOUT` is too short for the file size/resolution.

**Solutions:**

1. **Increase timeout in pipeline config:**
   ```
   OIIO_TIMEOUT = 600  # 10 minutes for very large files
   ```

2. **Profile the slow operation:**
   ```bash
   # Test locally
   time oiiotool large_file.exr --resize 1920x1080 -o test.png
   ```
   If it takes >300s, you need a longer timeout or smaller target resolution.

3. **Reduce proxy resolution:**
   - Edit `oiio_processor.py`
   - Change: `width=1920, height=1080` to `width=1280, height=720` (or smaller)
   - Rebuild and redeploy

4. **Check pod resource allocation:**
   - Ensure pipeline has adequate memory (2Gi+) and CPU (2 cores+)
   - Memory pressure can slow ffmpeg encoding

---

### Error: "ffmpeg not found in PATH"

**Symptom:** Proxy generation fails with "ffmpeg not found".

**Root Cause:** ffmpeg installed but not in PATH, or build didn't include it.

**Solutions:**

1. **Check Aptfile:**
   ```bash
   grep ffmpeg functions/oiio_proxy_generator/Aptfile
   ```
   If missing, add it:
   ```
   ffmpeg
   libopenimageio-dev
   libopenexr-dev
   ```

2. **Verify ffmpeg installation:**
   ```bash
   # In container shell
   dpkg -l | grep ffmpeg
   which ffmpeg
   ```

3. **Rebuild:**
   ```bash
   vastde functions build oiio-proxy-generator --target functions/oiio_proxy_generator --pull-policy never
   docker build -t $REGISTRY_HOST/oiio-proxy-generator:latest -f Dockerfile.fix .
   docker push $REGISTRY_HOST/oiio-proxy-generator:latest
   ```

---

### Error: "OCIO config not found" or "OCIO: Config file error"

**Symptom:** Color space transforms fail with config file errors.

**Root Cause:** OCIO config path incorrect or file doesn't exist in container.

**Solutions:**

1. **Verify config path in container:**
   ```bash
   # SSH to running pod (if available)
   ls -la /usr/share/color/opencolorio/aces_1.3/
   ```

2. **Check environment variable:**
   ```bash
   # From pipeline config, verify OCIO_CONFIG_PATH is set correctly:
   OCIO_CONFIG_PATH=/usr/share/color/opencolorio/aces_1.3/config.ocio
   ```

3. **Install OCIO config via Aptfile:**
   If ACES config isn't available, add package:
   ```
   opencolorio
   ```

4. **Use custom config:**
   - Copy config to container during build (Dockerfile)
   - Set `OCIO_CONFIG_PATH` to custom path

5. **Fall back to bypass in dev mode:**
   ```
   DEV_MODE=true
   ```
   This skips OCIO transforms entirely.

---

### Error: "Source color space could not be detected"

**Symptom:** Handler logs "detected_colorspace: unknown", thumbnail/proxy generated with incorrect color.

**Root Cause:** EXR metadata missing or uses non-standard attribute names.

**Solutions:**

1. **Inspect EXR metadata locally:**
   ```bash
   oiiotool source.exr --info -v | grep -E "colorspace|chromaticities"
   ```

2. **Check what oiio-proxy detects:**
   - Look at handler logs: `ctx.logger.info("Color space: %s", detected_colorspace)`
   - Should show detected color space or "scene_linear" (default)

3. **Add custom colorspace detection:**
   - Edit `ocio_transform.py` `detect_colorspace()` method
   - Add heuristic for your EXR convention

4. **Override via environment:**
   - Currently not supported; would require code change
   - Open feature request if needed

---

## S3 Access Issues

### Error: "Failed to download s3://bucket/key: Access Denied"

**Symptom:** Handler cannot read source file from S3.

**Root Cause:** S3 credentials lack read permission on source bucket.

**Solutions:**

1. **Verify S3 credentials:**
   ```bash
   aws s3 ls s3://$SOURCE_BUCKET \
     --endpoint-url http://$S3_ENDPOINT \
     --access-key $S3_ACCESS_KEY \
     --secret-key $S3_SECRET_KEY
   ```

2. **Check bucket policy:**
   ```bash
   aws s3api get-bucket-policy --bucket $SOURCE_BUCKET --endpoint-url http://$S3_ENDPOINT
   ```
   User/role should have `s3:GetObject` permission.

3. **Verify credentials in pipeline:**
   - Go to pipeline config
   - Check `S3_ACCESS_KEY` and `S3_SECRET_KEY` are set correctly
   - Test manually:
     ```bash
     aws s3 cp s3://$SOURCE_BUCKET/test.exr . --endpoint-url http://$S3_ENDPOINT
     ```

4. **Check endpoint URL format:**
   - Should be `http://10.1.0.1` or `https://` with valid certificate
   - NOT `http://10.1.0.1:80` (port shouldn't be needed)

---

### Error: "Failed to upload to S3: Access Denied"

**Symptom:** Handler cannot write proxy outputs to S3.

**Root Cause:** S3 credentials lack write permission on destination bucket.

**Solutions:**

1. **Verify upload credentials have write permissions:**
   ```bash
   aws s3 cp test.jpg s3://$SOURCE_BUCKET/.proxies/ \
     --endpoint-url http://$S3_ENDPOINT
   ```

2. **Check bucket policy allows PutObject:**
   ```bash
   aws s3api get-bucket-policy --bucket $SOURCE_BUCKET
   ```
   User/role should have `s3:PutObject`.

3. **Verify `.proxies/` subdirectory exists:**
   - S3 doesn't require pre-creating directories, but check:
     ```bash
     aws s3 ls s3://$SOURCE_BUCKET/.proxies/ --endpoint-url http://$S3_ENDPOINT
     ```

4. **Test upload locally:**
   ```bash
   dd if=/dev/zero of=test.jpg bs=1M count=1
   aws s3 cp test.jpg s3://$SOURCE_BUCKET/.proxies/ --endpoint-url http://$S3_ENDPOINT
   ```

---

### Error: "Connection timeout" when downloading

**Symptom:** S3 download hangs for 30+ seconds then fails.

**Root Cause:** S3 endpoint unreachable or overloaded.

**Solutions:**

1. **Check endpoint reachability:**
   ```bash
   ping 10.1.0.1
   curl -I http://10.1.0.1:7180/health  # VAST VIP endpoint
   ```

2. **Check network connectivity from cluster:**
   ```bash
   # If you can SSH to a pod:
   nc -zv 10.1.0.1 80
   ```

3. **Increase connection timeout in pipeline:**
   Currently hardcoded to 5s in boto3 config. To increase:
   - Edit `main.py` init():
     ```python
     config=Config(
         connect_timeout=10,  # Increase from 5
         read_timeout=60,     # Increase from 30
     )
     ```
   - Rebuild and redeploy

4. **Check if S3 service is overloaded:**
   ```bash
   vast logs --component s3 --tail 100
   ```

---

## VastDB Persistence Issues

### Error: "VastDB endpoint not configured"

**Symptom:** Database persistence skipped, no data inserted.

**Root Cause:** VAST_DB_ENDPOINT or fallback not set.

**Solutions:**

1. **Check environment variables:**
   ```bash
   echo $VAST_DB_ENDPOINT
   echo $S3_ENDPOINT  # Fallback
   ```

2. **Set in pipeline config:**
   ```
   VAST_DB_ENDPOINT = http://10.1.0.1
   S3_ENDPOINT = http://10.1.0.1  # Fallback
   ```

3. **Check credentials priority:**
   1. `ctx.secrets` (if running on DataEngine)
   2. Environment variables (`VAST_DB_*`)
   3. Fallback to `S3_*` vars
   4. Default (empty, skips persistence)

---

### Error: "Failed to create VAST DataBase session"

**Symptom:** Persistence fails with detailed error message.

**Root Cause:** Invalid credentials or unreachable endpoint.

**Solutions:**

1. **Verify endpoint is accessible:**
   ```bash
   curl -I https://your-vast-endpoint.example.com
   # Should return 200 or 401, not 404 or timeout
   ```

2. **Verify credentials are valid:**
   ```bash
   # Use VAST CLI to test credentials
   vast auth verify \
       --endpoint http://10.1.0.1 \
       --access-key $ACCESS_KEY \
       --secret-key $SECRET_KEY
   ```

3. **Check credential format:**
   ```python
   # Credentials should be plain strings, not quoted
   # In pipeline config:
   S3_ACCESS_KEY=abc123def456  # Correct
   S3_ACCESS_KEY='abc123def456'  # Wrong (includes quotes)
   ```

4. **Enable debug logging:**
   ```python
   # In vast_db_persistence.py, enable:
   logging.basicConfig(level=logging.DEBUG)
   ```

---

### Error: "proxy_outputs table not found"

**Symptom:** Insert fails with "table does not exist".

**Root Cause:** Table auto-creation failed or wrong schema.

**Solutions:**

1. **Check table creation:**
   - First invocation should auto-create table
   - Check logs for: "Creating table: proxy_outputs"

2. **Verify schema exists:**
   ```python
   from vastdb import connect
   session = connect(...)
   with session.transaction() as tx:
       bucket = tx.bucket("sergio-db")
       try:
           schema = bucket.schema("exr_metadata_2")
           print("Schema exists")
       except:
           print("Schema not found")
   ```

3. **Check permissions:**
   - Database user must have CREATE TABLE permission
   - Run schema creation in dedicated transaction

4. **Check bucket type:**
   ```bash
   vast bucket info sergio-db
   # Should show: protocols = S3, DATABASE
   ```
   If DATABASE protocol is missing, bucket can't hold tables.

---

### Error: "Rollback failed" (during persistence)

**Symptom:** Transaction error and rollback error both logged.

**Root Cause:** Database connection lost or session broken.

**Solutions:**

1. **Reconnect and retry:**
   - Handler already retries per-event
   - Check logs for: "Persisted proxy_outputs: file_id=..."

2. **Check VAST Database health:**
   ```bash
   vast db health
   vast logs --component database --tail 100
   ```

3. **Check session reuse:**
   - `vastdb_session` is created in `init()` and reused
   - If connection dies, next handler invocation creates new session

---

## Kafka Publishing Issues

### Error: "Kafka broker not reachable"

**Symptom:** Handler logs warning, events not published.

**Root Cause:** Broker endpoint incorrect or not accessible.

**Solutions:**

1. **Verify broker is running:**
   ```bash
   # Check if vastbroker pod is running
   kubectl get pods -n vast | grep broker
   ```

2. **Check broker address:**
   ```bash
   # Test connectivity
   nc -zv vastbroker 9092
   ```

3. **Update broker endpoint in pipeline:**
   ```
   KAFKA_BROKER = vastbroker:9092  # Default
   # or for external broker:
   KAFKA_BROKER = kafka.example.com:9092
   ```

4. **Skip Kafka in dev mode:**
   ```
   DEV_MODE = true
   ```
   Events are logged but not published.

---

### Error: "Kafka delivery error" in logs

**Symptom:** Event published but logs show delivery error.

**Root Cause:** Topic doesn't exist or broker rejecting message.

**Solutions:**

1. **Check topic exists:**
   ```bash
   kafka-topics --bootstrap-server vastbroker:9092 --list | grep spaceharbor.proxy
   ```

2. **Create topic if missing:**
   ```bash
   kafka-topics --bootstrap-server vastbroker:9092 \
     --create --topic spaceharbor.proxy \
     --partitions 3 --replication-factor 1
   ```

3. **Verify topic config:**
   ```bash
   kafka-topics --bootstrap-server vastbroker:9092 \
     --describe --topic spaceharbor.proxy
   ```

4. **Monitor producer:**
   ```bash
   kafka-console-consumer --bootstrap-server vastbroker:9092 \
     --topic spaceharbor.proxy --from-beginning
   ```

---

## Color Space Transform Issues

### Error: "oiiotool colorconvert failed"

**Symptom:** OCIO transform fails with subprocess error.

**Root Cause:** Invalid color space names or missing OCIO config.

**Solutions:**

1. **List available color spaces:**
   ```bash
   # In container
   export OCIO=/usr/share/color/opencolorio/aces_1.3/config.ocio
   oiiotool --info source.exr | grep -i color
   ```

2. **Verify source and target color spaces:**
   - Source should match OCIO config (e.g., "ACEScg", "scene_linear")
   - Target should be "sRGB" or "Rec.709"
   - Check `_COLORSPACE_MAP` in `ocio_transform.py`

3. **Test transform locally:**
   ```bash
   export OCIO=/usr/share/color/opencolorio/aces_1.3/config.ocio
   oiiotool source.exr --colorconvert ACEScg sRGB -o test_srgb.exr
   ```

4. **Use safe defaults:**
   - If detection fails, code defaults to "scene_linear"
   - Transforms from scene_linear to sRGB/Rec.709 should always work

---

### Error: "Source color space unknown" produces poor quality output

**Symptom:** Thumbnails look dark or washed out because color space wasn't detected.

**Root Cause:** EXR missing colorspace metadata, default to scene_linear may be wrong.

**Solutions:**

1. **Check source metadata:**
   ```bash
   oiiotool source.exr --info -v | grep -A50 "Attributes"
   ```

2. **If colorspace attribute missing:**
   - Ask rendering team to add: `--colorspace "ARRI LogC"`
   - Or check render software default

3. **Test with known color space:**
   ```bash
   oiiotool source.exr --colorconvert "YOUR_COLORSPACE" sRGB -o test.jpg
   ```

4. **Improve heuristics in code:**
   - Edit `ocio_transform.py` `detect_colorspace()`
   - Add more chromaticity checks or software-specific logic

---

## Performance Issues

### Problem: "Processing is very slow (>60 seconds per file)"

**Symptom:** Handler logs show processing_time_seconds > 60.

**Root Cause:** Large file, high resolution, slow storage, or CPU bottleneck.

**Solutions:**

1. **Profile the pipeline:**
   ```bash
   # Check logs for each step:
   Downloaded X bytes
   OCIO transform: ... (check duration)
   Thumbnail generated
   Proxy generated
   Uploaded to S3
   ```

2. **Reduce proxy resolution:**
   - Edit `oiio_processor.py`
   - Change: `width=1920, height=1080` to smaller resolution
   - This affects ffmpeg encoding time most

3. **Increase concurrency:**
   - Set `Concurrency (max)` higher in pipeline config
   - This distributes load across more pods

4. **Check pod resource allocation:**
   - Ensure pipeline has 2Gi+ memory and 2+ CPU cores
   - Memory pressure causes massive slowdown in ffmpeg

5. **Check S3 performance:**
   ```bash
   # Test S3 bandwidth
   dd if=/dev/zero of=10mb bs=1M count=10
   time aws s3 cp 10mb s3://$BUCKET/ --endpoint-url http://$S3_ENDPOINT
   ```
   If S3 download is >10s, that's the bottleneck.

---

### Problem: "Memory usage is high" or "Pod OOMKilled"

**Symptom:** Pod crashes with exit code 137 or "Killed".

**Root Cause:** ffmpeg encoding uses significant memory for large files.

**Solutions:**

1. **Increase memory allocation:**
   - In pipeline config: `Memory = 4Gi` (up from 2Gi)
   - Retest with large file

2. **Reduce proxy bitrate:**
   - Edit `oiio_processor.py`
   - Change ffmpeg CRF from 23 to 28 (lower quality, smaller output)
   - This may slightly reduce memory usage

3. **Monitor actual usage:**
   ```bash
   # In running pod
   free -h
   watch ps aux  # Check ffmpeg memory
   ```

---

## Log Analysis

### Collecting Logs for Debugging

```bash
# Tail recent logs
vastde logs tail oiio-proxy-pipeline -f

# Get logs for specific time range
vastde logs get oiio-proxy-pipeline --since 30m

# Get logs for specific pod/invocation
vastde logs get oiio-proxy-pipeline --selector pod=oiio-proxy-generator-xyz

# Export to file
vastde logs get oiio-proxy-pipeline --tail 1000 > debug.log
```

### Key Log Markers

Look for these in logs:

```
INITIALIZING OIIO-PROXY-GENERATOR  # Container started
Processing new proxy request       # Handler invoked
Element event - Trigger            # Event received
Downloaded X bytes                 # S3 download complete
Thumbnail generated               # oiiotool succeeded
Proxy generated                   # ffmpeg succeeded
Uploaded to s3://                 # S3 upload complete
Persisted proxy_outputs           # VastDB insert succeeded
Published proxy.generated          # Kafka event sent
PROXY GENERATION RESULTS           # Summary before return
```

If any step is missing, that's where the failure occurred.

---

## Testing and Validation

### Local Testing (no VAST cluster)

```bash
# Install dependencies
pip install -r functions/oiio_proxy_generator/requirements.txt

# Run unit tests
pytest functions/oiio_proxy_generator/test_vast_db_persistence.py -v

# Run integration tests
pytest functions/oiio_proxy_generator/ -v -k test_oiio
```

### Manual Function Test

```bash
# Create test event
python3 << 'EOF'
import json

event = {
    "type": "Element",
    "id": "test-123",
    "trigger": "media-trigger",
    "bucket": "test-bucket",
    "object_key": "test.exr",
}

# Mock context
class MockContext:
    class Logger:
        def info(self, msg, *args): print(msg % args)
        def warning(self, msg, *args): print(msg % args)
        def error(self, msg, *args): print(msg % args)
    
    logger = Logger()
    secrets = {}

# Call handler
from main import handler
result = handler(MockContext(), event)
print(json.dumps(result, indent=2))
EOF
```

---

## Support and Escalation

If issues persist:

1. **Collect diagnostic info:**
   ```bash
   # Logs
   vastde logs get oiio-proxy-pipeline --tail 500 > logs.txt
   
   # Config
   vastde pipelines get oiio-proxy-pipeline -o json > pipeline.json
   
   # Status
   vastde functions get oiio-proxy-generator -o json > function.json
   ```

2. **File issue with:**
   - Full error message and traceback
   - Reproduction steps (file size, format, color space)
   - Log output (sanitized, remove paths)
   - Configuration (sanitized, remove credentials)
   - VAST cluster version
   - Python and dependency versions

3. **Contact support:**
   - VAST support portal
   - Include diagnostic bundle (above)
   - Reference this troubleshooting guide
