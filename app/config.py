from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    access_token: str = field(default_factory=lambda: os.environ.get("APP_ACCESS_TOKEN", "").strip())
    database_path: str = field(default_factory=lambda: os.environ.get("DATABASE_PATH", "data/portal.db"))
    health_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("HEALTH_CHECK_INTERVAL_SECONDS", "300")))
    # Public IPs our server uses to call exchanges; shown in the wizard for the key's IP allowlist.
    egress_ips: list[str] = field(
        default_factory=lambda: [ip.strip() for ip in os.environ.get("SERVER_EGRESS_IPS", "").split(",") if ip.strip()]
    )
    secure_cookies: bool = field(default_factory=lambda: os.environ.get("SECURE_COOKIES", "true").lower() != "false")


def explain_setting(name: str, value: str, problem: str) -> str:
    """Say what this server actually sees for a required setting, never its value."""
    if not value:
        msg = f"{name} is not set on this server."
    else:
        msg = f"{name} is set but {problem}."
    names = [k for k in os.environ if k != name]
    near = [k for k in names if k.strip().upper() == name] or difflib.get_close_matches(name, names, n=3, cutoff=0.7)
    if near:
        msg += " Similar variable names found: " + ", ".join(repr(k) for k in near) + ". The name must match exactly."
    where = [f"{label} '{os.environ[k]}'" for k, label in
             (("RAILWAY_SERVICE_NAME", "service"), ("RAILWAY_ENVIRONMENT_NAME", "environment")) if os.environ.get(k)]
    if where:
        msg += " This is Railway " + " in ".join(where) + "; add the variable there and deploy."
    return msg
