"""Mention writer: dedup, keyword gating, candidate-pair creation, write cap.

Rules from the design doc:
  * Only keyword-matched items are stored (the raw stream is never persisted).
  * Candidate (mention, entity) pairs are created ONLY for rows this process
    actually inserted — never for already-existing rows. This is what prevents
    the irrelevant-row re-analysis loop.
  * Ingest never updates existing rows (a re-sent body would resurrect a
    retention-purged body; metric refresh is the daily job's task).
  * The daily write cap is enforced by STOPPING further accepts; callers must
    advance their cursors only over items write_batch() reported as safe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import db
from .match import match_entities


def author_hash(platform: str, author: str | None) -> bytes | None:
    if not author:
        return None
    return hashlib.sha256(f"{platform}:{author}".encode()).digest()


@dataclass
class WriteResult:
    safe_count: int      # items (in input order) the cursor may advance over
    inserted: int
    duplicates: int
    unmatched: int
    capped: bool


class MentionWriter:
    def __init__(self, cfg, stats):
        self.cfg = cfg
        self.stats = stats
        self.cap = int(cfg.ingest.get("daily_write_cap", 5000))
        self.written_today = db.writes_today()

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.written_today)

    def write_batch(self, rows: list[dict]) -> WriteResult:
        """rows: mention dicts in ASCENDING time order, each with an extra
        '_match_text' key. Returns how far the cursor may safely advance."""
        if not rows:
            return WriteResult(0, 0, 0, 0, False)

        matched: list[tuple[int, dict, set[int]]] = []  # (idx, row, entity_ids)
        unmatched = 0
        for i, row in enumerate(rows):
            ents = match_entities(row.pop("_match_text", "") or "", self.cfg.matchers)
            if ents:
                matched.append((i, row, ents))
            else:
                unmatched += 1

        existing = self._existing_ids([r["external_id"] for _, r, _ in matched],
                                      rows[0]["platform"]) if matched else set()

        safe_count = len(rows)
        capped = False
        to_insert: list[tuple[dict, set[int]]] = []
        for i, row, ents in matched:
            if row["external_id"] in existing:
                continue
            if len(to_insert) >= self.remaining:
                # cap hit: everything from this input position on is unsafe
                safe_count = min(safe_count, i)
                capped = True
                break
            to_insert.append((row, ents))

        inserted = 0
        if to_insert:
            payload = []
            for row, _ in to_insert:
                r = dict(row)
                if isinstance(r.get("author_hash"), (bytes, bytearray)):
                    r["author_hash"] = "\\x" + r["author_hash"].hex()
                payload.append(r)
            res = (
                db.client()
                .table("mentions")
                .upsert(payload, on_conflict="platform,external_id",
                        ignore_duplicates=True)
                .execute()
            )
            returned = {r["external_id"]: r["id"] for r in (res.data or [])}
            pairs = []
            for row, ents in to_insert:
                mid = returned.get(row["external_id"])
                if mid is None:
                    continue  # lost a race to another writer; dedup did its job
                pairs.extend({"mention_id": mid, "entity_id": e} for e in ents)
            if pairs:
                db.client().table("mention_entities").upsert(
                    pairs, on_conflict="mention_id,entity_id", ignore_duplicates=True
                ).execute()
            inserted = len(returned)
            self.written_today += inserted

        self.stats.incr("scanned", len(rows))
        self.stats.incr("inserted", inserted)
        self.stats.incr("duplicates", len(existing))
        self.stats.incr("unmatched", unmatched)
        if capped:
            self.stats["write_cap_hit"] = True
        return WriteResult(safe_count, inserted, len(existing), unmatched, capped)

    @staticmethod
    def _existing_ids(external_ids: list[str], platform: str) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(external_ids), 100):
            chunk = external_ids[i : i + 100]
            res = (
                db.client()
                .table("mentions")
                .select("external_id")
                .eq("platform", platform)
                .in_("external_id", chunk)
                .execute()
            )
            out.update(r["external_id"] for r in res.data)
        return out
