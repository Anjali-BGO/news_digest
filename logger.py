import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

LOG_DIR  = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE       = LOG_DIR / "app.log"        # all INFO+ logs
ERROR_LOG_FILE = LOG_DIR / "errors.log"     # ERROR+ only
PIPELINE_FILE  = LOG_DIR / "pipeline.log"   # pipeline step-by-step
AUDIT_LOG_FILE = LOG_DIR / "audit.log"      # every accepted/rejected article


def _make_handler(path: Path, level: int) -> RotatingFileHandler:
    """Rotating file handler — max 5MB per file, keep 3 backups."""
    h = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    h.setLevel(level)
    h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    return h


def get_logger(name: str) -> logging.Logger:
    """
    Returns a named logger that writes to:
      - stdout (INFO+)
      - logs/app.log (INFO+, rotating)
      - logs/errors.log (ERROR+, rotating)
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger   # already configured

    logger.setLevel(logging.DEBUG)

    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(sh)

    # app.log — everything INFO+
    logger.addHandler(_make_handler(LOG_FILE, logging.INFO))

    # errors.log — ERROR+ only
    logger.addHandler(_make_handler(ERROR_LOG_FILE, logging.ERROR))

    return logger


def get_pipeline_logger() -> logging.Logger:
    """
    Dedicated logger for pipeline step-by-step progress.
    Writes to logs/pipeline.log in addition to app.log.
    """
    logger = logging.getLogger("pipeline")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | PIPELINE | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(sh)

    logger.addHandler(_make_handler(LOG_FILE,      logging.INFO))
    logger.addHandler(_make_handler(ERROR_LOG_FILE, logging.ERROR))
    logger.addHandler(_make_handler(PIPELINE_FILE,  logging.DEBUG))

    return logger


def get_audit_logger() -> logging.Logger:
    """
    Dedicated logger for every accepted/rejected article.
    Writes to logs/audit.log (plain INFO lines, no rotation overlap with app.log).
    """
    logger = logging.getLogger("audit")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False   # don't double-write to root / app.log

    logger.addHandler(_make_handler(AUDIT_LOG_FILE, logging.DEBUG))

    return logger