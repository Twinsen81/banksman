"""The reap job: what banksman admin reap-agent prints, and how banksman reads a saved job."""

import os
import plistlib
import sys

import pytest

from banksman import job
from banksman.cli import main


def test_reap_agent_prints_a_launchd_job_on_macos(capsys, monkeypatch):
    monkeypatch.setattr(job, "LAUNCHD", True)
    assert main(["admin", "reap-agent"]) == 0
    out = capsys.readouterr().out
    assert out == job.launchd(job.program(sys.argv[0]))
    saved = plistlib.loads(out.encode())
    assert saved == {
        "Label": "banksman.reap",
        "ProgramArguments": [sys.executable, "-m", "banksman", "reap"],
        "StartInterval": 300,
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
    }
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/banksman.reap.plist" in out


def test_reap_agent_prints_a_crontab_line_elsewhere(capsys, monkeypatch):
    monkeypatch.setattr(job, "LAUNCHD", False)
    assert main(["admin", "reap-agent"]) == 0
    comment, line = capsys.readouterr().out.splitlines()
    assert comment.startswith("# banksman reap job")
    assert line == f"*/5 * * * * {sys.executable} -m banksman reap >/dev/null"


def test_the_job_runs_the_banksman_that_printed_it(tmp_path, monkeypatch):
    program = tmp_path / "bin" / "banksman"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    link = tmp_path / "link" / "banksman"
    link.parent.mkdir()
    link.symlink_to(program)
    # The link stays, so that an upgrade that moves its target keeps the job working.
    assert job.program(str(link)) == (str(link),)
    monkeypatch.chdir(tmp_path)
    assert job.program("bin/banksman") == (str(program),)
    # As python -m banksman, or a file that is not banksman: this Python with -m banksman.
    assert job.program("/somewhere/banksman/__main__.py") == (sys.executable, "-m", "banksman")
    program.chmod(0o644)
    assert job.program(str(program)) == (sys.executable, "-m", "banksman")


def test_a_saved_launchd_job_names_its_banksman():
    printed = job.launchd(("/opt/my tools/banksman",))
    assert job.saved_launchd(printed) == ("/opt/my tools/banksman",)
    for changed in (
        printed.replace("<integer>300</integer>", "<integer>600</integer>"),
        printed.replace("<true/>", "<false/>"),
        printed.replace("banksman reap job", "a reap job"),
        "",
        "not a job",
        printed.replace("<string>reap</string>\n", ""),
    ):
        assert job.saved_launchd(changed) is None


def test_a_crontab_line_names_its_banksman():
    line = job.cron_line(("/opt/my tools/banksman",))
    assert line == "*/5 * * * * '/opt/my tools/banksman' reap >/dev/null"
    assert job.saved_cron(f"MAILTO=me\n{line}\n0 * * * * backup\n") == (
        ("/opt/my tools/banksman",),
        True,
    )
    assert job.saved_cron("*/10 * * * * /opt/banksman reap\n") == (None, True)
    assert job.saved_cron("# */5 * * * * /opt/banksman reap\n0 * * * * backup\n") == (None, False)


def test_launchctl_and_crontab_answer_through_the_runner():
    calls = []

    def run(command):
        calls.append(command)
        return {"launchctl": (113, ""), "crontab": (1, "")}[os.path.basename(command[0])]

    assert job.loaded(run) is False
    assert job.crontab(run) == ""
    assert calls == [
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/banksman.reap"],
        ["crontab", "-l"],
    ]
    assert job.crontab(lambda command: (0, "* * * * * x\n")) == "* * * * * x\n"


def test_a_program_that_cannot_run_is_an_error(tmp_path):
    with pytest.raises(job.JobError, match="crontab-of-no-system is not on the PATH"):
        job.run_program(["crontab-of-no-system", "-l"])
    with pytest.raises(job.JobError, match=f"{tmp_path}/launchctl does not exist"):
        job.run_program([str(tmp_path / "launchctl"), "print"])
    assert job.run_program([sys.executable, "-c", "print('x'); raise SystemExit(3)"]) == (3, "x\n")
