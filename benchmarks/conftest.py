"""Measure, compare against a recorded baseline, and fail on regression.

A benchmark that prints numbers gets ignored within two months. The mechanism
here is the whole point: each measurement is compared against a committed
baseline and a tolerance, and the test fails when it regresses past it. That
turns a report into a test, which is the only kind of artefact a build protects.

`--benchmark-record` rewrites the baselines. It is a deliberate act, taken when
a change is meant to move a number, and the diff it produces is the thing a
reviewer looks at.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BASELINES = Path(__file__).parent / "baselines.json"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--benchmark-record",
        action="store_true",
        help="rewrite baselines.json from this run instead of comparing against it",
    )


def environment() -> dict[str, Any]:
    """The machine shape, recorded beside the numbers.

    A benchmark without its hardware is a number people quote back at you
    wrongly, so the baselines carry the box they came from.
    """
    return {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "cpus": os.cpu_count(),
    }


@dataclass
class Measurement:
    name: str
    value: float
    unit: str
    higher_is_better: bool


class Recorder:
    """Collects this run's measurements so the session hook can compare them."""

    def __init__(self) -> None:
        self.taken: dict[str, Measurement] = {}

    def record(self, name: str, value: float, *, unit: str, higher_is_better: bool) -> Measurement:
        measurement = Measurement(name, value, unit, higher_is_better)
        self.taken[name] = measurement
        return measurement


@pytest.fixture(scope="session")
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture(scope="session")
def baselines() -> dict[str, Any]:
    if not BASELINES.is_file():
        return {"environment": environment(), "measurements": {}}
    loaded: dict[str, Any] = json.loads(BASELINES.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def benchmark(
    request: pytest.FixtureRequest, recorder: Recorder, baselines: dict[str, Any]
) -> Callable[..., None]:
    """Assert one measurement against its baseline, or record it.

    Tolerances are per measurement rather than global, because throughput and
    latency degrade differently and a single number would be wrong for one of
    them.
    """
    recording = bool(request.config.getoption("--benchmark-record"))

    def check(
        name: str,
        value: float,
        *,
        unit: str,
        higher_is_better: bool,
        tolerance: float,
    ) -> None:
        recorder.record(name, value, unit=unit, higher_is_better=higher_is_better)
        entry = baselines.get("measurements", {}).get(name)

        print(f"\n  {name}: {value:.4g} {unit}")

        if recording or entry is None:
            if entry is None and not recording:
                pytest.skip(
                    f"no baseline for {name}. Run with --benchmark-record to set one, "
                    "on a machine whose shape you are willing to publish."
                )
            return

        baseline = float(entry["value"])
        limit = baseline * (1 - tolerance) if higher_is_better else baseline * (1 + tolerance)
        regressed = value < limit if higher_is_better else value > limit

        assert not regressed, (
            f"{name} regressed: {value:.4g} {unit} against a baseline of "
            f"{baseline:.4g} {unit} with a {tolerance:.0%} tolerance.\n"
            f"Baselines were measured on {baselines.get('environment')}; this run is on "
            f"{environment()}. If the machines differ, that is the first thing to rule out.\n"
            f"If the change is deliberate, rerun with --benchmark-record and commit the diff."
        )

    return check


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the baselines when asked, once, with the environment attached."""
    _ = exitstatus
    if not session.config.getoption("--benchmark-record", default=False):
        return
    recorder: Recorder | None = getattr(session, "_psych_recorder", None)
    if recorder is None or not recorder.taken:
        return

    BASELINES.write_text(
        json.dumps(
            {
                "environment": environment(),
                "note": (
                    "Recorded by `pytest benchmarks --benchmark-record`. These numbers "
                    "belong to the environment above; see benchmarks/README.md."
                ),
                "measurements": {
                    name: {
                        "value": round(m.value, 6),
                        "unit": m.unit,
                        "higher_is_better": m.higher_is_better,
                    }
                    for name, m in sorted(recorder.taken.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="session", autouse=True)
def _expose_recorder(request: pytest.FixtureRequest, recorder: Recorder) -> None:
    """Hand the session the recorder, so the finish hook can write baselines."""
    request.session._psych_recorder = recorder  # type: ignore[attr-defined]


def median_of(samples: list[float]) -> float:
    """The middle sample. A mean on a shared runner is one noisy neighbour away
    from being a different number."""
    return statistics.median(samples)


class Stopwatch:
    """Monotonic timing, so a clock adjustment cannot produce a negative duration."""

    def __enter__(self) -> Stopwatch:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self.started
