import asyncio
import json
from pathlib import Path

import httpx
import pytest

from os_perf import scenario
from os_perf.cli import parse_args

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
