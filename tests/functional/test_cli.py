"""The CLI, exercised the way a new developer meets it.

Two of these run generated code as a subprocess, which is what makes them
functional rather than unit tests. That is deliberate and it is the only way
this file is worth having: a scaffold is the first thing somebody runs, and a
test that only checked the files were written would pass while `python main.py`
raised on line one.

No network. The templates fall back to `psych_runtime.testing.fake_model` when
`OPENAI_API_KEY` is unset, and these clear it from the child's environment so a
developer's own key on the machine running the suite cannot turn a template
test into a billed provider call.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from psych_runtime.cli import main
from psych_runtime.templates import SCRIPT_TEMPLATES, TEMPLATES

pytestmark = pytest.mark.functional

REPO = Path(__file__).resolve().parents[2]


def run_template(project: Path) -> subprocess.CompletedProcess[str]:
    """Execute a generated project's ``main.py`` with no provider configured."""
    environment = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    environment["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "main.py"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=environment,
    )


class TestEveryTemplateIsCovered:
    def test_no_template_ships_without_a_test(self) -> None:
        """A scaffold nobody exercises is one nobody notices breaking.

        `SCRIPT_TEMPLATES` are run as subprocesses by the class below; anything
        else needs its own test, and adding a template without one should fail
        here rather than six months later in somebody's terminal.
        """
        exercised = set(SCRIPT_TEMPLATES) | {"fastapi"}
        assert set(TEMPLATES) == exercised, (
            f"these templates have no test: {sorted(set(TEMPLATES) - exercised)}"
        )


class TestNewScaffoldsSomethingThatRuns:
    @pytest.mark.parametrize("template", SCRIPT_TEMPLATES)
    def test_the_generated_project_runs_with_no_api_key(
        self, template: str, tmp_path: Path
    ) -> None:
        """The promise the README makes, kept for every template.

        `psych new` then `python main.py` has to produce an answer on a machine
        with no key, no database and no Docker, or the on-ramp this command
        exists for does not exist.
        """
        project = tmp_path / "demo"
        assert main(["new", str(project), "--template", template]) == 0

        result = run_template(project)
        assert result.returncode == 0, (
            f"the {template} template failed to run.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "fake model" in result.stdout, "it should say why it is not calling a provider"
        assert "A1" in result.stdout

    def test_the_tour_reaches_every_feature_it_advertises(self, tmp_path: Path) -> None:
        """Its README lists six things, so its output has to show six things.

        Asserting on the output rather than the exit code is the point: an
        approval that silently never fires still exits 0, and that exact bug is
        what this test was written after finding.
        """
        project = tmp_path / "tour"
        assert main(["new", str(project), "--template", "tour"]) == 0

        stdout = run_template(project).stdout
        assert "waiting on approval for: issue_refund" in stdout, "the approval never suspended"
        assert "load_skill" in stdout, "the skill was never loaded on demand"
        assert "remembered: prefers email over SMS" in stdout, "memory did not survive the Run"
        assert "step verify (tool) completed=True" in stdout, "the workflow step did not complete"
        assert "cost:     None" in stdout, "an unknown price must read None, never 0"

    def test_a_project_name_reaches_the_generated_files(self, tmp_path: Path) -> None:
        assert main(["new", str(tmp_path / "acme-support")]) == 0
        readme = (tmp_path / "acme-support" / "README.md").read_text(encoding="utf-8")
        assert readme.startswith("# acme-support")
        assert "__PROJECT__" not in readme

    def test_it_refuses_a_non_empty_directory_without_force(self, tmp_path: Path) -> None:
        """Overwriting somebody's work silently is worse than making them ask."""
        project = tmp_path / "occupied"
        project.mkdir()
        (project / "main.py").write_text("print('mine')", encoding="utf-8")

        assert main(["new", str(project)]) == 1
        assert (project / "main.py").read_text(encoding="utf-8") == "print('mine')"

        assert main(["new", str(project), "--force"]) == 0
        assert "psych" in (project / "main.py").read_text(encoding="utf-8")


class TestSkillsInstall:
    def test_it_copies_every_skill_and_says_how_many(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        destination = tmp_path / ".agents" / "skills"
        assert main(["skills", "install", "--dest", str(destination)]) == 0

        installed = sorted(p.name for p in destination.iterdir() if (p / "SKILL.md").is_file())
        available = sorted(p.parent.name for p in (REPO / ".agents" / "skills").glob("*/SKILL.md"))
        assert installed == available
        assert f"installed {len(available)} skills" in capsys.readouterr().out

    def test_installing_twice_changes_nothing_and_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Idempotent, because somebody will run it again and should not lose edits."""
        destination = tmp_path / "skills"
        main(["skills", "install", "--dest", str(destination)])
        capsys.readouterr()

        edited = destination / "psych" / "SKILL.md"
        edited.write_text("# edited locally\n", encoding="utf-8")

        assert main(["skills", "install", "--dest", str(destination)]) == 0
        assert "skipped" in capsys.readouterr().out
        assert edited.read_text(encoding="utf-8") == "# edited locally\n"

        assert main(["skills", "install", "--dest", str(destination), "--force"]) == 0
        assert edited.read_text(encoding="utf-8") != "# edited locally\n"

    def test_list_names_every_skill(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["skills", "list"]) == 0
        out = capsys.readouterr().out
        for skill in (REPO / ".agents" / "skills").glob("*/SKILL.md"):
            assert skill.parent.name in out


class TestDoctorReportsWithoutReaching:
    def test_it_names_the_version_the_adapters_and_the_skills(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "psych 0" in out
        assert "asyncpg" in out
        assert "OPENAI_API_KEY" in out
        assert "Agent skills" in out

    def test_it_promises_to_open_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A doctor that hangs on an unreachable DSN is worse than one that does not try."""
        main(["doctor"])
        assert "opened a socket or a database connection" in capsys.readouterr().out


class TestTheParserItself:
    def test_no_arguments_prints_help_rather_than_failing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == 0
        assert "usage: psych" in capsys.readouterr().out

    def test_skills_with_no_subcommand_prints_its_own_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["skills"]) == 0
        out = capsys.readouterr().out
        assert "install" in out
        assert "list" in out


class TestTheFastapiTemplateServesRuns:
    """The integration every consumer writes first, driven through its own routes.

    `main.py` starts a server and blocks, so this drives the ASGI app directly
    rather than running it as a subprocess. That is not a weaker test: it
    exercises the same handlers a request would, and it can assert on the
    tenant check and the idempotency key, which are the two things in this
    template that are easy to write and easy to get subtly wrong.
    """

    @pytest.fixture(autouse=True)
    def offline_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The in-process template must not inherit a developer's provider key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("PSYCH_POSTGRES_DSN", raising=False)

    @staticmethod
    def generate(tmp_path: Path) -> Path:
        project = tmp_path / "api"
        assert main(["new", str(project), "--template", "fastapi"]) == 0
        return project

    @staticmethod
    def client(project: Path) -> Any:
        """Import the generated app from its own directory, as uvicorn would."""
        from fastapi.testclient import TestClient

        sys.path.insert(0, str(project))
        for module in ("main", "agent", "store"):
            sys.modules.pop(module, None)
        try:
            generated = importlib.import_module("main")
            return TestClient(generated.app)
        finally:
            sys.path.remove(str(project))

    def test_it_admits_a_run_executes_it_and_reads_it_back(self, tmp_path: Path) -> None:
        with self.client(self.generate(tmp_path)) as client:
            started = client.post("/runs", json={"message": "where is order A1?"})
            assert started.status_code == 200
            run_id = started.json()["run_id"]

            deadline = time.monotonic() + 60
            status = client.get(f"/runs/{run_id}").json()
            while status["lifecycle"] not in {"done", "failed", "stopped"}:
                assert time.monotonic() < deadline, "the in-process Worker never picked it up"
                time.sleep(0.2)
                status = client.get(f"/runs/{run_id}").json()

            assert status["lifecycle"] == "done"
            assert client.get(f"/runs/{run_id}/answer").json()["text"]

            report = client.get(f"/runs/{run_id}/report").json()
            assert report["terminal_state"] == "completed"
            assert [call["tool"] for call in report["tool_calls"]] == ["lookup_order"]

    def test_an_idempotency_key_admits_one_run_not_two(self, tmp_path: Path) -> None:
        """What makes at-least-once delivery survivable, so it is worth pinning."""
        with self.client(self.generate(tmp_path)) as client:
            body = {"message": "refund A1", "idempotency_key": "webhook-evt-8891"}
            first = client.post("/runs", json=body).json()
            second = client.post("/runs", json=body).json()

            assert first["run_id"] == second["run_id"]
            assert first["created"] is True
            assert second["created"] is False

    def test_another_tenant_gets_a_404_rather_than_a_403(self, tmp_path: Path) -> None:
        """A 403 confirms the Run exists, which is the thing being withheld."""
        with self.client(self.generate(tmp_path)) as client:
            run_id = client.post("/runs", json={"message": "hello"}).json()["run_id"]
            assert client.get(f"/runs/{run_id}").status_code == 200

            elsewhere = client.get(f"/runs/{run_id}", headers={"X-Tenant": "somebody-else"})
            assert elsewhere.status_code == 404

    def test_the_worker_refuses_to_claim_from_an_unreachable_store(self, tmp_path: Path) -> None:
        """Two processes cannot share an in-memory store, so it says so and exits.

        Starting and silently claiming nothing is the failure mode this
        replaces: a Worker that looks healthy while no Run ever moves.
        """
        project = self.generate(tmp_path)
        environment = {k: v for k, v in os.environ.items() if k != "PSYCH_POSTGRES_DSN"}
        environment["PYTHONPATH"] = str(REPO)

        result = subprocess.run(
            [sys.executable, "worker.py"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=environment,
        )
        assert result.returncode != 0
        assert "PSYCH_POSTGRES_DSN is unset" in result.stderr
