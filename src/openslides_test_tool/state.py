"""Bookkeeping of everything the tool creates or changes, and the cleanup.

The state is written to `state.json` after every change, so a crashed or
killed run can be cleaned up later with `openslides-test-tool cleanup --state <file>`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .client import OpenSlidesError, OSClient
from .protocol import Protocol


class State:
    KINDS = ("polls", "motions", "assignments", "users")

    def __init__(self, path: Path, data: dict[str, Any] | None = None):
        self.path = path
        self.data: dict[str, Any] = data or {}
        for kind in self.KINDS:
            self.data.setdefault(kind, [])
        self.data.setdefault("org_settings", {})

    @classmethod
    def load(cls, path: Path) -> "State":
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, kind: str, ids: list[int]) -> None:
        self.data[kind].extend(ids)
        self.save()

    def remove(self, kind: str, ids: list[int]) -> None:
        drop = set(ids)
        self.data[kind] = [i for i in self.data[kind] if i not in drop]
        self.save()

    def remember_org_setting(self, field: str, original: Any) -> None:
        # Only remember the first original value.
        self.data["org_settings"].setdefault(field, original)
        self.save()

    def is_empty(self) -> bool:
        return not any(self.data[kind] for kind in self.KINDS) and not self.data["org_settings"]


def _is_gone(err: OpenSlidesError) -> bool:
    text = f"{err} {err.body}".lower()
    return "does not exist" in text or "not found" in text or err.status == 404


async def cleanup(admin: OSClient, state: State, protocol: Protocol, batch: int = 100) -> bool:
    """Deletes everything in the state and restores changed settings.

    Returns True if everything was cleaned up.
    """
    from .polls import get_api  # local import to avoid a cycle

    success = True

    if state.data["polls"]:
        api = get_api(state.data.get("api", "classic"))
        polls = list(state.data["polls"])
        with protocol.step("cleanup_polls", count=len(polls)):
            for poll_id in polls:
                try:
                    await api.delete(admin, poll_id)
                except OpenSlidesError as err:
                    if not _is_gone(err):
                        protocol.warn(f"could not delete poll {poll_id}: {err}")
                        success = False
                        continue
                state.remove("polls", [poll_id])

    for kind, action in (("motions", "motion.delete"), ("assignments", "assignment.delete")):
        ids = list(state.data[kind])
        if not ids:
            continue
        with protocol.step(f"cleanup_{kind}", count=len(ids)):
            for id_ in ids:
                try:
                    await admin.action(action, [{"id": id_}])
                except OpenSlidesError as err:
                    if not _is_gone(err):
                        protocol.warn(f"could not delete {kind[:-1]} {id_}: {err}")
                        success = False
                        continue
                state.remove(kind, [id_])

    users = list(state.data["users"])
    if users:
        with protocol.step("cleanup_users", count=len(users)):
            for start in range(0, len(users), batch):
                chunk = users[start : start + batch]
                try:
                    await admin.action("user.delete", [{"id": id_} for id_ in chunk])
                    state.remove("users", chunk)
                except OpenSlidesError:
                    # Retry one by one to find the broken one.
                    for id_ in chunk:
                        try:
                            await admin.action("user.delete", [{"id": id_}])
                        except OpenSlidesError as err:
                            if not _is_gone(err):
                                protocol.warn(f"could not delete user {id_}: {err}")
                                success = False
                                continue
                        state.remove("users", [id_])

    settings = dict(state.data["org_settings"])
    if settings:
        with protocol.step("restore_organization_settings", fields=",".join(settings)):
            try:
                await admin.action("organization.update", [{"id": 1, **settings}])
                state.data["org_settings"] = {}
                state.save()
            except OpenSlidesError as err:
                protocol.warn(f"could not restore organization settings {settings}: {err}")
                success = False

    if success:
        protocol.log("Cleanup vollständig – alle Testdaten entfernt.")
    else:
        protocol.warn(f"Cleanup unvollständig. Rest steht in {state.path}; erneut mit 'openslides-test-tool cleanup --state {state.path}'.")
    return success
