"""The poll load test scenario.

1. Login superuser, preflight checks (meeting, group, electronic voting, API)
2. Create n users in the meeting (same password) and log them in
3. Per round: create `parallel` motions/assignments with one poll each
4. Wait for the operator (ENTER)
5. Start all polls, all users vote in all polls
6. Stop all polls, measure until the results are visible
7. Validate the results against the sent ballots
8. On exit (also Ctrl+C): delete everything, restore settings
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .client import OpenSlidesError, OSClient, make_http_client
from .observer import Observer
from .polls import PollApi, PollHandle, PollSpec, detect_api, get_api
from .protocol import Protocol, latency_stats
from .state import State, cleanup, delete_users


class Abort(Exception):
    """The operator aborted the run."""


@dataclass
class Outcome:
    ok: bool
    latency: float
    t_send: float
    t_done: float
    result: Any = None
    error: str = ""


@dataclass
class Job:
    kind: str
    user: str
    poll_id: int | None
    run: Callable[[], Awaitable[Any]]
    delay: float = 0.0


async def run_jobs(protocol: Protocol, jobs: list[Job], concurrency: int, round_: int | None = None) -> list[Outcome]:
    """Runs many requests with limited concurrency and records each of them."""
    semaphore = asyncio.Semaphore(concurrency) if concurrency > 0 else None

    async def one(job: Job) -> Outcome:
        if job.delay > 0:
            await asyncio.sleep(job.delay)
        if semaphore:
            await semaphore.acquire()
        t_send = time.perf_counter()
        try:
            result = await job.run()
            t_done = time.perf_counter()
            outcome = Outcome(True, t_done - t_send, t_send, t_done, result)
            status = 200
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - every error is a measurement
            t_done = time.perf_counter()
            status = getattr(err, "status", None)
            outcome = Outcome(False, t_done - t_send, t_send, t_done, error=f"{type(err).__name__}: {err}")
        finally:
            if semaphore:
                semaphore.release()
        protocol.request(
            job.kind,
            round_=round_,
            poll_id=job.poll_id,
            user=job.user,
            t_send=outcome.t_send,
            latency=outcome.latency,
            ok=outcome.ok,
            status=status,
            error=outcome.error,
        )
        return outcome

    return list(await asyncio.gather(*(one(job) for job in jobs)))


def stats_of(outcomes: list[Outcome]) -> dict[str, Any]:
    ok = [o.latency for o in outcomes if o.ok]
    errors = sum(1 for o in outcomes if not o.ok)
    wall = None
    if outcomes:
        wall = max(o.t_done for o in outcomes) - min(o.t_send for o in outcomes)
    return latency_stats(ok, errors, wall)


def report_errors(protocol: Protocol, name: str, outcomes: list[Outcome], limit: int = 5) -> None:
    errors = Counter(o.error for o in outcomes if not o.ok)
    for error, count in errors.most_common(limit):
        protocol.warn(f"  {name}: {count}x {error}")


# ---------------------------------------------------------------- operator


async def ask(prompt: str) -> str:
    """Reads one line from stdin without blocking the event loop."""
    print(prompt, flush=True)
    loop = asyncio.get_running_loop()
    if sys.stdin is None or sys.stdin.closed:
        raise Abort("no stdin")
    try:
        fd = sys.stdin.fileno()
        future: asyncio.Future[str] = loop.create_future()

        def on_input() -> None:
            line = sys.stdin.readline()
            if not future.done():
                future.set_result(line if line else "q")

        loop.add_reader(fd, on_input)
    except (NotImplementedError, OSError, ValueError):
        return await asyncio.to_thread(sys.stdin.readline)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


async def wait_operator(protocol: Protocol, prompt: str) -> None:
    start = time.perf_counter()
    answer = (await ask(prompt)).strip().lower()
    protocol.event("operator", prompt=prompt, answer=answer, waited_s=time.perf_counter() - start)
    if answer in ("q", "quit", "exit", "n"):
        raise Abort("operator quit")


# ---------------------------------------------------------------- scenario


class Scenario:
    def __init__(self, args: argparse.Namespace, protocol: Protocol, http: httpx.AsyncClient):
        self.args = args
        self.protocol = protocol
        self.http = http
        self.admin = OSClient(http, args.username, args.password)
        self.state = State(protocol.run_dir / "state.json")
        self.rng = random.Random(args.seed)
        self.api: PollApi | None = None
        self.group_id: int = 0
        self.voters: list[OSClient] = []
        self.users: list[tuple[int, str]] = []
        self.reused: set[int] = set()
        self.meeting_users: dict[int, int] = {}
        self.validation: list[dict[str, Any]] = []

    # ------------------------------------------------------------ preflight

    async def preflight(self) -> None:
        args, protocol = self.args, self.protocol
        with protocol.step("admin_login", username=args.username):
            await self.admin.login()

        with protocol.step("preflight", meeting_id=args.meeting) as step:
            data = await self.admin.get(
                [
                    {
                        "collection": "meeting",
                        "ids": [args.meeting],
                        "fields": {
                            "name": None,
                            "group_ids": {
                                "type": "relation-list",
                                "collection": "group",
                                "fields": {"name": None, "external_id": None},
                            },
                        },
                    },
                    {"collection": "organization", "ids": [1], "fields": {"enable_electronic_voting": None}},
                ]
            )
            name = data.get(f"meeting/{args.meeting}/name")
            if name is None:
                raise OpenSlidesError(f"meeting {args.meeting} not found (or no permission to see it)")
            step.details["meeting"] = name

            groups = {
                gid: (data.get(f"group/{gid}/name"), data.get(f"group/{gid}/external_id"))
                for gid in data.get(f"meeting/{args.meeting}/group_ids") or []
            }
            if args.group_id:
                if args.group_id not in groups:
                    raise OpenSlidesError(f"group {args.group_id} is not part of meeting {args.meeting}: {groups}")
                self.group_id = args.group_id
            else:
                for gid, (gname, external_id) in groups.items():
                    if external_id == "Delegates" or gname in ("Delegates", "Delegierte"):
                        self.group_id = gid
                        break
                if not self.group_id:
                    raise OpenSlidesError(
                        f"no group 'Delegates' found in meeting {args.meeting}, use --group-id. Groups: {groups}"
                    )
            step.details["group"] = f"{self.group_id} ({groups[self.group_id][0]})"

            api_name = args.api if args.api != "auto" else await detect_api(self.admin)
            self.api = get_api(api_name)
            self.state.data.update(api=api_name, meeting_id=args.meeting, base_url=str(self.http.base_url))
            self.state.save()
            step.details["api"] = api_name
            protocol.meta["api"] = api_name
            protocol.meta["meeting"] = name

            if not data.get("organization/1/enable_electronic_voting"):
                if not args.enable_electronic_voting:
                    raise OpenSlidesError(
                        "electronic voting is disabled in the organization. "
                        "Enable it or use --enable-electronic-voting (will be reset afterwards)."
                    )
                self.state.remember_org_setting("enable_electronic_voting", False)
                await self.admin.action("organization.update", [{"id": 1, "enable_electronic_voting": True}])
                step.details["electronic_voting"] = "enabled temporarily"

    # ---------------------------------------------------------------- users

    async def prepare_users(self) -> None:
        """Makes sure `amount` test users exist: reuses persistent ones, creates the rest."""
        args = self.args
        names = [f"{args.user_prefix}{i}" for i in range(1, args.amount + 1)]
        if args.persistent_users:
            self.state.data["persistent_user_prefix"] = args.user_prefix
            self.state.save()
            found = await self.find_users(names)
            await self.attach_users(found)
            names = [n for n in names if n not in found]
        await self.create_users(names)
        if not self.users:
            raise OpenSlidesError("no user could be created")
        self.users.sort()

    async def find_users(self, names: list[str]) -> dict[str, int]:
        """Looks up existing users by exact username (organization wide). Returns name -> user id."""
        args, protocol = self.args, self.protocol
        chunks = [names[i : i + 200] for i in range(0, len(names), 200)]
        found: dict[str, int] = {}

        async def search(chunk: list[str]) -> int:
            result = await self.admin.presenter(
                "search_users",
                {
                    "permission_type": "meeting",
                    "permission_id": args.meeting,
                    "search": [{"username": name} for name in chunk],
                },
            )
            hits = 0
            for name, matches in zip(chunk, result):
                for match in matches or []:
                    if str(match.get("username", "")).lower() == name.lower():
                        found[name] = int(match["id"])
                        hits += 1
                        break
            return hits

        with protocol.step("find_users", amount=len(names), prefix=args.user_prefix) as step:
            jobs = [Job("search_users", f"chunk{i + 1}", None, (lambda c=c: search(c))) for i, c in enumerate(chunks)]
            outcomes = await run_jobs(protocol, jobs, args.create_concurrency)
            report_errors(protocol, "search_users", outcomes)
            if not all(o.ok for o in outcomes):
                raise OpenSlidesError("could not search for existing users")
            step.details["found"] = len(found)
            step.details["missing"] = len(names) - len(found)
        for name, uid in found.items():
            self.users.append((uid, name))
            self.reused.add(uid)
        return found

    async def attach_users(self, found: dict[str, int]) -> None:
        """Adds reused users to the meeting and group, marks them present and active where needed."""
        args, protocol = self.args, self.protocol
        if not found:
            return
        ids = sorted(found.values())
        data = await self.admin.get(
            [
                {
                    "collection": "user",
                    "ids": ids,
                    "fields": {
                        "is_active": None,
                        "is_present_in_meeting_ids": None,
                        "meeting_user_ids": {
                            "type": "relation-list",
                            "collection": "meeting_user",
                            "fields": {"meeting_id": None, "group_ids": None},
                        },
                    },
                }
            ]
        )
        updates: list[dict[str, Any]] = []
        for uid in ids:
            groups: list[int] = []
            in_meeting = False
            for mu in data.get(f"user/{uid}/meeting_user_ids") or []:
                if data.get(f"meeting_user/{mu}/meeting_id") == args.meeting:
                    in_meeting = True
                    self.meeting_users[uid] = mu
                    groups = list(data.get(f"meeting_user/{mu}/group_ids") or [])
            present = list(data.get(f"user/{uid}/is_present_in_meeting_ids") or [])
            active = bool(data.get(f"user/{uid}/is_active"))
            if in_meeting and self.group_id in groups and args.meeting in present and active:
                continue
            updates.append(
                {
                    "id": uid,
                    "is_active": True,
                    "meeting_id": args.meeting,
                    "group_ids": sorted({*groups, self.group_id}),
                    "is_present_in_meeting_ids": sorted({*present, args.meeting}),
                }
            )
        if not updates:
            return

        batches = [updates[i : i + args.create_batch] for i in range(0, len(updates), args.create_batch)]
        with protocol.step("attach_users", amount=len(updates), batches=len(batches)) as step:
            jobs = [
                Job("attach_users", f"batch{i + 1}", None, (lambda b=b: self.admin.action("user.update", b)))
                for i, b in enumerate(batches)
            ]
            outcomes = await run_jobs(protocol, jobs, args.create_concurrency)
            report_errors(protocol, "attach_users", outcomes)
            step.details["updated"] = sum(len(b) for b, o in zip(batches, outcomes) if o.ok)
            if not all(o.ok for o in outcomes):
                step.ok = False
        await self.ensure_meeting_users([u["id"] for u in updates])

    async def create_users(self, names: list[str]) -> None:
        args, protocol = self.args, self.protocol
        batches = [names[i : i + args.create_batch] for i in range(0, len(names), args.create_batch)]

        async def create(batch: list[str]) -> list[int]:
            result = await self.admin.action(
                "user.create",
                [
                    {
                        "username": name,
                        "first_name": "Perf",
                        "last_name": name,
                        "default_password": args.users_password,
                        "is_active": True,
                        "meeting_id": args.meeting,
                        "group_ids": [self.group_id],
                        "is_present_in_meeting_ids": [args.meeting],
                    }
                    for name in batch
                ],
            )
            ids = [int(r["id"]) for r in result]
            if not args.persistent_users:
                self.state.add("users", ids)
            for r, name in zip(result, batch):
                self.users.append((int(r["id"]), name))
                if r.get("meeting_user_id"):
                    self.meeting_users[int(r["id"])] = int(r["meeting_user_id"])
            return ids

        with protocol.step("create_users", amount=len(names), batch=args.create_batch, batches=len(batches)) as step:
            if not names:
                step.details["created"] = 0
                return
            before = len(self.users)
            jobs = [Job("create_users", f"batch{i + 1}", None, (lambda b=b: create(b))) for i, b in enumerate(batches)]
            outcomes = await run_jobs(protocol, jobs, args.create_concurrency)
            step.details["created"] = len(self.users) - before
            protocol.add_stats("create_users (per batch)", stats_of(outcomes))
            report_errors(protocol, "create_users", outcomes)
            if len(self.users) < args.amount:
                step.ok = False

    async def login_users(self) -> None:
        args, protocol = self.args, self.protocol
        ids = {name: uid for uid, name in self.users}
        clients = [OSClient(self.http, name, args.users_password) for _, name in self.users]
        with protocol.step("login_users", amount=len(clients), concurrency=args.login_concurrency) as step:
            jobs = [Job("login", c.username, None, c.login) for c in clients]
            outcomes = await run_jobs(protocol, jobs, args.login_concurrency)
            self.voters = [c for c, o in zip(clients, outcomes) if o.ok]
            # Reused users may have another password: set ours and try again.
            retry = [c for c, o in zip(clients, outcomes) if not o.ok and ids[c.username] in self.reused]
            if retry:
                outcomes += await self.reset_passwords_and_login(retry)
            step.details["logged_in"] = len(self.voters)
            protocol.add_stats("login", stats_of(outcomes))
            report_errors(protocol, "login", outcomes)
        if not self.voters:
            raise OpenSlidesError("no user could log in")

    async def reset_passwords_and_login(self, clients: list[OSClient]) -> list[Outcome]:
        args, protocol = self.args, self.protocol
        ids = {name: uid for uid, name in self.users}
        protocol.warn(f"  {len(clients)} wiederverwendete User konnten sich nicht einloggen – setze ihr Passwort")
        batches = [clients[i : i + args.create_batch] for i in range(0, len(clients), args.create_batch)]

        async def set_password(batch: list[OSClient]) -> None:
            await self.admin.action(
                "user.set_password",
                [{"id": ids[c.username], "password": args.users_password, "set_as_default": True} for c in batch],
            )

        jobs = [Job("set_password", f"batch{i + 1}", None, (lambda b=b: set_password(b))) for i, b in enumerate(batches)]
        outcomes = await run_jobs(protocol, jobs, args.create_concurrency)
        report_errors(protocol, "set_password", outcomes)
        retry = [c for b, o in zip(batches, outcomes) if o.ok for c in b]
        if not retry:
            return []
        outcomes = await run_jobs(protocol, [Job("login", c.username, None, c.login) for c in retry], args.login_concurrency)
        self.voters += [c for c, o in zip(retry, outcomes) if o.ok]
        return outcomes

    async def ensure_meeting_users(self, user_ids: list[int]) -> None:
        missing = [uid for uid in user_ids if uid not in self.meeting_users]
        if not missing:
            return
        data = await self.admin.get(
            [
                {
                    "collection": "user",
                    "ids": missing,
                    "fields": {
                        "meeting_user_ids": {
                            "type": "relation-list",
                            "collection": "meeting_user",
                            "fields": {"meeting_id": None},
                        }
                    },
                }
            ]
        )
        for uid in missing:
            for mu in data.get(f"user/{uid}/meeting_user_ids") or []:
                if data.get(f"meeting_user/{mu}/meeting_id") == self.args.meeting:
                    self.meeting_users[uid] = mu

    # --------------------------------------------------------------- content

    async def ensure_motion_can_have_polls(self, motion_id: int) -> None:
        data = await self.admin.get(
            [
                {
                    "collection": "motion",
                    "ids": [motion_id],
                    "fields": {
                        "state_id": {
                            "type": "relation",
                            "collection": "motion_state",
                            "fields": {
                                "allow_create_poll": None,
                                "workflow_id": {
                                    "type": "relation",
                                    "collection": "motion_workflow",
                                    "fields": {
                                        "state_ids": {
                                            "type": "relation-list",
                                            "collection": "motion_state",
                                            "fields": {"allow_create_poll": None},
                                        }
                                    },
                                },
                            },
                        }
                    },
                }
            ]
        )
        state_id = data.get(f"motion/{motion_id}/state_id")
        if state_id is None or data.get(f"motion_state/{state_id}/allow_create_poll"):
            return
        workflow_id = data.get(f"motion_state/{state_id}/workflow_id")
        for candidate in data.get(f"motion_workflow/{workflow_id}/state_ids") or []:
            if data.get(f"motion_state/{candidate}/allow_create_poll"):
                await self.admin.action("motion.set_state", [{"id": motion_id, "state_id": candidate}])
                return
        raise OpenSlidesError(f"workflow {workflow_id} has no state that allows polls")

    async def create_content(self, round_: int, index: int, target: str) -> tuple[str, int]:
        title = f"Perf-Test R{round_} #{index + 1} ({datetime.now():%H:%M:%S})"
        if target == "motion":
            result = await self.admin.action(
                "motion.create",
                [{"meeting_id": self.args.meeting, "title": title, "text": "<p>OpenSlides performance test</p>"}],
            )
            motion_id = int(result[0]["id"])
            self.state.add("motions", [motion_id])
            await self.ensure_motion_can_have_polls(motion_id)
            return f"motion/{motion_id}", motion_id
        result = await self.admin.action("assignment.create", [{"meeting_id": self.args.meeting, "title": title}])
        assignment_id = int(result[0]["id"])
        self.state.add("assignments", [assignment_id])
        return f"assignment/{assignment_id}", assignment_id

    async def create_polls(self, round_: int) -> list[PollHandle]:
        args, protocol, api = self.args, self.protocol, self.api
        assert api is not None
        targets = [args.targets[i % len(args.targets)] for i in range(args.parallel)]

        candidates: list[tuple[int, int | None]] = []
        if "assignment" in targets:
            candidate_ids = [uid for uid, _ in self.users[: args.candidates]]
            await self.ensure_meeting_users(candidate_ids)
            candidates = [(uid, self.meeting_users.get(uid)) for uid in candidate_ids]

        with protocol.step("create_content", round=round_, targets=",".join(targets)) as step:
            jobs = [
                Job("create_content", target, None, (lambda i=i, t=target: self.create_content(round_, i, t)))
                for i, target in enumerate(targets)
            ]
            outcomes = await run_jobs(protocol, jobs, 0, round_)
            report_errors(protocol, "create_content", outcomes)
            contents = [(t, o.result[0]) for t, o in zip(targets, outcomes) if o.ok]
            step.details["created"] = len(contents)
            if not contents:
                raise OpenSlidesError("could not create motions/assignments")

        async def create_poll(target: str, content_object_id: str, index: int) -> PollHandle:
            spec = PollSpec(
                target=target,
                meeting_id=args.meeting,
                content_object_id=content_object_id,
                title=f"{'Abstimmung' if target == 'motion' else 'Wahl'} R{round_} #{index + 1}",
                group_id=self.group_id,
                poll_type=args.poll_type,
                candidates=candidates,
                classic_backend=args.classic_backend,
                live_voting=args.live_voting,
            )
            handle = await api.create(self.admin, spec)
            self.state.add("polls", [handle.id])
            return handle

        with protocol.step("create_polls", round=round_, count=len(contents)) as step:
            jobs = [
                Job("create_poll", t, None, (lambda i=i, t=t, c=c: create_poll(t, c, i)))
                for i, (t, c) in enumerate(contents)
            ]
            outcomes = await run_jobs(protocol, jobs, 0, round_)
            report_errors(protocol, "create_poll", outcomes)
            polls = [o.result for o in outcomes if o.ok]
            step.details["poll_ids"] = ",".join(str(p.id) for p in polls)
            protocol.add_stats(f"R{round_} create_poll", stats_of(outcomes))
            if not polls:
                raise OpenSlidesError("could not create any poll")
        return polls

    # ---------------------------------------------------------------- voting

    def choose(self, handle: PollHandle) -> Any:
        choices = handle.choices()
        wanted = self.args.vote_choice
        if handle.target == "motion" and wanted in choices:
            return wanted
        if wanted == "first":
            return choices[0]
        return self.rng.choice(choices)

    async def run_round(self, round_: int) -> None:
        args, protocol, api = self.args, self.protocol, self.api
        assert api is not None
        polls = await self.create_polls(round_)
        poll_ids = [p.id for p in polls]

        observer = Observer(
            self.admin,
            [{"collection": "poll", "ids": poll_ids, "fields": api.observer_fields()}],
            protocol,
        )
        connect = await observer.start()
        protocol.record_step("observer_connect", connect, round=round_)
        try:
            if args.wait:
                await wait_operator(
                    protocol,
                    f"\n>>> Runde {round_}: {len(polls)} Poll(s) angelegt (IDs {poll_ids}), "
                    f"{len(self.voters)} Wähler bereit.\n>>> [ENTER] = Abstimmung starten, [q] = abbrechen & aufräumen",
                )

            with protocol.step("refresh_tokens", round=round_) as step:
                refreshed = await asyncio.gather(*(c.ensure_fresh(margin=300) for c in [self.admin, *self.voters]))
                step.details["refreshed"] = sum(refreshed)

            # ---- start
            start_sent: dict[int, float] = {}

            async def start(handle: PollHandle) -> None:
                start_sent[handle.id] = time.perf_counter()
                await api.start(self.admin, handle.id)

            with protocol.step("start_polls", round=round_, count=len(polls)):
                outcomes = await run_jobs(
                    protocol, [Job("start_poll", "admin", p.id, (lambda p=p: start(p))) for p in polls], 0, round_
                )
                report_errors(protocol, "start_poll", outcomes)
            started = [p for p, o in zip(polls, outcomes) if o.ok]
            if not started:
                raise OpenSlidesError("no poll could be started")

            with protocol.step("start_visible", round=round_):
                visible = await observer.wait_for(
                    lambda d: all(api.state(d, p.id) == "started" for p in started), args.result_timeout
                )
                if not visible:
                    raise OpenSlidesError("polls are not visible as started")

            # ---- vote
            expected: dict[int, Counter] = {p.id: Counter() for p in started}

            def vote_job(voter: OSClient, handle: PollHandle) -> Job:
                choice = self.choose(handle)

                async def send() -> Any:
                    path, body = api.ballot(handle, choice)
                    response = await voter.request("POST", path, json=body)
                    if response.status_code >= 300:
                        raise OpenSlidesError(f"{response.status_code} {response.text[:200]}", response.status_code)
                    expected[handle.id][choice] += 1
                    return choice

                delay = self.rng.uniform(0, args.vote_spread) if args.vote_spread > 0 else 0.0
                return Job("vote", voter.username, handle.id, send, delay)

            jobs = [vote_job(voter, handle) for voter in self.voters for handle in started]
            with protocol.step(
                "voting", round=round_, polls=len(started), voters=len(self.voters), ballots=len(jobs)
            ) as step:
                outcomes = await run_jobs(protocol, jobs, args.vote_concurrency, round_)
                step.details["accepted"] = sum(o.ok for o in outcomes)
                step.details["errors"] = sum(not o.ok for o in outcomes)
            protocol.add_stats(f"R{round_} vote (all polls)", stats_of(outcomes))
            for handle in started:
                poll_outcomes = [o for j, o in zip(jobs, outcomes) if j.poll_id == handle.id]
                protocol.add_stats(f"R{round_} vote poll {handle.id}", stats_of(poll_outcomes))
            report_errors(protocol, "vote", outcomes)

            # ---- stop + result
            if args.wait and args.wait_before_stop:
                await wait_operator(protocol, ">>> Alle Stimmen abgegeben. [ENTER] = Abstimmung beenden")

            timings: dict[int, dict[str, float]] = {}

            async def stop(handle: PollHandle) -> None:
                t_sent = time.perf_counter()
                await api.stop(self.admin, handle.id, args.publish)
                t_returned = time.perf_counter()
                ok = await observer.wait_for(lambda d: api.is_finished(d, handle.id), args.result_timeout)
                t_visible = time.perf_counter()
                timings[handle.id] = {
                    "stop_request_s": t_returned - t_sent,
                    "result_visible_s": t_visible - t_sent,
                    "t_sent": t_sent,
                    "t_visible": t_visible,
                }
                if not ok:
                    raise OpenSlidesError(f"result of poll {handle.id} not visible after {args.result_timeout}s")

            with protocol.step("stop_and_results", round=round_, count=len(started), publish=args.publish):
                outcomes = await run_jobs(
                    protocol, [Job("stop_poll", "admin", p.id, (lambda p=p: stop(p))) for p in started], 0, round_
                )
                report_errors(protocol, "stop_poll", outcomes)
            for poll_id, t in sorted(timings.items()):
                protocol.log(
                    f"  poll {poll_id}: stop-request {t['stop_request_s'] * 1000:.1f} ms, "
                    f"Ergebnis sichtbar nach {t['result_visible_s'] * 1000:.1f} ms"
                )
            if timings:
                protocol.add_stats(
                    f"R{round_} stop request", latency_stats([t["stop_request_s"] for t in timings.values()], 0)
                )
                protocol.add_stats(
                    f"R{round_} result visible", latency_stats([t["result_visible_s"] for t in timings.values()], 0)
                )
                protocol.record_step(
                    "results_visible_all",
                    max(t["t_visible"] for t in timings.values()) - min(t["t_sent"] for t in timings.values()),
                    round=round_,
                )

            # ---- validate
            for handle in started:
                self.validate(round_, handle, observer.data, expected[handle.id])
        finally:
            await observer.stop()

    def validate(self, round_: int, handle: PollHandle, data: dict[str, Any], expected: Counter) -> None:
        api = self.api
        assert api is not None
        if not api.is_finished(data, handle.id):
            self.validation.append({"round": round_, "poll_id": handle.id, "ok": False, "reason": "not finished"})
            return
        counts, total = api.tally(data, handle)
        sent = sum(expected.values())
        mismatches = {
            str(choice): {"expected": expected.get(choice, 0), "server": counts.get(choice, 0.0)}
            for choice in set(counts) | set(expected)
            if abs(counts.get(choice, 0.0) - expected.get(choice, 0)) > 1e-6
        }
        total_ok = total is None or abs(total - sent) < 1e-6
        ok = not mismatches and total_ok
        record = {
            "round": round_,
            "poll_id": handle.id,
            "ok": ok,
            "accepted_ballots": sent,
            "server_total": total,
            "counts": {str(k): v for k, v in counts.items()},
        }
        if mismatches:
            record["mismatches"] = mismatches
        self.validation.append(record)
        self.protocol.event("validation", **record)
        if ok:
            self.protocol.log(f"  ✔ Ergebnis poll {handle.id} korrekt ({sent} Stimmen: {dict(record['counts'])})")
        else:
            self.protocol.error(
                f"  ✘ Ergebnis poll {handle.id} weicht ab: gesendet={sent}, Server total={total}, Abweichungen={mismatches}"
            )


# ------------------------------------------------------------------ entry


def make_run_dir(args: argparse.Namespace) -> Path:
    name = f"{datetime.now():%Y%m%d-%H%M%S}_m{args.meeting}_n{args.amount}_p{args.parallel}"
    return Path(args.out) / name


async def run(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> int:
    protocol = Protocol(make_run_dir(args), quiet=args.quiet)
    protocol.meta["settings"] = {k: v for k, v in vars(args).items() if "password" not in k and k != "func"}
    protocol.log(f"OpenSlides Performance-Test gegen {args.base_url} – Protokoll in {protocol.run_dir}")
    exit_code = 0
    async with make_http_client(
        args.base_url,
        verify_tls=args.verify_tls,
        timeout=args.timeout,
        max_connections=args.max_connections,
        force_ipv4=args.ipv4,
        transport=transport,
    ) as http:
        scenario = Scenario(args, protocol, http)
        try:
            await scenario.preflight()
            await scenario.prepare_users()
            await scenario.login_users()
            round_ = 0
            while True:
                round_ += 1
                await scenario.run_round(round_)
                if args.rounds and round_ >= args.rounds:
                    break
                if not args.wait:
                    if not args.rounds:
                        break
                    continue
                await wait_operator(protocol, "\n>>> [ENTER] = weitere Runde, [q] = beenden & aufräumen")
        except Abort:
            protocol.log("Abbruch durch Bediener.")
        except asyncio.CancelledError:
            # Swallow the first Ctrl+C so the cleanup below can still await.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            protocol.warn("Unterbrochen (Ctrl+C) – räume auf … (nochmal Ctrl+C = hart abbrechen)")
            exit_code = 130
        except OpenSlidesError as err:
            protocol.error(f"Fehler: {err}")
            exit_code = 1
        except Exception as err:  # noqa: BLE001
            protocol.error(f"Unerwarteter Fehler: {type(err).__name__}: {err}")
            exit_code = 1
        finally:
            protocol.meta["validation"] = scenario.validation
            if any(not v["ok"] for v in scenario.validation):
                exit_code = exit_code or 2
            try:
                if args.persistent_users and scenario.users:
                    protocol.log(
                        f"{len(scenario.users)} User mit Prefix {args.user_prefix!r} bleiben erhalten. "
                        f"Löschen: openslides-test-tool cleanup-users --user-prefix {args.user_prefix}"
                    )
                if args.keep:
                    protocol.warn(f"--keep: Testdaten bleiben erhalten. Aufräumen: openslides-test-tool cleanup --state {scenario.state.path}")
                elif not scenario.state.is_empty():
                    if not scenario.admin.logged_in:
                        await scenario.admin.login()
                    if not await cleanup(scenario.admin, scenario.state, protocol, args.create_batch):
                        exit_code = exit_code or 3
            except (asyncio.CancelledError, Exception) as err:  # noqa: BLE001
                protocol.error(
                    f"Cleanup fehlgeschlagen ({type(err).__name__}: {err}). "
                    f"Nachholen mit: openslides-test-tool cleanup --state {scenario.state.path}"
                )
                exit_code = exit_code or 3
            protocol.write_summary()
            protocol.print_summary()
            protocol.close()
    return exit_code


async def run_cleanup(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> int:
    state_path = Path(args.state)
    state = State.load(state_path)
    protocol = Protocol(state_path.parent, quiet=args.quiet)
    base_url = args.base_url
    async with make_http_client(
        base_url, verify_tls=args.verify_tls, timeout=args.timeout, force_ipv4=args.ipv4, transport=transport
    ) as http:
        admin = OSClient(http, args.username, args.password)
        try:
            await admin.login()
            ok = await cleanup(admin, state, protocol)
        finally:
            protocol.close()
    return 0 if ok else 3


async def run_cleanup_users(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Deletes every user named <prefix><number> in the organization."""
    protocol = Protocol(Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}_cleanup_users", quiet=args.quiet)
    pattern = re.compile(rf"^{re.escape(args.user_prefix)}\d+$")
    ok = True
    async with make_http_client(
        args.base_url, verify_tls=args.verify_tls, timeout=args.timeout, force_ipv4=args.ipv4, transport=transport
    ) as http:
        admin = OSClient(http, args.username, args.password)
        try:
            with protocol.step("admin_login", username=args.username):
                await admin.login()
            with protocol.step("find_users", prefix=args.user_prefix) as step:
                data = await admin.get(
                    [
                        {
                            "collection": "organization",
                            "ids": [1],
                            "fields": {
                                "user_ids": {"type": "relation-list", "collection": "user", "fields": {"username": None}}
                            },
                        }
                    ]
                )
                users = sorted(
                    uid
                    for uid in data.get("organization/1/user_ids") or []
                    if pattern.match(str(data.get(f"user/{uid}/username", "")))
                )
                step.details["found"] = len(users)
            if users:
                with protocol.step("cleanup_users", count=len(users)):
                    ok = await delete_users(admin, users, protocol, args.create_batch)
                if ok:
                    protocol.log(f"{len(users)} User mit Prefix {args.user_prefix!r} gelöscht.")
                else:
                    protocol.warn("Nicht alle User konnten gelöscht werden.")
            else:
                protocol.log(f"Keine User mit Prefix {args.user_prefix!r} gefunden.")
        except OpenSlidesError as err:
            protocol.error(f"Fehler: {err}")
            ok = False
        finally:
            protocol.close()
    return 0 if ok else 3


async def run_check(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Only login + preflight, nothing gets created (except the optional electronic voting switch)."""
    args.enable_electronic_voting = False
    protocol = Protocol(Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}_check", quiet=args.quiet)
    async with make_http_client(
        args.base_url, verify_tls=args.verify_tls, timeout=args.timeout, force_ipv4=args.ipv4, transport=transport
    ) as http:
        scenario = Scenario(args, protocol, http)
        try:
            await scenario.preflight()
            protocol.log("Check erfolgreich – das Ziel ist bereit für den Test.")
            return 0
        except OpenSlidesError as err:
            protocol.error(f"Check fehlgeschlagen: {err}")
            return 1
        finally:
            scenario.state.path.unlink(missing_ok=True)
            protocol.close()
