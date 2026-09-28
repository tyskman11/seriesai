from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_directory: Path | str) -> None:
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("seriesai")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for name in ("app.log", "errors.log"):
        handler = logging.FileHandler(directory / name, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(logging.ERROR if name == "errors.log" else logging.INFO)
        root.addHandler(handler)
