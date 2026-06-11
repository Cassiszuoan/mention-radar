"""CLI entrypoints — one short-lived process per job step.

  python -m pipeline.run ingest            # reddit + youtube + apify fallback
  python -m pipeline.run sentiment         # pending candidate pairs → Gemini
  python -m pipeline.run aggregate_detect  # trailing recompute + alert checks
  python -m pipeline.run retention         # daily compliance/maintenance
  python -m pipeline.run report --period weekly|monthly
"""

from __future__ import annotations

import argparse

from . import aggregate, db, detect, ingest_apify, ingest_reddit, ingest_youtube
from . import report as report_mod
from . import retention as retention_mod
from . import sentiment as sentiment_mod
from .config import load


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["ingest", "sentiment", "aggregate_detect",
                                    "retention", "report"])
    ap.add_argument("--period", choices=["weekly", "monthly"], default="weekly")
    args = ap.parse_args()

    with db.sanitized_run(args.job) as stats:
        cfg = load(stats=stats)
        if args.job == "ingest":
            ingest_reddit.run(cfg, stats)
            ingest_youtube.run(cfg, stats)
            ingest_apify.run_if_needed(cfg, stats,
                                       stale=ingest_reddit.arctic_is_stale(cfg))
        elif args.job == "sentiment":
            sentiment_mod.run(cfg, stats)
        elif args.job == "aggregate_detect":
            aggregate.run(cfg, stats)
            detect.run(cfg, stats)
        elif args.job == "retention":
            retention_mod.run(cfg, stats)
        elif args.job == "report":
            report_mod.run(cfg, stats, period=args.period)


if __name__ == "__main__":
    main()
