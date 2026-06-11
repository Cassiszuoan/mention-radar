"""Load DB-driven config (entities / keywords / sources) for a cycle."""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .match import Matcher, build_matchers


@dataclass
class Config:
    entities: dict[int, dict]          # id -> row
    sources: list[dict]                # active sources
    matchers: list[Matcher]
    ingest: dict
    gemini: dict
    apify: dict
    alert_defaults: dict


def load(stats=None) -> Config:
    c = db.client()
    entities = {e["id"]: e for e in c.table("entities").select("*").eq("active", True).execute().data}
    keywords = c.table("keywords").select("*").eq("active", True).execute().data
    keywords = [k for k in keywords if k["entity_id"] in entities]
    sources = c.table("sources").select("*").eq("active", True).execute().data
    return Config(
        entities=entities,
        sources=sources,
        matchers=build_matchers(keywords, stats=stats),
        ingest=db.get_app_config("ingest"),
        gemini=db.get_app_config("gemini"),
        apify=db.get_app_config("apify"),
        alert_defaults=db.get_app_config("alert_defaults"),
    )


def save_source_config(source_id: int, cfg: dict) -> None:
    db.client().table("sources").update({"config": cfg}).eq("id", source_id).execute()


def entity_thresholds(entity_row: dict, defaults: dict) -> dict:
    """Deep-merge entity-level threshold overrides onto app defaults."""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in defaults.items()}
    for k, v in (entity_row.get("thresholds") or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged
