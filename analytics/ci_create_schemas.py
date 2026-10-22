"""Create the tables dbt reads, on an empty Postgres (CI data job, ADR-015).

Usage: uv run python analytics/ci_create_schemas.py <sqlalchemy url>

Uses the services' own schema code, so CI fails when the code and the dbt models
drift apart:
  - analytics_sink.db.setup: schemas raw and analytics, raw.events, raw.dlq,
    raw.llm_spend (without the read-only role: no password in CI);
  - agent.reviews.ReviewStore: public.review_queue and public.review_decisions.
"""

import sys

from sqlalchemy import create_engine


def create(database_url: str) -> None:
    from agent.reviews import ReviewStore
    from analytics_sink.db import setup

    setup(create_engine(database_url), bi_password=None)
    ReviewStore(database_url)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit(__doc__)
    create(args[0])
    print("created raw, analytics and the review tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
