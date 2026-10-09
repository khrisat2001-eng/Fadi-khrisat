from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    access_token: str = field(default_factory=lambda: os.environ.get("APP_ACCESS_TOKEN", ""))
    database_path: str = field(default_factory=lambda: os.environ.get("DATABASE_PATH", "data/portal.db"))
    health_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("HEALTH_CHECK_INTERVAL_SECONDS", "300")))
    # Public IPs our server uses to call exchanges; shown in the wizard for the key's IP allowlist.
    egress_ips: list[str] = field(
        default_factory=lambda: [ip.strip() for ip in os.environ.get("SERVER_EGRESS_IPS", "").split(",") if ip.strip()]
    )
    secure_cookies: bool = field(default_factory=lambda: os.environ.get("SECURE_COOKIES", "true").lower() != "false")
