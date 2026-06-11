"""Idempotent aggregation: full recompute of trailing windows via SQL function.

Late data has three paths in (source indexing lag, Gemini backlog catch-up,
metric refresh); recomputing trailing 72h hourly / 7d daily self-heals all of
them. At this scale the recompute is a few thousand rows — sub-second.
"""

from __future__ import annotations

from . import db
from .config import Config


def run(cfg: Config, stats) -> None:
    db.client().rpc("fn_recompute_agg", {"p_hours": 72, "p_days": 7}).execute()
    stats["recomputed"] = "hourly72h+daily7d"
