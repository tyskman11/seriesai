from __future__ import annotations

import time


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class ProgressReporter:
    """Compact, flush-on-write terminal progress with an empirical ETA."""

    def __init__(self, stage: str, total: int) -> None:
        self.stage, self.total, self.started = stage, total, time.monotonic()

    def update(self, completed: int, label: str, status: str) -> None:
        elapsed = time.monotonic() - self.started
        eta = elapsed / completed * (self.total - completed) if completed else 0
        percentage = completed / self.total * 100 if self.total else 100
        print(f"[{self.stage}] {completed}/{self.total} ({percentage:5.1f}%) | {status}: {label} | elapsed {_duration(elapsed)} | ETA {_duration(eta) if completed else 'calculating'}", flush=True)

    def starting(self, label: str) -> None:
        print(f"[{self.stage}] starting {label} ({self.total} item(s) queued)", flush=True)
