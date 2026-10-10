"""Bounded MPSC ring with nonblocking admission and explicit overflow counts.

Portable Python provides no general compare-and-swap primitive. This queue
uses short lock sections, and deliberately makes no formal lock-free claim.
Producer admission never waits for the lock or disk IO.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RingStats:
    capacity: int
    size: int
    admitted: int
    rejected_full: int
    rejected_contention: int
    queued_bytes: int
    byte_capacity: int | None
    rejected_bytes: int


class RingBuffer[T]:
    def __init__(
        self, capacity: int = 16_384, *, max_bytes: int | None = None,
        sizeof: Callable[[T], int] | None = None,
    ) -> None:
        if not 1 <= capacity <= 1_048_576:
            raise ValueError("queue capacity must be between 1 and 1048576")
        self.capacity = capacity
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise ValueError("byte capacity must be a positive integer")
        if max_bytes is not None and sizeof is None:
            raise ValueError("a byte-limited queue requires an item size function")
        self.max_bytes = max_bytes
        self._sizeof = sizeof
        self._weights = [0] * capacity
        self._bytes = 0
        self._rejected_bytes = 0
        self._slots: list[T | None] = [None] * capacity
        self._head = 0
        self._tail = 0
        self._size = 0
        self._admitted = 0
        self._rejected_full = 0
        self._lock = threading.Lock()
        # Rejections due to lock contention are tracked with a separate lock;
        # admission itself always returns immediately on contention.
        self._contention = 0
        self._contention_lock = threading.Lock()

    def try_push(self, item: T) -> bool:
        if item is None:
            raise ValueError("None is reserved for empty queue slots")
        weight = self._sizeof(item) if self._sizeof is not None else 0
        if type(weight) is not int or weight < 0:
            raise ValueError("item size must be a nonnegative integer")
        if not self._lock.acquire(blocking=False):
            # Do not wait on metrics either. The count is a lower bound when
            # concurrent producers contend on both locks; stats expose it as such.
            if self._contention_lock.acquire(blocking=False):
                try:
                    self._contention += 1
                finally:
                    self._contention_lock.release()
            return False
        try:
            if self._size == self.capacity:
                self._rejected_full += 1
                return False
            if self.max_bytes is not None and self._bytes + weight > self.max_bytes:
                self._rejected_bytes += 1
                return False
            self._slots[self._tail] = item
            self._weights[self._tail] = weight
            self._bytes += weight
            self._tail = (self._tail + 1) % self.capacity
            self._size += 1
            self._admitted += 1
            return True
        finally:
            self._lock.release()

    def pop_batch(self, limit: int = 256) -> list[T]:
        if limit < 1:
            raise ValueError("batch limit must be positive")
        items: list[T] = []
        with self._lock:
            for _ in range(min(limit, self._size)):
                item = self._slots[self._head]
                if item is None:
                    raise RuntimeError("queue occupied slot has no item")
                items.append(item)
                self._slots[self._head] = None
                self._bytes -= self._weights[self._head]
                self._weights[self._head] = 0
                self._head = (self._head + 1) % self.capacity
                self._size -= 1
        return items

    def stats(self) -> RingStats:
        with self._lock, self._contention_lock:
            return RingStats(
                self.capacity, self._size, self._admitted, self._rejected_full, self._contention,
                self._bytes, self.max_bytes, self._rejected_bytes,
            )

    def __len__(self) -> int:
        with self._lock:
            return self._size
