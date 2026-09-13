"""Pure checks for translating execution limits to POSIX uid limits."""

from __future__ import annotations

import pytest

from psych_runtime.sandbox import subprocess as sandbox_subprocess


def test_nproc_ceiling_includes_the_existing_uid_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_subprocess, "_uid_task_count", lambda _uid: 37)

    assert sandbox_subprocess._nproc_ceiling(1000, 16) == 53


def test_nproc_ceiling_remains_conservative_when_the_baseline_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_subprocess, "_uid_task_count", lambda _uid: None)

    assert sandbox_subprocess._nproc_ceiling(1000, 16) == 16
