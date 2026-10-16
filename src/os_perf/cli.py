"""Command line interface.

The global options are compatible with openslides-performance
(-d/--domain, -u/--username, -p/--password, --http, -4).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import signal
import sys
from datetime import datetime

from . import __version__

TARGETS = ("motion", "assignment")


def _targets(value: str) -> list[str]:
    targets = [t.strip() for t in value.split(",") if t.strip()]
    aliases = {"m": "motion", "antrag": "motion", "a": "assignment", "wahl": "assignment", "election": "assignment"}
    targets = [aliases.get(t, t) for t in targets]
    for t in targets:
        if t not in TARGETS:
            raise argparse.ArgumentTypeError(f"unknown target {t!r}, use motion and/or assignment")
    return targets


def _common(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("connection (like openslides-performance)")
    g.add_argument("-d", "--domain", default="localhost:8000", help="Domain of the OpenSlides server (default: %(default)s)")
    g.add_argument("-u", "--username", default="superadmin", help="Superuser name (default: %(default)s)")
    g.add_argument(
        "-p",
        "--password",
        default=None,
        help="Superuser password. Default: env OS_PERF_PASSWORD, otherwise prompted",
    )
    g.add_argument("--http", action="store_true", help="Use http instead of https")
    g.add_argument("-4", "--ipv4", action="store_true", help="Force IPv4")
    g.add_argument("--verify-tls", action="store_true", help="Verify TLS certificates (default: off, like openslides-performance)")
    g.add_argument("--timeout", type=float, default=120.0, help="Timeout per request in seconds (default: %(default)s)")
    g.add_argument("-q", "--quiet", action="store_true", help="Only print warnings, errors and the summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="os-perf",
        description="Performance test for OpenSlides 4 polls (elections and motion votes).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------ run
    run = sub.add_parser(
        "run",
        help="Run the poll load test",
        description="Creates users and polls, lets all users vote, stops the polls and measures everything. "
        "All created data is deleted at the end (also on Ctrl+C).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _common(run)
    g = run.add_argument_group("scenario")
    g.add_argument("-m", "--meeting", type=int, required=True, help="ID of the meeting to test in")
    g.add_argument("-n", "--amount", type=int, default=10, help="Number of users (voters) to create")
    g.add_argument("--users-password", default="pass", help="Password of all created users")
    g.add_argument(
        "--user-prefix",
        default=None,
        help="Username prefix of the created users (default: perf<HHMMSS>_, so runs never collide)",
    )
    g.add_argument("--group-id", type=int, default=0, help="Group for the users (default: the meeting's Delegates group)")
    g.add_argument(
        "-t",
        "--target",
        dest="targets",
        type=_targets,
        default=["assignment"],
        help="What to test: assignment (Wahl), motion (Antrag) or both comma separated (distributed over the parallel polls)",
    )
    g.add_argument("-P", "--parallel", type=int, default=1, help="Number of polls running at the same time")
    g.add_argument("--candidates", type=int, default=3, help="Number of candidates for elections (taken from the test users)")
    g.add_argument(
        "--poll-type",
        choices=("secret", "named", "open"),
        default="secret",
        help="secret = pseudoanonymous (classic) / secret (new); named; open (new API only)",
    )
    g.add_argument("--classic-backend", choices=("fast", "long"), default=None, help="poll/backend for the classic API")
    g.add_argument("--live-voting", action="store_true", help="Enable live voting (only named polls)")
    g.add_argument("--vote-choice", default="random", choices=("random", "yes", "no", "abstain", "first"), help="Ballot to send")
    g.add_argument("--publish", action="store_true", help="Publish the polls when stopping them")
    g.add_argument("--api", choices=("auto", "classic", "new"), default="auto", help="Poll API: classic (4.0-4.3) or new vote service")
    g.add_argument("--enable-electronic-voting", action="store_true", help="Enable electronic voting if disabled (reset afterwards)")
    g.add_argument("--seed", type=int, default=None, help="Random seed for reproducible ballots")

    g = run.add_argument_group("flow")
    g.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="Number of rounds. 0 = interactive until 'q' (with --no-wait: 1 round)",
    )
    g.add_argument("--no-wait", dest="wait", action="store_false", help="Do not wait for ENTER before starting the polls")
    g.add_argument("--wait-before-stop", action="store_true", help="Also wait for ENTER before stopping the polls")
    g.add_argument("--keep", action="store_true", help="Do not delete the created data at the end")
    g.add_argument("--result-timeout", type=float, default=300.0, help="Seconds to wait for a poll state/result to become visible")
    g.add_argument("--out", default="runs", help="Directory for the protocols")

    g = run.add_argument_group("load")
    g.add_argument("--vote-concurrency", type=int, default=0, help="Max. simultaneous vote requests (0 = all at once)")
    g.add_argument("--vote-spread", type=float, default=0.0, help="Spread the votes randomly over this many seconds")
    g.add_argument("--login-concurrency", type=int, default=50, help="Max. simultaneous logins")
    g.add_argument("--create-batch", type=int, default=100, help="Users per user.create request")
    g.add_argument("--create-concurrency", type=int, default=4, help="Simultaneous user.create requests")
    g.add_argument("--max-connections", type=int, default=1000, help="Max. open HTTP connections")
    run.set_defaults(func="run")

    # ---------------------------------------------------------------- check
    check = sub.add_parser("check", help="Only check login, meeting, group, electronic voting and the poll API")
    _common(check)
    check.add_argument("-m", "--meeting", type=int, required=True, help="ID of the meeting")
    check.add_argument("--group-id", type=int, default=0, help="Group for the users")
    check.add_argument("--api", choices=("auto", "classic", "new"), default="auto")
    check.add_argument("--out", default="runs")
    check.set_defaults(func="check", enable_electronic_voting=False, seed=None)

    # -------------------------------------------------------------- cleanup
    clean = sub.add_parser("cleanup", help="Delete leftovers of an aborted run using its state.json")
    _common(clean)
    clean.add_argument("--state", required=True, help="Path to state.json of the run")
    clean.set_defaults(func="cleanup")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.password is None:
        args.password = os.environ.get("OS_PERF_PASSWORD")
    if args.password is None:
        args.password = getpass.getpass(f"Password for {args.username}: ")
    proto = "http" if args.http else "https"
    args.base_url = args.domain if args.domain.startswith("http") else f"{proto}://{args.domain}"

    if args.func == "run":
        if args.user_prefix is None:
            args.user_prefix = f"perf{datetime.now():%H%M%S}_"
        if args.amount < 1 or args.parallel < 1:
            parser.error("--amount and --parallel must be >= 1")
        if "assignment" in args.targets and args.candidates < 1:
            parser.error("--candidates must be >= 1")
        args.candidates = min(args.candidates, args.amount)
        if args.poll_type == "open" and args.api == "classic":
            args.poll_type = "named"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from . import scenario

    entry = {"run": scenario.run, "check": scenario.run_check, "cleanup": scenario.run_cleanup}[args.func]

    async def runner() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)  # type: ignore[union-attr]
        except (NotImplementedError, RuntimeError):
            pass
        return await entry(args)

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        print("\nHart abgebrochen. Ggf. 'os-perf cleanup --state <run>/state.json' ausführen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
