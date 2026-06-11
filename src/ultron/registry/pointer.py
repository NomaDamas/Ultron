"""Active module pointer store with atomic compare-and-swap."""

from __future__ import annotations

from threading import Lock


class ActivePointerStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pointers: dict[tuple[str, str], tuple[int, tuple[str, ...]]] = {}

    def get(self, key: tuple[str, str]) -> tuple[int, list[str]]:
        with self._lock:
            version, hashes = self._pointers.get(key, (0, ()))
            return version, list(hashes)

    def swap(
        self,
        key: tuple[str, str],
        expected_version: int,
        new_hashes: list[str],
    ) -> int:
        with self._lock:
            current_version, _ = self._pointers.get(key, (0, ()))
            if current_version != expected_version:
                raise ValueError("stale active pointer version")
            new_version = current_version + 1
            self._pointers[key] = (new_version, tuple(new_hashes))
            return new_version
