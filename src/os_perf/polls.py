"""Poll handling via the backend actions of OpenSlides 4
(poll.create/start/stop/publish/delete).

Votes go to `POST /system/vote?id=<poll_id>`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import OSClient

MOTION_CHOICES = ("yes", "no", "abstain")


@dataclass
class PollSpec:
    target: str  # "motion" or "assignment"
    meeting_id: int
    content_object_id: str  # e.g. "motion/12"
    title: str
    group_id: int
    poll_type: str  # "secret", "named" or "open"
    candidates: list[tuple[int, int | None]] = field(default_factory=list)  # (user_id, meeting_user_id)
    classic_backend: str | None = None  # "fast" / "long" for the classic API
    live_voting: bool = False


@dataclass
class PollHandle:
    id: int
    spec: PollSpec
    option_ids: list[int] = field(default_factory=list)

    @property
    def target(self) -> str:
        return self.spec.target

    def choices(self) -> list[Any]:
        """All possible ballot choices (used for random voting and the tally)."""
        if self.target == "motion":
            return list(MOTION_CHOICES)
        return list(self.option_ids)


class PollApi:
    name = ""

    async def create(self, admin: OSClient, spec: PollSpec) -> PollHandle:
        raise NotImplementedError

    async def start(self, admin: OSClient, poll_id: int) -> None:
        raise NotImplementedError

    def ballot(self, handle: PollHandle, choice: Any) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    async def stop(self, admin: OSClient, poll_id: int, publish: bool) -> None:
        raise NotImplementedError

    async def delete(self, admin: OSClient, poll_id: int) -> None:
        raise NotImplementedError

    def observer_fields(self) -> dict[str, Any]:
        raise NotImplementedError

    def state(self, data: dict[str, Any], poll_id: int) -> str | None:
        return data.get(f"poll/{poll_id}/state")

    def is_finished(self, data: dict[str, Any], poll_id: int) -> bool:
        raise NotImplementedError

    def tally(self, data: dict[str, Any], handle: PollHandle) -> tuple[dict[Any, float], float | None]:
        """Returns (count per choice, total ballots) from the observed data."""
        raise NotImplementedError

    async def _read_option_ids(self, admin: OSClient, poll_id: int) -> list[int]:
        data = await admin.get([{"collection": "poll", "ids": [poll_id], "fields": {"option_ids": None}}])
        return list(data.get(f"poll/{poll_id}/option_ids") or [])


def _to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class ClassicPollApi(PollApi):
    name = "classic"

    TYPE_MAP = {"secret": "pseudoanonymous", "named": "named", "open": "named"}

    async def create(self, admin: OSClient, spec: PollSpec) -> PollHandle:
        payload: dict[str, Any] = {
            "title": spec.title,
            "meeting_id": spec.meeting_id,
            "content_object_id": spec.content_object_id,
            "type": self.TYPE_MAP[spec.poll_type],
            "entitled_group_ids": [spec.group_id],
            "onehundred_percent_base": "disabled",
        }
        if spec.target == "motion":
            payload.update(
                pollmethod="YNA",
                options=[{"content_object_id": spec.content_object_id}],
            )
        else:
            payload.update(
                pollmethod="Y",
                min_votes_amount=1,
                max_votes_amount=1,
                max_votes_per_option=1,
                global_yes=False,
                global_no=False,
                global_abstain=False,
                options=[{"content_object_id": f"user/{uid}"} for uid, _ in spec.candidates],
            )
        if spec.classic_backend:
            payload["backend"] = spec.classic_backend
        if spec.live_voting and spec.poll_type != "secret":
            payload["live_voting_enabled"] = True
        result = await admin.action("poll.create", [payload])
        poll_id = int(result[0]["id"])
        handle = PollHandle(poll_id, spec)
        handle.option_ids = await self._read_option_ids(admin, poll_id)
        return handle

    async def start(self, admin: OSClient, poll_id: int) -> None:
        await admin.action("poll.start", [{"id": poll_id}])

    def ballot(self, handle: PollHandle, choice: Any) -> tuple[str, dict[str, Any]]:
        if handle.target == "motion":
            value = {str(handle.option_ids[0]): {"yes": "Y", "no": "N", "abstain": "A"}[choice]}
        else:
            value = {str(choice): 1}
        return f"/system/vote?id={handle.id}", {"value": value}

    async def stop(self, admin: OSClient, poll_id: int, publish: bool) -> None:
        await admin.action("poll.stop", [{"id": poll_id}])
        if publish:
            await admin.action("poll.publish", [{"id": poll_id}])

    async def delete(self, admin: OSClient, poll_id: int) -> None:
        await admin.action("poll.delete", [{"id": poll_id}])

    def observer_fields(self) -> dict[str, Any]:
        return {
            "state": None,
            "votescast": None,
            "votesvalid": None,
            "votesinvalid": None,
            "option_ids": {
                "type": "relation-list",
                "collection": "option",
                "fields": {"yes": None, "no": None, "abstain": None},
            },
        }

    def is_finished(self, data: dict[str, Any], poll_id: int) -> bool:
        return self.state(data, poll_id) in ("finished", "published") and (
            data.get(f"poll/{poll_id}/votescast") is not None
        )

    def tally(self, data: dict[str, Any], handle: PollHandle) -> tuple[dict[Any, float], float | None]:
        total = data.get(f"poll/{handle.id}/votescast")
        if handle.target == "motion":
            option = handle.option_ids[0]
            counts = {c: _to_float(data.get(f"option/{option}/{c}")) for c in MOTION_CHOICES}
        else:
            counts = {o: _to_float(data.get(f"option/{o}/yes")) for o in handle.option_ids}
        return counts, (_to_float(total) if total is not None else None)


APIS: dict[str, PollApi] = {"classic": ClassicPollApi()}


def get_api(name: str) -> PollApi:
    return APIS[name]
