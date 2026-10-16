"""Watches polls via a streaming autoupdate connection – like a browser does.

With this we measure when a change (poll started, result available) is
really visible for clients, not only when the write request returned.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .client import OpenSlidesError, OSClient
from .protocol import Protocol


class Observer:
    def __init__(self, client: OSClient, requests: list[dict[str, Any]], protocol: Protocol, fallback_interval: float = 2.0):
        self.client = client
        self.requests = requests
        self.protocol = protocol
        self.fallback_interval = fallback_interval
        self.data: dict[str, Any] = {}
        self.messages = 0
        self._changed = asyncio.Event()
        self._connected = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False

    def _merge(self, message: dict[str, Any]) -> None:
        changed = False
        for key, value in message.items():
            if self.data.get(key) != value:
                self.data[key] = value
                changed = True
        if changed:
            self._changed.set()

    async def start(self, timeout: float = 30.0) -> float:
        """Starts the stream and waits for the first message. Returns the connect time."""
        start = time.perf_counter()
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            self.protocol.warn("autoupdate stream: no first message, using polling fallback only")
        return time.perf_counter() - start

    async def _run(self) -> None:
        while not self._stopped:
            try:
                async with self.client.stream(self.requests) as messages:
                    async for message in messages:
                        self.messages += 1
                        if "type" in message and "msg" in message and len(message) <= 3:
                            # error message from the autoupdate service
                            self.protocol.warn(f"autoupdate error: {message}")
                            continue
                        self._merge(message)
                        self._connected.set()
            except asyncio.CancelledError:
                raise
            except (OpenSlidesError, Exception) as err:  # noqa: BLE001 - keep the observer alive
                if self._stopped:
                    return
                self.protocol.warn(f"autoupdate stream broken ({type(err).__name__}: {err}), reconnecting")
            if not self._stopped:
                await asyncio.sleep(1)

    async def refresh(self) -> None:
        """Reads the data once without the stream (fallback)."""
        try:
            self._merge(await self.client.get(self.requests))
        except OpenSlidesError as err:
            self.protocol.warn(f"autoupdate single read failed: {err}")

    async def wait_for(self, predicate: Callable[[dict[str, Any]], bool], timeout: float) -> bool:
        """Waits until predicate(data) is true. Uses a single read as fallback."""
        deadline = time.perf_counter() + timeout
        next_fallback = time.perf_counter() + self.fallback_interval
        while True:
            if predicate(self.data):
                return True
            now = time.perf_counter()
            if now >= deadline:
                return False
            if now >= next_fallback:
                await self.refresh()
                next_fallback = time.perf_counter() + self.fallback_interval
                continue
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(deadline, next_fallback) - now)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
