"""Build a throw-away Home Assistant config for the browser end-to-end test.

    python -m tests.e2e.setup_ha /tmp/ha-e2e

Creates: configuration.yaml (frontend + YAML dashboard with the card), a
login (tester / tester1234), onboarding marked done, the integration
installed under custom_components and a config entry for the simulated VTO.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG = """
homeassistant:
  name: E2E
  latitude: 52.23
  longitude: 21.01
  elevation: 100
  unit_system: metric
  time_zone: Europe/Warsaw
  country: PL
  language: pl
  auth_providers:
    - type: homeassistant
http:
  server_host: 127.0.0.1
  server_port: 18123
frontend:
lovelace:
  mode: yaml
logger:
  default: warning
  logs:
    custom_components.dahua_vto_talk: debug
"""

DASHBOARD = """
title: Domofon
views:
  - title: Domofon
    path: domofon
    cards:
      - type: custom:dahua-vto-talk-card
        mode: toggle
"""


def main(target: str) -> None:
    cfg = Path(target)
    if cfg.exists():
        shutil.rmtree(cfg)
    (cfg / "custom_components").mkdir(parents=True)
    shutil.copytree(ROOT / "custom_components" / "dahua_vto_talk", cfg / "custom_components" / "dahua_vto_talk")
    (cfg / "configuration.yaml").write_text(CONFIG)
    (cfg / "ui-lovelace.yaml").write_text(DASHBOARD)
    storage = cfg / ".storage"
    storage.mkdir()
    (storage / "onboarding").write_text(
        json.dumps(
            {
                "version": 4,
                "minor_version": 1,
                "key": "onboarding",
                "data": {"done": ["user", "core_config", "analytics", "integration"]},
            }
        )
    )
    now = datetime.now(UTC).isoformat()
    entry = {
        "created_at": now,
        "data": {
            "host": "127.0.0.1",
            "username": "admin",
            "password": "T3st@pass",
            "rtsp_port": 18554,
            "http_port": 18080,
            "channel": 1,
        },
        "disabled_by": None,
        "discovery_keys": {},
        "domain": "dahua_vto_talk",
        "entry_id": uuid.uuid4().hex,
        "minor_version": 1,
        "modified_at": now,
        "options": {},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "subentries": [],
        "title": "VTO2202F (127.0.0.1)",
        "unique_id": "1A2B3C4D5E6F7G8",
        "version": 1,
    }
    (storage / "core.config_entries").write_text(
        json.dumps({"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {"entries": [entry]}})
    )
    subprocess.run(
        [sys.executable, "-m", "homeassistant", "--script", "auth", "-c", str(cfg), "add", "tester", "tester1234"],
        check=True,
    )
    print("config ready in", cfg)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ha-e2e")
