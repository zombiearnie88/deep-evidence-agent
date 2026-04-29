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
from watchdog.observers.api import BaseObserver
from watchdog.observers import Observer

_WatchCreateEvent = DirCreatedEvent | FileCreatedEvent
_WatchModifyEvent = DirModifiedEvent | FileModifiedEvent


def _decode_watch_path(value: str | bytes) -> Path:
    """Normalize watchdog event paths into `Path` objects."""
    if isinstance(value, bytes):
        return Path(value.decode("utf-8", errors="ignore"))
    return Path(value)


class DebouncedHandler(FileSystemEventHandler):
    """Collect create/modify/move events and flush them after debounce delay."""

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

    def pending_count(self) -> int:
        """Return the number of unique paths currently waiting for flush."""
        with self._lock:
            return len(self._pending)

    def close(self) -> None:
        """Cancel any scheduled flush when the watcher is stopping."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()

    def _handle(self, event: FileSystemEvent) -> None:
        """Handle one watchdog event and queue its file path if eligible."""
        if event.is_directory:
            return
        raw_path = event.src_path
        dest_path = getattr(event, "dest_path", None)
        if dest_path:
            raw_path = dest_path
        path = _decode_watch_path(raw_path)
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

    def on_moved(self, event: FileSystemEvent) -> None:
        """Watchdog hook for move events, using the destination path."""
        self._handle(event)


class FileWatcherHandle:
    """Lifecycle handle for one recursive watchdog observer."""

    def __init__(self, observer: BaseObserver, handler: DebouncedHandler) -> None:
        self._observer: BaseObserver = observer
        self._handler: DebouncedHandler = handler
        self._started: bool = False

    def start(self) -> None:
        """Start the underlying observer thread."""
        if self._started:
            return
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        """Stop the observer and cancel any pending debounce flush."""
        self._handler.close()
        if self._started:
            self._observer.stop()
            self._observer.join()
            self._started = False

    def join(self, timeout: float | None = None) -> None:
        """Join the observer thread when running in blocking mode."""
        if not self._started:
            return
        self._observer.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Return whether the observer thread is still alive."""
        return self._observer.is_alive()

    def pending_count(self) -> int:
        """Return debounced-but-not-yet-flushed path count."""
        return self._handler.pending_count()


def start_file_watcher(
    paths: list[Path],
    callback: Callable[[list[Path]], None],
    debounce_seconds: float = 2.0,
) -> FileWatcherHandle:
    """Create a recursive watcher for one or more directories.

    Every directory is watched recursively. The callback receives sorted unique file
    paths after the debounce window closes. Hidden files are ignored at the low-level
    event stage, and move events use the destination path so renames into a watched
    folder are treated like new files.

    Args:
        paths: Absolute directories to observe recursively.
        callback: Function called with sorted changed file paths.
        debounce_seconds: Debounce period in seconds.

    Returns:
        A lifecycle handle that supports `start()`, `stop()`, and `pending_count()`.
    """
    handler = DebouncedHandler(callback, debounce_seconds=debounce_seconds)
    observer = Observer()
    for path in paths:
        observer.schedule(handler, str(path), recursive=True)
    return FileWatcherHandle(observer, handler)


def watch_directory(
    raw_dir: Path, callback: Callable[[list[Path]], None], debounce: float = 2.0
) -> None:
    """Watch one directory recursively and invoke callback with debounced paths.

    Args:
        raw_dir: Directory to observe recursively.
        callback: Function called with sorted changed file paths.
        debounce: Debounce period in seconds.
    """
    handle = start_file_watcher([raw_dir], callback, debounce_seconds=debounce)
    handle.start()
    try:
        while handle.is_alive():
            handle.join(timeout=1.0)
    except KeyboardInterrupt:
        handle.stop()
        return
    handle.stop()
