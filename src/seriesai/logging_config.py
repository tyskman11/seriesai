from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(log_directory: Path | str, console: bool = True) -> logging.Logger:
    """One shared logger for analysis and error capture.

    - console: every INFO step is printed to the terminal that runs the command, so the
      current step is always visible there.
    - logs/app.log: everything (INFO+) for later analysis.
    - logs/errors.log: ERROR+ only, including full tracebacks, for fault finding.
    Uncaught exceptions (main thread and worker threads) are routed into errors.log too.
    """
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("seriesai")
    root.setLevel(logging.INFO)
    root.propagate = False
    root.handlers.clear()
    file_formatter = logging.Formatter(_FILE_FORMAT)
    for name, level in (("app.log", logging.INFO), ("errors.log", logging.ERROR)):
        handler = logging.FileHandler(directory / name, encoding="utf-8")
        handler.setFormatter(file_formatter); handler.setLevel(level)
        root.addHandler(handler)
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        stream.setLevel(logging.INFO)
        root.addHandler(stream)
    _install_exception_hooks(root)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"seriesai.{name}")


def _install_exception_hooks(root: logging.Logger) -> None:
    def log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb); return
        root.error("Unbehandelter Fehler", exc_info=(exc_type, exc, tb))
    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: root.error(f"Unbehandelter Fehler im Thread {getattr(args.thread, 'name', '?')}", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
