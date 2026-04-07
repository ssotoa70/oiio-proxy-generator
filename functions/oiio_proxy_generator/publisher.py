"""Kafka event publishing for proxy generation completion.

Publishes ProxyGeneratedEvent to the spaceharbor.proxy topic.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("oiio-proxy-generator")

try:
    from confluent_kafka import Producer
except ImportError:
    Producer = None


@dataclass
class ProxyGeneratedEvent:
    asset_id: str
    thumbnail_uri: str
    proxy_uri: str
    thumbnail_size_bytes: int = 0
    proxy_size_bytes: int = 0
    source_size_bytes: int = 0
    project_id: str = ""
    shot_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    type: str = "proxy.generated"

    def to_dict(self) -> dict:
        d = {
            "type": self.type,
            "asset_id": self.asset_id,
            "thumbnail_uri": self.thumbnail_uri,
            "proxy_uri": self.proxy_uri,
            "thumbnail_size_bytes": self.thumbnail_size_bytes,
            "proxy_size_bytes": self.proxy_size_bytes,
            "source_size_bytes": self.source_size_bytes,
            "timestamp": self.timestamp,
        }
        if self.project_id:
            d["project_id"] = self.project_id
        if self.shot_id:
            d["shot_id"] = self.shot_id
        return d


def publish_proxy_generated(
    asset_id: str,
    thumbnail_uri: str,
    proxy_uri: str,
    thumbnail_size_bytes: int = 0,
    proxy_size_bytes: int = 0,
    source_size_bytes: int = 0,
    project_id: str = "",
    shot_id: str = "",
    broker: str = "vastbroker:9092",
    topic: str = "spaceharbor.proxy",
    dev_mode: bool = False,
) -> None:
    event = ProxyGeneratedEvent(
        asset_id=asset_id,
        thumbnail_uri=thumbnail_uri,
        proxy_uri=proxy_uri,
        thumbnail_size_bytes=thumbnail_size_bytes,
        proxy_size_bytes=proxy_size_bytes,
        source_size_bytes=source_size_bytes,
        project_id=project_id,
        shot_id=shot_id,
    )
    payload = json.dumps(event.to_dict()).encode("utf-8")

    if dev_mode or os.environ.get("DEV_MODE", "false").lower() == "true":
        log.info("[DEV] proxy.generated event (not publishing): %s", event.to_dict())
        return

    if Producer is None:
        log.warning("confluent_kafka not available, skipping Kafka publish")
        return

    producer = Producer({"bootstrap.servers": broker})
    producer.produce(
        topic=topic,
        key=asset_id.encode("utf-8"),
        value=payload,
        on_delivery=lambda err, msg: log.error("Kafka delivery error: %s", err) if err else None,
    )
    producer.flush()
    log.info("Published proxy.generated for asset %s", asset_id)
