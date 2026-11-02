import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def setup(name: str) -> None:
    """Log to logs/<name>.log, plus stderr when there is a console. Under pythonw (the scheduled task runs
    windowless) sys.stdout/stderr are None. Consoles get UTF-8 (Windows defaults to cp1252 and mangles emoji)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8")
    LOG_DIR.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
