"""Adapters for the two poll APIs of OpenSlides 4.

classic:  OpenSlides 4.0 - 4.3 (stable). Polls are handled by backend actions
          (poll.create/start/stop/publish/delete), votes go to
          `POST /system/vote?id=<poll_id>`.

new:      The reworked vote service (branch `feature/vote` of
          openslides-vote-service, used by ostcar's fork of
          openslides-performance). Everything goes through
          `/system/vote/poll/...`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .client import OpenSlidesError, OSClient, _error_message

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


class NewPollApi(PollApi):
    name = "new"

    BASE = "/system/vote/poll"

    async def _post(self, admin: OSClient, path: str, body: dict[str, Any] | None = None) -> Any:
        response = await admin.request("POST", path, json=body if body is not None else {})
        if response.status_code != 200:
            raise OpenSlidesError(
                f"POST {path} failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )
        try:
            return response.json()
        except ValueError:
            return None

    async def create(self, admin: OSClient, spec: PollSpec) -> PollHandle:
        body: dict[str, Any] = {
            "title": spec.title,
            "content_object_id": spec.content_object_id,
            "meeting_id": spec.meeting_id,
            "visibility": spec.poll_type,
            "entitled_group_ids": [spec.group_id],
        }
        if spec.target == "motion":
            body.update(method="approval", method_config={"allow_abstain": True})
        else:
            meeting_user_ids = [mu for _, mu in spec.candidates if mu]
            body.update(
                method="selection",
                method_config={"max_options_amount": 1, "min_options_amount": 1},
                option_type="meeting_user",
                options=meeting_user_ids,
            )
        if spec.live_voting and spec.poll_type != "secret":
            body["live_voting_enabled"] = True
        result = await self._post(admin, f"{self.BASE}/", body)
        poll_id = int(result["poll_id"])
        handle = PollHandle(poll_id, spec)
        if spec.target != "motion":
            handle.option_ids = await self._read_option_ids(admin, poll_id)
        return handle

    async def start(self, admin: OSClient, poll_id: int) -> None:
        await self._post(admin, f"{self.BASE}/{poll_id}/start")

    def ballot(self, handle: PollHandle, choice: Any) -> tuple[str, dict[str, Any]]:
        if handle.target == "motion":
            return f"{self.BASE}/{handle.id}/vote", {"value": choice}
        return f"{self.BASE}/{handle.id}/vote", {"value": [choice]}

    async def stop(self, admin: OSClient, poll_id: int, publish: bool) -> None:
        path = f"{self.BASE}/{poll_id}/finalize" + ("?publish" if publish else "")
        await self._post(admin, path)

    async def delete(self, admin: OSClient, poll_id: int) -> None:
        response = await admin.request("DELETE", f"{self.BASE}/{poll_id}")
        if response.status_code != 200:
            raise OpenSlidesError(
                f"DELETE poll {poll_id} failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )

    def observer_fields(self) -> dict[str, Any]:
        return {"state": None, "result": None, "published": None, "option_ids": None}

    def is_finished(self, data: dict[str, Any], poll_id: int) -> bool:
        return self.state(data, poll_id) == "finished" and data.get(f"poll/{poll_id}/result") not in (None, "")

    def tally(self, data: dict[str, Any], handle: PollHandle) -> tuple[dict[Any, float], float | None]:
        raw = data.get(f"poll/{handle.id}/result")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        result = raw if isinstance(raw, dict) else {}
        if handle.target == "motion":
            counts = {c: _to_float(result.get(c)) for c in MOTION_CHOICES}
        else:
            counts = {o: _to_float(result.get(str(o))) for o in handle.option_ids}
        total = result.get("total_ballots")
        return counts, (_to_float(total) if total is not None else None)


APIS: dict[str, PollApi] = {"classic": ClassicPollApi(), "new": NewPollApi()}


def get_api(name: str) -> PollApi:
    return APIS[name]


async def detect_api(admin: OSClient) -> str:
    """Finds out which vote service is running.

    The classic vote service only knows `/system/vote` (exact path), so a
    request to `/system/vote/poll/` returns 404. The new service answers with
    a validation error.
    """
    response = await admin.request("POST", f"{NewPollApi.BASE}/", json={})
    return "classic" if response.status_code in (404, 405) else "new"
