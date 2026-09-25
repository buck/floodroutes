"""Connection helper: reads POSTGRES_PASSWORD from the repo's .env (never logged)."""
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def _env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def conninfo() -> str:
    env = _env()
    return psycopg.conninfo.make_conninfo(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5442"),
        dbname=os.getenv("PGDATABASE", "houston311"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", env.get("POSTGRES_PASSWORD", "")),
        application_name="floodroutes",
    )


def connect(**kw) -> psycopg.Connection:
    return psycopg.connect(conninfo(), **kw)
