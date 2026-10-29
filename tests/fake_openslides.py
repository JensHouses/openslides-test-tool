"""In-memory fake of the OpenSlides HTTP API, used as httpx.MockTransport.

Supports the classic poll actions and the new vote service, enough to run the
whole scenario end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.parse
from collections import Counter
from typing import Any

import httpx


def _token(user_id: int, ttl: int = 600) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return "bearer " + ".".join([enc({"alg": "none"}), enc({"userId": user_id, "exp": int(time.time()) + ttl}), "sig"])


class ActionError(Exception):
    pass


class FakeOpenSlides:
    def __init__(self, api: str = "classic", electronic_voting: bool = True, stop_delay: float = 0.01):
        self.api = api
        self.stop_delay = stop_delay
        self.db: dict[str, Any] = {
            "organization/1/enable_electronic_voting": electronic_voting,
            "meeting/1/name": "Testmeeting",
            "meeting/1/group_ids": [1, 2],
            "group/1/name": "Default",
            "group/1/external_id": "Default",
            "group/2/name": "Delegates",
            "group/2/external_id": "Delegates",
            "motion_workflow/1/state_ids": [1, 2],
            "motion_state/1/allow_create_poll": False,
            "motion_state/1/workflow_id": 1,
            "motion_state/2/allow_create_poll": True,
            "motion_state/2/workflow_id": 1,
        }
        self.users: dict[str, tuple[int, str]] = {"superadmin": (1, "secret")}
        self.next_id: Counter = Counter({"user": 1})
        self.ballots: dict[int, dict[int, Any]] = {}
        self.changed = asyncio.Event()
        self.action_log: list[str] = []
        self.vote_requests = 0

    # ----------------------------------------------------------- helpers

    def new_id(self, collection: str) -> int:
        self.next_id[collection] += 1
        return self.next_id[collection]

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def delete_model(self, collection: str, id_: int) -> None:
        prefix = f"{collection}/{id_}/"
        if not any(k.startswith(prefix) for k in self.db):
            raise ActionError(f"Model '{collection}/{id_}' does not exist.")
        for key in [k for k in self.db if k.startswith(prefix)]:
            del self.db[key]

    def ids(self, collection: str) -> set[int]:
        return {int(k.split("/")[1]) for k in self.db if k.startswith(collection + "/")}

    def resolve(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for request in requests:
            for id_ in request["ids"]:
                for field, sub in request["fields"].items():
                    key = f"{request['collection']}/{id_}/{field}"
                    value = self.db.get(key)
                    if value is None:
                        continue
                    out[key] = value
                    if sub:
                        ids = value if sub["type"] == "relation-list" else [value]
                        out.update(self.resolve([{"collection": sub["collection"], "ids": ids, "fields": sub["fields"]}]))
        return out

    # ----------------------------------------------------------- transport

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/system/auth/login":
            body = json.loads(request.content)
            user = self.users.get(body["username"])
            if not user or user[1] != body["password"]:
                return httpx.Response(403, json={"message": "Username or password is incorrect."})
            return httpx.Response(
                200,
                json={"success": True},
                headers={"authentication": _token(user[0]), "set-cookie": f"refreshId=bearer%20u{user[0]}; Path=/"},
            )

        uid = self.auth(request)
        if path == "/system/auth/who-am-i":
            return httpx.Response(200, headers={"authentication": _token(uid)})
        if uid is None:
            return httpx.Response(401, json={"message": "not logged in"})

        if path == "/system/action/handle_request":
            return self.handle_actions(json.loads(request.content))
        if path == "/system/presenter/handle_request":
            return self.handle_presenters(json.loads(request.content))
        if path == "/system/autoupdate":
            requests = json.loads(request.content)
            if request.url.params.get("single"):
                return httpx.Response(200, json=self.resolve(requests))
            return httpx.Response(200, content=self.stream(requests))
        if path == "/system/vote" and self.api == "classic":
            return self.classic_vote(int(request.url.params["id"]), uid, json.loads(request.content))
        if path.startswith("/system/vote/poll") and self.api == "new":
            return self.new_vote_service(request, path, uid)
        return httpx.Response(404, text="404 page not found")

    def auth(self, request: httpx.Request) -> int | None:
        token = request.headers.get("authentication", "")
        if not token:
            return None
        payload = token.removeprefix("bearer ").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["userId"]

    async def stream(self, requests):
        last = self.resolve(requests)
        yield (json.dumps(last) + "\n").encode()
        while True:
            await self.changed.wait()
            current = self.resolve(requests)
            diff = {k: v for k, v in current.items() if last.get(k) != v}
            last = current
            if diff:
                yield (json.dumps(diff) + "\n").encode()

    # ------------------------------------------------------------- actions

    def handle_actions(self, payload: list[dict[str, Any]]) -> httpx.Response:
        results = []
        try:
            for action in payload:
                self.action_log.append(action["action"])
                handler = getattr(self, "a_" + action["action"].replace(".", "_"))
                results.append([handler(item) for item in action["data"]])
        except ActionError as err:
            return httpx.Response(400, json={"success": False, "message": str(err)})
        self.notify()
        return httpx.Response(200, json={"success": True, "results": results})

    def handle_presenters(self, payload: list[dict[str, Any]]) -> httpx.Response:
        results = []
        for item in payload:
            self.action_log.append("presenter:" + item["presenter"])
            handler = getattr(self, "p_" + item["presenter"])
            results.append(handler(item["data"]))
        return httpx.Response(200, json=results)

    def p_search_users(self, data):
        by_name = {name.lower(): (name, uid) for name, (uid, _) in self.users.items()}
        out = []
        for search in data["search"]:
            hit = by_name.get(search.get("username", "").strip().lower())
            out.append([{"id": hit[1], "username": hit[0]}] if hit else [])
        return out

    def a_organization_update(self, item):
        for key, value in item.items():
            if key != "id":
                self.db[f"organization/1/{key}"] = value

    def add_user(self, username: str, password: str, meeting_id: int | None = None, group_ids=(), present=True) -> int:
        """Creates a user directly (test setup) and returns its id."""
        if username in self.users:
            raise ActionError(f"A user with the username {username} already exists.")
        uid = self.new_id("user")
        self.users[username] = (uid, password)
        self.db[f"user/{uid}/username"] = username
        self.db[f"user/{uid}/is_active"] = True
        self.db[f"user/{uid}/meeting_user_ids"] = []
        self.db[f"user/{uid}/is_present_in_meeting_ids"] = []
        self.db.setdefault("organization/1/user_ids", [1]).append(uid)
        if meeting_id:
            self.join_meeting(uid, meeting_id, list(group_ids), present)
        return uid

    def join_meeting(self, uid: int, meeting_id: int, group_ids: list[int], present: bool) -> int:
        for mu in self.db[f"user/{uid}/meeting_user_ids"]:
            if self.db[f"meeting_user/{mu}/meeting_id"] == meeting_id:
                break
        else:
            mu = self.new_id("meeting_user")
            self.db[f"user/{uid}/meeting_user_ids"].append(mu)
            self.db[f"meeting_user/{mu}/meeting_id"] = meeting_id
            self.db[f"meeting_user/{mu}/user_id"] = uid
        self.db[f"meeting_user/{mu}/group_ids"] = group_ids
        presence = self.db[f"user/{uid}/is_present_in_meeting_ids"]
        if present and meeting_id not in presence:
            presence.append(meeting_id)
        return mu

    def a_user_create(self, item):
        uid = self.add_user(
            item["username"],
            item["default_password"],
            item.get("meeting_id"),
            item.get("group_ids", []),
            item.get("meeting_id") in item.get("is_present_in_meeting_ids", []),
        )
        return {"id": uid}

    def a_user_update(self, item):
        uid = item["id"]
        if f"user/{uid}/username" not in self.db:
            raise ActionError(f"Model 'user/{uid}' does not exist.")
        if "is_active" in item:
            self.db[f"user/{uid}/is_active"] = item["is_active"]
        if "meeting_id" in item:
            self.join_meeting(uid, item["meeting_id"], item.get("group_ids", []), False)
        if "is_present_in_meeting_ids" in item:
            self.db[f"user/{uid}/is_present_in_meeting_ids"] = list(item["is_present_in_meeting_ids"])

    def a_user_set_password(self, item):
        uid = item["id"]
        username = self.db.get(f"user/{uid}/username")
        if username is None:
            raise ActionError(f"Model 'user/{uid}' does not exist.")
        self.users[username] = (uid, item["password"])

    def a_user_delete(self, item):
        uid = item["id"]
        username = self.db.get(f"user/{uid}/username")
        for mu in self.db.get(f"user/{uid}/meeting_user_ids", []):
            self.delete_model("meeting_user", mu)
        self.delete_model("user", uid)
        self.users.pop(username, None)
        self.db["organization/1/user_ids"] = [u for u in self.db.get("organization/1/user_ids", []) if u != uid]

    def a_motion_create(self, item):
        mid = self.new_id("motion")
        self.db[f"motion/{mid}/title"] = item["title"]
        self.db[f"motion/{mid}/state_id"] = 1
        return {"id": mid}

    def a_motion_set_state(self, item):
        self.db[f"motion/{item['id']}/state_id"] = item["state_id"]

    def a_motion_delete(self, item):
        self.delete_model("motion", item["id"])

    def a_assignment_create(self, item):
        aid = self.new_id("assignment")
        self.db[f"assignment/{aid}/title"] = item["title"]
        return {"id": aid}

    def a_assignment_delete(self, item):
        self.delete_model("assignment", item["id"])

    # classic polls
    def a_poll_create(self, item):
        if not self.db["organization/1/enable_electronic_voting"]:
            raise ActionError("Electronic voting is not allowed.")
        if item["content_object_id"].startswith("motion/"):
            state = self.db[f"{item['content_object_id']}/state_id"]
            if not self.db[f"motion_state/{state}/allow_create_poll"]:
                raise ActionError("Motion state doesn't allow to create poll.")
        pid = self.new_id("poll")
        option_ids = []
        for option in item["options"]:
            oid = self.new_id("option")
            option_ids.append(oid)
            self.db[f"option/{oid}/content_object_id"] = option["content_object_id"]
        self.db.update(
            {
                f"poll/{pid}/state": "created",
                f"poll/{pid}/pollmethod": item["pollmethod"],
                f"poll/{pid}/option_ids": option_ids,
            }
        )
        return {"id": pid}

    def a_poll_start(self, item):
        self.db[f"poll/{item['id']}/state"] = "started"
        self.ballots[item["id"]] = {}

    def a_poll_stop(self, item):
        pid = item["id"]
        self.db[f"poll/{pid}/state"] = "finished"
        ballots = self.ballots.pop(pid, {})
        counts: dict[str, Counter] = {}
        for value in ballots.values():
            for oid, choice in value.items():
                counts.setdefault(oid, Counter())[{"Y": "yes", "N": "no", "A": "abstain", 1: "yes"}[choice]] += 1
        for oid in self.db[f"poll/{pid}/option_ids"]:
            for key in ("yes", "no", "abstain"):
                self.db[f"option/{oid}/{key}"] = f"{counts.get(str(oid), Counter())[key]}.000000"
        self.db[f"poll/{pid}/votescast"] = f"{len(ballots)}.000000"
        self.db[f"poll/{pid}/votesvalid"] = f"{len(ballots)}.000000"

    def a_poll_publish(self, item):
        self.db[f"poll/{item['id']}/state"] = "published"

    def a_poll_delete(self, item):
        for oid in self.db.get(f"poll/{item['id']}/option_ids", []):
            self.delete_model("option", oid)
        self.delete_model("poll", item["id"])

    def classic_vote(self, pid: int, uid: int, body: dict) -> httpx.Response:
        self.vote_requests += 1
        if self.db.get(f"poll/{pid}/state") != "started":
            return httpx.Response(400, json={"error": "not-started", "message": "Poll is not started"})
        if uid in self.ballots[pid]:
            return httpx.Response(400, json={"error": "double-vote", "message": "Not the first vote"})
        self.ballots[pid][uid] = body["value"]
        return httpx.Response(200)

    # ------------------------------------------------------- new vote service

    def new_vote_service(self, request: httpx.Request, path: str, uid: int) -> httpx.Response:
        parts = path.removeprefix("/system/vote/poll").strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts == [""]:
            if not body.get("title"):
                return httpx.Response(400, json={"error": "invalid", "message": "Field 'title' can not be empty"})
            pid = self.new_id("poll")
            option_ids = []
            for _ in body.get("options", []):
                option_ids.append(self.new_id("poll_option"))
            self.db.update(
                {
                    f"poll/{pid}/state": "created",
                    f"poll/{pid}/method": body["method"],
                    f"poll/{pid}/option_ids": option_ids,
                }
            )
            self.notify()
            return httpx.Response(200, json={"poll_id": pid})
        pid = int(parts[0])
        if f"poll/{pid}/state" not in self.db:
            return httpx.Response(404, json={"error": "not-exist", "message": "poll does not exist"})
        command = parts[1] if len(parts) > 1 else ""
        if request.method == "DELETE":
            self.delete_model("poll", pid)
        elif command == "start":
            self.db[f"poll/{pid}/state"] = "started"
            self.ballots[pid] = {}
        elif command == "vote":
            self.vote_requests += 1
            if uid in self.ballots.get(pid, {}):
                return httpx.Response(400, json={"error": "double-vote", "message": "Not the first vote"})
            self.ballots[pid][uid] = body["value"]
        elif command == "finalize":
            ballots = self.ballots.pop(pid, {})
            result: Counter = Counter()
            for value in ballots.values():
                if isinstance(value, list):
                    for oid in value:
                        result[str(oid)] += 1
                else:
                    result[value] += 1
            data = {k: str(v) for k, v in result.items()}
            data["total_ballots"] = len(ballots)
            self.db[f"poll/{pid}/state"] = "finished"
            self.db[f"poll/{pid}/result"] = json.dumps(data)
            if "publish" in urllib.parse.parse_qs(request.url.query.decode(), keep_blank_values=True):
                self.db[f"poll/{pid}/published"] = True
        self.notify()
        return httpx.Response(200, json={})
