import asyncio
import json
from pathlib import Path

import httpx
import pytest

from openslides_test_tool import scenario
from openslides_test_tool.cli import parse_args

from .fake_openslides import FakeOpenSlides


def _args(tmp_path: Path, *extra: str):
    return parse_args(
        ["run", "-d", "http://fake", "-u", "superadmin", "-p", "secret", "-m", "1", "--no-wait", "-q", "--out", str(tmp_path), *extra]
    )


def _run(fake: FakeOpenSlides, args) -> int:
    async def main():
        return await scenario.run(args, transport=httpx.MockTransport(fake.handler))

    return asyncio.run(main())


def _summary(tmp_path: Path) -> dict:
    (run_dir,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    return json.loads((run_dir / "summary.json").read_text())


def _assert_clean(fake: FakeOpenSlides):
    assert set(fake.users) == {"superadmin"}
    for collection in ("poll", "motion", "assignment", "meeting_user", "option"):
        assert fake.ids(collection) == set(), collection


@pytest.mark.parametrize("api", ["classic", "new"])
def test_parallel_mixed_polls(tmp_path, api):
    fake = FakeOpenSlides(api=api)
    args = _args(tmp_path, "-n", "25", "-P", "3", "-t", "motion,assignment", "--seed", "1", "--publish")
    assert _run(fake, args) == 0

    summary = _summary(tmp_path)
    assert summary["meta"]["api"] == api  # auto detection
    validation = summary["meta"]["validation"]
    assert len(validation) == 3 and all(v["ok"] for v in validation)
    assert all(v["accepted_ballots"] == 25 for v in validation)
    assert fake.vote_requests == 75
    steps = {s["name"] for s in summary["steps"]}
    assert {"create_users", "login_users", "start_polls", "voting", "stop_and_results", "cleanup_users"} <= steps
    _assert_clean(fake)


def test_rounds_and_electronic_voting_restored(tmp_path):
    fake = FakeOpenSlides(api="classic", electronic_voting=False)
    args = _args(tmp_path, "-n", "5", "--rounds", "2", "--enable-electronic-voting", "--vote-spread", "0.05")
    assert _run(fake, args) == 0
    assert len(_summary(tmp_path)["meta"]["validation"]) == 2
    assert fake.db["organization/1/enable_electronic_voting"] is False
    _assert_clean(fake)


def test_electronic_voting_disabled_aborts_cleanly(tmp_path):
    fake = FakeOpenSlides(api="classic", electronic_voting=False)
    assert _run(fake, _args(tmp_path, "-n", "3")) == 1
    _assert_clean(fake)


def test_cleanup_after_failure(tmp_path):
    fake = FakeOpenSlides(api="classic")

    def broken_start(item):
        raise __import__("tests.fake_openslides", fromlist=["ActionError"]).ActionError("boom")

    fake.a_poll_start = broken_start
    assert _run(fake, _args(tmp_path, "-n", "4", "-P", "2")) == 1
    _assert_clean(fake)


def test_keep_and_cleanup_command(tmp_path):
    fake = FakeOpenSlides(api="new")
    assert _run(fake, _args(tmp_path, "-n", "4", "--keep")) == 0
    assert len(fake.users) == 5
    (run_dir,) = [p for p in tmp_path.iterdir() if p.is_dir()]

    args = parse_args(["cleanup", "-d", "http://fake", "-p", "secret", "-q", "--state", str(run_dir / "state.json")])

    async def main():
        return await scenario.run_cleanup(args, transport=httpx.MockTransport(fake.handler))

    assert asyncio.run(main()) == 0
    _assert_clean(fake)


# ------------------------------------------------------------ persistent users


def _steps(tmp_path: Path) -> dict:
    """Step name -> details of the newest run directory."""
    run_dir = max(p for p in tmp_path.iterdir() if p.is_dir())
    summary = json.loads((run_dir / "summary.json").read_text())
    return {s["name"]: s.get("details", {}) for s in summary["steps"]}


def _cleanup_users(fake: FakeOpenSlides, tmp_path: Path, prefix: str) -> int:
    args = parse_args(
        ["cleanup-users", "-d", "http://fake", "-p", "secret", "-q", "--out", str(tmp_path), "--user-prefix", prefix]
    )

    async def main():
        return await scenario.run_cleanup_users(args, transport=httpx.MockTransport(fake.handler))

    return asyncio.run(main())


@pytest.mark.parametrize("api", ["classic", "new"])
def test_persistent_users_reused(tmp_path, api):
    fake = FakeOpenSlides(api=api)
    assert _run(fake, _args(tmp_path, "-n", "10", "--persistent-users")) == 0
    steps = _steps(tmp_path)
    assert (steps["find_users"]["found"], steps["find_users"]["missing"]) == (0, 10)
    assert steps["create_users"]["created"] == 10
    assert "attach_users" not in steps
    # users survive, everything else is gone
    assert len(fake.users) == 11 and all(u.startswith("perf_") for u in fake.users if u != "superadmin")
    for collection in ("poll", "motion", "assignment", "option"):
        assert fake.ids(collection) == set(), collection
    fake.action_log.clear()

    # second run needs more users: only the missing 5 are created
    assert _run(fake, _args(tmp_path, "-n", "15", "--persistent-users", "-P", "2")) == 0
    steps = _steps(tmp_path)
    assert (steps["find_users"]["found"], steps["find_users"]["missing"]) == (10, 5)
    assert steps["create_users"]["created"] == 5
    assert steps["login_users"]["logged_in"] == 15
    assert fake.action_log.count("user.create") == 1
    assert "user.delete" not in fake.action_log
    assert len(fake.users) == 16
    validation = _summary_of(tmp_path)["meta"]["validation"]
    assert len(validation) == 2 and all(v["ok"] and v["accepted_ballots"] == 15 for v in validation)

    # third run with fewer users creates nothing and uses the first ones
    assert _run(fake, _args(tmp_path, "-n", "3", "--persistent-users")) == 0
    steps = _steps(tmp_path)
    assert steps["create_users"]["created"] == 0
    assert steps["login_users"]["logged_in"] == 3
    assert len(fake.users) == 16


def _summary_of(tmp_path: Path) -> dict:
    run_dir = max(p for p in tmp_path.iterdir() if p.is_dir())
    return json.loads((run_dir / "summary.json").read_text())


def test_persistent_users_password_reset(tmp_path):
    fake = FakeOpenSlides(api="classic")
    assert _run(fake, _args(tmp_path, "-n", "6", "--persistent-users")) == 0
    for name in ("perf_2", "perf_5"):
        uid, _ = fake.users[name]
        fake.users[name] = (uid, "something-else")

    assert _run(fake, _args(tmp_path, "-n", "6", "--persistent-users")) == 0
    steps = _steps(tmp_path)
    assert steps["login_users"]["logged_in"] == 6
    assert fake.action_log.count("user.set_password") == 1
    assert fake.users["perf_2"][1] == "pass" and fake.users["perf_5"][1] == "pass"
    assert _summary_of(tmp_path)["meta"]["validation"][0]["accepted_ballots"] == 6


def test_persistent_user_outside_meeting_is_attached(tmp_path):
    fake = FakeOpenSlides(api="new")
    fake.add_user("perf_2", "pass")  # exists in the organization, not in the meeting
    uid = fake.add_user("perf_3", "pass", meeting_id=1, group_ids=[1], present=False)  # wrong group, absent
    fake.db[f"user/{uid}/is_active"] = False

    assert _run(fake, _args(tmp_path, "-n", "4", "--persistent-users", "--candidates", "4")) == 0
    steps = _steps(tmp_path)
    assert (steps["find_users"]["found"], steps["find_users"]["missing"]) == (2, 2)
    assert steps["attach_users"]["updated"] == 2
    assert steps["create_users"]["created"] == 2
    assert steps["login_users"]["logged_in"] == 4
    assert fake.action_log.count("user.create") == 1
    for name in ("perf_2", "perf_3"):
        uid, _ = fake.users[name]
        (mu,) = fake.db[f"user/{uid}/meeting_user_ids"]
        assert 2 in fake.db[f"meeting_user/{mu}/group_ids"]
        assert fake.db[f"user/{uid}/is_present_in_meeting_ids"] == [1]
        assert fake.db[f"user/{uid}/is_active"] is True
    assert fake.db[f"meeting_user/{mu}/group_ids"] == [1, 2]  # existing group kept
    assert _summary_of(tmp_path)["meta"]["validation"][0]["accepted_ballots"] == 4


def test_cleanup_users_command(tmp_path):
    fake = FakeOpenSlides(api="classic")
    fake.add_user("perf_x", "pass", meeting_id=1)  # no number suffix: must survive
    fake.add_user("other_1", "pass", meeting_id=1)
    assert _run(fake, _args(tmp_path, "-n", "7", "--persistent-users")) == 0
    assert len(fake.users) == 10

    assert _cleanup_users(fake, tmp_path, "perf_") == 0
    assert set(fake.users) == {"superadmin", "perf_x", "other_1"}
    assert fake.ids("meeting_user") == {
        mu for name in ("perf_x", "other_1") for mu in fake.db[f"user/{fake.users[name][0]}/meeting_user_ids"]
    }
    assert "user.delete" in fake.action_log

    # nothing left: still fine
    assert _cleanup_users(fake, tmp_path, "perf_") == 0


def test_without_flag_users_are_still_deleted(tmp_path):
    fake = FakeOpenSlides(api="classic")
    assert _run(fake, _args(tmp_path, "-n", "3")) == 0
    assert "presenter:search_users" not in fake.action_log
    _assert_clean(fake)
