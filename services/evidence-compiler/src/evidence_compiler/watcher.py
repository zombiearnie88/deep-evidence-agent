"""File watching utilities for automatic ingestion workflows."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import (
    DirCreatedEvent,
    DirModifiedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

_WatchCreateEvent = DirCreatedEvent | FileCreatedEvent
_WatchModifyEvent = DirModifiedEvent | FileModifiedEvent


class DebouncedHandler(FileSystemEventHandler):
    """Collect create/modify events and flush them after debounce delay."""

    def __init__(
        self, callback: Callable[[list[Path]], None], debounce_seconds: float = 2.0
    ) -> None:
        super().__init__()
        self._callback: Callable[[list[Path]], None] = callback
        self._debounce_seconds: float = debounce_seconds
        self._pending: set[Path] = set()
        self._timer: threading.Timer | None = None
        self._lock: threading.Lock = threading.Lock()

    def _schedule_flush(self) -> None:
        """Reset debounce timer and schedule pending path flush."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        """Deliver pending paths to callback and clear internal buffer."""
        with self._lock:
            paths = sorted(self._pending)
            self._pending.clear()
            self._timer = None
        if paths:
            self._callback(paths)

    def _handle(self, event: FileSystemEvent) -> None:
        """Handle one watchdog event and queue its file path if eligible."""
        if event.is_directory:
            return
        src_path = event.src_path
        if isinstance(src_path, bytes):
            path = Path(src_path.decode("utf-8", errors="ignore"))
        else:
            path = Path(src_path)
        if path.name.startswith("."):
            return
        with self._lock:
            self._pending.add(path)
        self._schedule_flush()

    def on_created(self, event: _WatchCreateEvent) -> None:
        """Watchdog hook for file creation events."""
        self._handle(event)

    def on_modified(self, event: _WatchModifyEvent) -> None:
        """Watchdog hook for file modification events."""
        self._handle(event)


def watch_directory(
    raw_dir: Path, callback: Callable[[list[Path]], None], debounce: float = 2.0
) -> None:
    """Watch directory recursively and invoke callback with debounced paths.

    Args:
        raw_dir: Directory to observe.
        callback: Function called with sorted changed file paths.
        debounce: Debounce period in seconds.
    """
    handler = DebouncedHandler(callback, debounce_seconds=debounce)
    observer = Observer()
    observer.schedule(handler, str(raw_dir), recursive=True)
    observer.start()
    try:
        while observer.is_alive():
            observer.join(timeout=1.0)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
