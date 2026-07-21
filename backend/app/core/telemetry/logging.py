"""structlog configuration — JSON output, one record per log line.

Role: configure the global structlog + stdlib logging processor chain once, at
process startup.
Called by: main.py lifespan startup, before any app code logs. Calls nothing
internal.
Gotcha: uvicorn's own access/startup lines stay in uvicorn's default text
format regardless of this function — they come from loggers with
`propagate=False` in uvicorn's own dictConfig, so they never reach the root
handler this sets up. Only `structlog.get_logger()` calls (app code) are JSON.
Making uvicorn's lines JSON too needs a custom `log_config` passed to uvicorn
itself; deferred as out of scope for scaffolding.
See: docs/ARCHITECTURE.md#observability
"""

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """Configure structlog (and the stdlib root logger) to emit JSON.

    WHY the stdlib root logger is configured too, not just structlog: any
    future code that calls plain `logging.getLogger(__name__)` instead of
    structlog still ends up JSON, as long as it propagates to root.
    """
    # `logging.getLevelNamesMapping()` (3.11+) returns {"DEBUG": 10, ...} —
    # used instead of the older `getLevelName(str)` overload, which mypy
    # can't type precisely because it accepts and returns either str or int.
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        # WHY make_filtering_bound_logger: drops calls below `level` before
        # any processor runs, so a DEBUG call costs nothing in prod beyond an
        # int comparison — no string formatting happens.
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
