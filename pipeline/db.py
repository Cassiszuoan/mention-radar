"""Supabase access + run logging.

All writes use the service_role key (RLS bypass) — this module must only ever
run server-side (GitHub Actions / operator machine), never in the dashboard.

Logging discipline (public repo, world-readable workflow logs): log counts and
ids only. NEVER log mention bodies, author handles, API payloads or keys.
The sanitized_run() wrapper enforces this for uncaught exceptions too.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from supabase import Client, create_client

_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# app_config
# ---------------------------------------------------------------------------

def get_app_config(key: str, default: dict | None = None) -> dict:
    rows = client().table("app_config").select("value").eq("key", key).execute().data
    if rows:
        return rows[0]["value"]
    if default is not None:
        return default
    raise KeyError(f"app_config key missing: {key}")


# ---------------------------------------------------------------------------
# pipeline_runs + healthcheck ping
# ---------------------------------------------------------------------------

class RunStats(dict):
    """Mutable stats bag persisted into pipeline_runs.stats."""

    def incr(self, key: str, by: int = 1) -> None:
        self[key] = int(self.get(key, 0)) + by


@contextmanager
def sanitized_run(job: str):
    """Wrap a job: creates a pipeline_runs row, guarantees that uncaught
    exceptions never print payload data (only exception class + message head),
    and pings the dead-man switch on success."""
    stats = RunStats()
    row = (
        client()
        .table("pipeline_runs")
        .insert({"job": job, "status": "running"})
        .execute()
        .data[0]
    )
    run_id = row["id"]
    t0 = time.monotonic()
    try:
        yield stats
        status = "ok"
    except Exception as exc:  # noqa: BLE001 — sanitize, then fail the job
        status = "error"
        # Class name + first 200 chars only; response bodies/rows stay out of logs.
        msg = f"{type(exc).__name__}: {str(exc)[:200]}"
        stats["error"] = msg
        print(f"[{job}] FAILED {msg}", file=sys.stderr)
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            frame = tb[-1]
            print(f"[{job}] at {frame.filename}:{frame.lineno} in {frame.name}",
                  file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        stats["duration_s"] = round(time.monotonic() - t0, 1)
        try:
            client().table("pipeline_runs").update(
                {
                    "finished_at": iso(now_utc()),
                    "status": status,
                    "stats": json.loads(json.dumps(stats, default=str)),
                }
            ).eq("id", run_id).execute()
        except Exception:
            print(f"[{job}] warn: could not persist run stats", file=sys.stderr)
        if status == "ok":
            _ping_healthcheck()
    print(f"[{job}] ok stats={ {k: v for k, v in stats.items() if k != 'error'} }")


def _ping_healthcheck() -> None:
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except requests.RequestException:
        pass  # the dead-man switch firing IS the signal; never fail the job on this


# ---------------------------------------------------------------------------
# Daily write-cap bookkeeping (cap is enforced by STOPPING fetches, never by
# dropping writes — cursors only advance past successfully written items).
# ---------------------------------------------------------------------------

def writes_today() -> int:
    midnight = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    res = (
        client()
        .table("mentions")
        .select("id", count="exact")
        .gte("fetched_at", iso(midnight))
        .limit(1)
        .execute()
    )
    return res.count or 0
