"""Timing and protocol writing.

Each run gets its own directory with:

- protocol.log   human readable log (also printed to the console)
- events.jsonl   one JSON object per measured step
- requests.csv   one line per single request (logins, votes, ...)
- summary.json   all steps and latency statistics at the end
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    if seconds < 120:
        return f"{seconds:.3f} s"
    return f"{seconds / 60:.1f} min"


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    lower, upper = math.floor(k), math.ceil(k)
    if lower == upper:
        return ordered[int(k)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def latency_stats(latencies: list[float], errors: int, wall: float | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "count": len(latencies) + errors,
        "ok": len(latencies),
        "errors": errors,
    }
    if latencies:
        stats.update(
            min=min(latencies),
            mean=statistics.fmean(latencies),
            p50=percentile(latencies, 50),
            p90=percentile(latencies, 90),
            p95=percentile(latencies, 95),
            p99=percentile(latencies, 99),
            max=max(latencies),
        )
    if wall:
        stats["wall_s"] = wall
        stats["throughput_per_s"] = len(latencies) / wall if wall > 0 else None
    return stats


@dataclass
class Step:
    name: str
    started_at: str
    duration_s: float | None = None
    ok: bool = True
    details: dict[str, Any] = field(default_factory=dict)


class Protocol:
    def __init__(self, run_dir: Path, quiet: bool = False):
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.quiet = quiet
        self.t0 = time.perf_counter()
        self.steps: list[Step] = []
        self.stats: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, Any] = {}
        self._log = open(run_dir / "protocol.log", "a", encoding="utf-8")
        self._events = open(run_dir / "events.jsonl", "a", encoding="utf-8")
        self._requests_file = open(run_dir / "requests.csv", "a", encoding="utf-8", newline="")
        self._requests = csv.writer(self._requests_file)
        self._requests.writerow(
            ["kind", "round", "poll_id", "user", "t_send_s", "latency_s", "ok", "status", "error"]
        )

    # ---------------------------------------------------------------- output

    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def log(self, message: str, level: str = "INFO") -> None:
        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{now} [+{self.elapsed():9.3f}s] {level:5} {message}"
        self._log.write(line + "\n")
        self._log.flush()
        if not self.quiet or level in ("WARN", "ERROR"):
            print(line, file=sys.stderr if level in ("WARN", "ERROR") else sys.stdout, flush=True)

    def warn(self, message: str) -> None:
        self.log(message, "WARN")

    def error(self, message: str) -> None:
        self.log(message, "ERROR")

    def event(self, kind: str, **data: Any) -> None:
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "t_s": round(self.elapsed(), 6), "kind": kind, **data}
        self._events.write(json.dumps(record, default=str) + "\n")
        self._events.flush()

    # ----------------------------------------------------------------- steps

    @contextmanager
    def step(self, name: str, **details: Any) -> Iterator[Step]:
        """Measures the wall time of a block and records it as a step."""
        step = Step(name=name, started_at=datetime.now().isoformat(timespec="milliseconds"), details=dict(details))
        self.log(f"▶ {name} {self._fmt_details(details)}".rstrip())
        start = time.perf_counter()
        try:
            yield step
        except BaseException as err:
            step.ok = False
            step.details.setdefault("error", f"{type(err).__name__}: {err}")
            raise
        finally:
            step.duration_s = time.perf_counter() - start
            self.steps.append(step)
            self.event("step", name=name, duration_s=step.duration_s, ok=step.ok, **step.details)
            status = "✔" if step.ok else "✘"
            self.log(
                f"{status} {name}: {_fmt_duration(step.duration_s)} {self._fmt_details(step.details, skip=details)}".rstrip(),
                "INFO" if step.ok else "ERROR",
            )

    def record_step(self, name: str, duration_s: float | None, ok: bool = True, **details: Any) -> None:
        """Records a step whose duration was measured elsewhere."""
        step = Step(name=name, started_at=datetime.now().isoformat(timespec="milliseconds"), duration_s=duration_s, ok=ok, details=details)
        self.steps.append(step)
        self.event("step", name=name, duration_s=duration_s, ok=ok, **details)
        self.log(f"{'✔' if ok else '✘'} {name}: {_fmt_duration(duration_s)} {self._fmt_details(details)}".rstrip(), "INFO" if ok else "ERROR")

    @staticmethod
    def _fmt_details(details: dict[str, Any], skip: dict[str, Any] | None = None) -> str:
        parts = []
        for key, value in details.items():
            if skip and key in skip and skip[key] == value:
                continue
            if isinstance(value, float):
                value = f"{value:.4f}"
            parts.append(f"{key}={value}")
        return ("(" + ", ".join(parts) + ")") if parts else ""

    # -------------------------------------------------------------- requests

    def request(
        self,
        kind: str,
        *,
        round_: int | None,
        poll_id: int | None,
        user: str,
        t_send: float,
        latency: float,
        ok: bool,
        status: int | None,
        error: str = "",
    ) -> None:
        self._requests.writerow(
            [kind, round_ or "", poll_id or "", user, f"{t_send - self.t0:.6f}", f"{latency:.6f}", int(ok), status or "", error[:300]]
        )

    def add_stats(self, name: str, stats: dict[str, Any]) -> None:
        self.stats[name] = stats
        self.event("stats", name=name, **stats)
        if stats.get("ok"):
            self.log(
                f"  {name}: {stats['ok']}/{stats['count']} ok, errors={stats['errors']}, "
                f"p50={_fmt_duration(stats.get('p50'))}, p95={_fmt_duration(stats.get('p95'))}, "
                f"p99={_fmt_duration(stats.get('p99'))}, max={_fmt_duration(stats.get('max'))}"
                + (f", {stats['throughput_per_s']:.1f}/s" if stats.get("throughput_per_s") else "")
            )
        else:
            self.log(f"  {name}: 0/{stats['count']} ok, errors={stats['errors']}", "WARN" if stats["count"] else "INFO")

    # --------------------------------------------------------------- summary

    def write_summary(self) -> None:
        summary = {
            "meta": self.meta,
            "total_s": self.elapsed(),
            "steps": [step.__dict__ for step in self.steps],
            "stats": self.stats,
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    def print_summary(self) -> None:
        lines = ["", "=" * 78, "ZUSAMMENFASSUNG", "=" * 78]
        width = max((len(s.name) for s in self.steps), default=10)
        for step in self.steps:
            mark = "ok " if step.ok else "ERR"
            lines.append(f"{mark} {step.name:<{width}}  {_fmt_duration(step.duration_s):>12}")
        if self.stats:
            lines.append("-" * 78)
            lines.append(f"{'Requests':<28}{'ok/total':>11}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}")
            for name, s in self.stats.items():
                lines.append(
                    f"{name[:28]:<28}{str(s['ok']) + '/' + str(s['count']):>11}"
                    f"{_fmt_duration(s.get('p50')):>10}{_fmt_duration(s.get('p95')):>10}"
                    f"{_fmt_duration(s.get('p99')):>10}{_fmt_duration(s.get('max')):>10}"
                )
        lines.append("-" * 78)
        lines.append(f"Gesamtlaufzeit: {_fmt_duration(self.elapsed())}")
        lines.append(f"Protokoll:      {self.run_dir}")
        text = "\n".join(lines)
        print(text, flush=True)
        self._log.write(text + "\n")
        self._log.flush()

    def close(self) -> None:
        for handle in (self._log, self._events, self._requests_file):
            try:
                handle.close()
            except OSError:
                pass
