import logging
import structlog
import sys

def setup_logger():
    # 1. Configure standard Python logging to output to stdout
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    # 2. Configure Structlog to wrap the standard logger
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars, # Support for Trace IDs
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


