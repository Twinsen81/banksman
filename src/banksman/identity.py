"""Holder identity: who asks for a lease, found from facts that every caller has.

The owner is the worktree. The agent is the nearest ancestor process whose program is a known
agent, otherwise the agent process that the environment names, and that process decides
liveness. The issue comes from the caller, otherwise from the branch name or the worktree
directory name. It works the same for any agent and for a person at a terminal, and the caller
types none of it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from banksman import fencing
from banksman.config import HolderRules
from banksman.errors import BanksmanError
from banksman.lease import ISSUE, MAX_PURPOSE_LENGTH, SESSION, Holder
from banksman.sanitize import clean
from banksman.system import Process, started_no_later

# The variable in which an agent gives the id of its session, for the agents that set one. It
# is only for display, so an agent without one loses nothing.
SESSION_VARIABLES = {"claude": "CLAUDE_CODE_SESSION_ID"}
# The variable in which an agent gives the pid of its agent process, for the agents that set
# one. A command that has left the process tree of its agent, for example with nohup, keeps the
# environment that it got from the agent.
PID_VARIABLES = {"claude": "CLAUDE_PID"}
_MAX_PID_DIGITS = 10
_MAX_GIT_FILE = 4096
_HEAD_BRANCH = "ref: refs/heads/"


def find_holder(
    table: Mapping[int, Process],
    rules: HolderRules,
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    pid: int | None = None,
    issue: str | None = None,
    owner_pid: int | None = None,
    purpose: str | None = None,
) -> Holder:
    """Return the holder for a caller: the process `pid`, by default this one.

    `issue`, `owner_pid`, and `purpose` are what the caller gives explicitly.
    """
    try:
        directory = Path(os.path.realpath(os.getcwd() if cwd is None else cwd))
    except FileNotFoundError:
        raise BanksmanError("the working directory does not exist") from None
    owner = worktree(directory)
    env = os.environ if env is None else env
    agent_pid, agent = find_agent(table, rules.agents, os.getpid() if pid is None else pid, env)
    if issue is not None and ISSUE.fullmatch(issue) is None:
        raise BanksmanError(
            "an issue id has 1 to 64 letters, digits, and the characters '#', '.', '_', '/',"
            " and '-'"
        )
    if issue is None:
        issue = issue_of(rules, branch(owner), owner.name)
    return Holder(
        owner=str(owner),
        owner_pid=agent_pid if owner_pid is None else owner_pid,
        issue=issue,
        agent=agent,
        session=agent_session(agent, env),
        purpose=None if purpose is None else purpose_text(purpose),
    )


def agent_session(agent: str | None, env: Mapping[str, str]) -> str | None:
    """Return the id of the session that the environment gives for an agent, when it is valid."""
    variable = SESSION_VARIABLES.get(agent or "")
    session = env.get(variable) if variable else None
    return session if session and SESSION.fullmatch(session) else None


def worktree(directory: Path) -> Path:
    """Return the git toplevel of a directory, or the directory itself outside a repository.

    A worktree or a submodule has a `.git` file instead of a directory, so any `.git` entry ends
    the search, as for `git rev-parse --show-toplevel`.
    """
    for candidate in (directory, *directory.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return directory


def branch(toplevel: Path) -> str | None:
    """Return the branch that is checked out in a worktree, or None, for example when detached."""
    dot_git = toplevel / ".git"
    try:
        if dot_git.is_dir():
            git_dir = dot_git
        else:
            # A linked worktree or a submodule: "gitdir: <path>", relative to the worktree.
            line = _read_small(dot_git).partition("\n")[0]
            if not line.startswith("gitdir: "):
                return None
            git_dir = toplevel / line[len("gitdir: ") :].strip()
        head = _read_small(git_dir / "HEAD").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not head.startswith(_HEAD_BRANCH):
        return None
    return head[len(_HEAD_BRANCH) :] or None


def issue_of(rules: HolderRules, *texts: str | None) -> str | None:
    """Return the first issue id that the issue pattern finds in the texts, in order.

    With a group in the pattern, the issue id is the text of the first group. A branch name is
    text that anybody can choose, so a match that is not a valid issue id is not used.
    """
    for text in texts:
        if not text:
            continue
        match = rules.issue_pattern.search(text)
        if match is None:
            continue
        found = match.group(1) if rules.issue_pattern.groups else match.group(0)
        if found and ISSUE.fullmatch(found):
            return found
    return None


def find_agent(
    table: Mapping[int, Process], agents: Sequence[str], pid: int, env: Mapping[str, str]
) -> tuple[int | None, str | None]:
    """Return the agent process of a process and the name of its program: the nearest ancestor
    whose program is a known agent, otherwise the agent process that `env`, the environment of
    the process, names."""
    for ancestor in fencing.ancestors(table, pid):
        process = table.get(ancestor)
        if process is None:
            break
        name = agent_name(process, agents)
        if name is not None:
            return ancestor, name
    caller = table.get(pid)
    return (None, None) if caller is None else named_agent(table, agents, env, caller)


def named_agent(
    table: Mapping[int, Process], agents: Sequence[str], env: Mapping[str, str], caller: Process
) -> tuple[int | None, str | None]:
    """Return the agent process that the environment of a process names, and the name of its
    program, when that process runs, is the known agent that sets the variable, and started no
    later than the process.

    The walk up the process tree comes first: when one agent runs another, the variable names the
    outer one, and the nearest one runs the command. The start time keeps a pid that the system
    gave to a later process from counting.
    """
    for agent, variable in PID_VARIABLES.items():
        value = env.get(variable, "")
        if not (value.isascii() and value.isdigit() and len(value) <= _MAX_PID_DIGITS):
            continue
        process = table.get(int(value))
        if (
            process is not None
            and process.pid != caller.pid
            and agent_name(process, agents) == agent
            and started_no_later(process.started, caller.started)
        ):
            return process.pid, agent
    return None, None


def agent_name(process: Process, agents: Sequence[str]) -> str | None:
    """Return the name of the program of a process when it is a known agent."""
    # macOS shows the full path of the executable, which for an agent that an app ships is a
    # path inside the app. The name is compared case-sensitively: the Claude app itself is
    # "Claude", and it is not the agent.
    name = os.path.basename(process.program)
    return name if name in agents else None


def purpose_text(text: str) -> str | None:
    """Return a purpose that is safe to show to people and to other agents, or None."""
    # Whitespace becomes one space before the control characters go, so that two lines stay two
    # words.
    cleaned = clean(" ".join(text.split())).strip()
    return cleaned[:MAX_PURPOSE_LENGTH].rstrip() or None


def _read_small(path: Path) -> str:
    with open(path, "rb") as file:
        return file.read(_MAX_GIT_FILE).decode()

