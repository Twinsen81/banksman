"""Holder identity: who asks for a lease, found from facts that every caller has.

The owner is the worktree. The agent is the nearest ancestor process whose program is a known
agent or that the environment names as the agent process, otherwise the agent process that the
environment names, and that process decides liveness. The issue comes from the caller,
otherwise from the branch name or the worktree directory name. It works the same for any agent
and for a person at a terminal, and the caller types none of it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from banksman import fencing
from banksman.config import HolderRules
from banksman.errors import BanksmanError
from banksman.lease import ISSUE, MAX_PURPOSE_LENGTH, SESSION, Holder, Session
from banksman.sanitize import clean
from banksman.system import Process, started_no_later

# The variable in which an agent gives the id of its session, for the agents that set one. The
# session keeps its id when the agent runs it in a new process, so a new process of the session
# continues its holdings. An agent without one loses only that.
SESSION_VARIABLES = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_SESSION_ID"}
# The variable in which an agent gives the pid of its agent process, for the agents that set
# one. A command that has left the process tree of its agent, for example with nohup, keeps the
# environment that it got from the agent.
PID_VARIABLES = {"claude": "CLAUDE_PID"}
_MAX_PID_DIGITS = 10
_MAX_GIT_FILE = 4096
_HEAD_BRANCH = "ref: refs/heads/"


@dataclass(frozen=True)
class StartedBy:
    """The agent process that started a process outside banksman, such as an emulator."""

    agent: str
    pid: int
    # False when the pid variable names an agent process that has ended since.
    running: bool = True


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
    from_env: bool = False,
) -> Holder:
    """Return the holder for a caller: the process `pid`, by default this one.

    `issue`, `owner_pid`, and `purpose` are what the caller gives explicitly. `from_env` is for
    a caller whose environment is that of another process that runs now (see `find_agent`).
    """
    try:
        directory = Path(os.path.realpath(os.getcwd() if cwd is None else cwd))
    except FileNotFoundError:
        raise BanksmanError("the working directory does not exist") from None
    owner = worktree(directory)
    env = os.environ if env is None else env
    caller = os.getpid() if pid is None else pid
    if from_env:
        agent_pid, agent = find_agent(table, rules.agents, caller, env, from_env=True)
    else:
        agent_pid, agent = find_agent(table, rules.agents, caller, env)
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


def session_of(agent: str | None, env: Mapping[str, str]) -> Session | None:
    """Return the session of an agent process from the environment, when the agent gives one."""
    session = agent_session(agent, env)
    return None if agent is None or session is None else Session(agent, session)


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
    table: Mapping[int, Process],
    agents: Sequence[str],
    pid: int,
    env: Mapping[str, str],
    *,
    from_env: bool = False,
) -> tuple[int | None, str | None]:
    """Return the agent process of a process and the name of its program: the nearest ancestor
    whose program is a known agent or that `env`, the environment of the process, names as the
    agent process, otherwise the agent process that `env` names.

    An agent can give its processes a title, which the process list shows in place of the
    program, so the walk alone can pass the agent and stop at a process above it, such as a
    service that hosts the sessions of the agent. When one agent runs another, the variable names
    the outer one, and the nearest one runs the command.

    With `from_env`, `env` is the environment of another process that runs now, not the
    environment that the caller got from its ancestors: a Gradle daemon takes the environment of
    the client of each build. The agent process that `env` names then counts when it runs, also
    when the caller or an ancestor of the caller started before it, and it comes before the walk,
    because the ancestors of such a caller say nothing about the client.
    """
    caller = table.get(pid)
    if caller is None:
        return None, None
    named_pid, named = named_agent(table, agents, env, caller, aged=not from_env)
    if from_env and named_pid is not None:
        return named_pid, named
    for ancestor in fencing.ancestors(table, pid):
        process = table.get(ancestor)
        if process is None:
            break
        if ancestor == named_pid:
            return named_pid, named
        name = agent_name(process, agents)
        if name is not None:
            return ancestor, name
    return named_pid, named


def named_agent(
    table: Mapping[int, Process],
    agents: Sequence[str],
    env: Mapping[str, str],
    caller: Process,
    *,
    aged: bool = True,
) -> tuple[int | None, str | None]:
    """Return the agent process that the environment of a process names, and the name of its
    program, when that process runs, is the known agent that sets the variable, also under a
    title (see `_is_agent`), and, with `aged`, started no later than each process of the branch
    of the caller (see `caller_branch`)."""
    for agent, variable in PID_VARIABLES.items():
        pid = _pid_value(env.get(variable, ""))
        if pid is None:
            continue
        process = table.get(pid)
        if (
            process is not None
            and process.pid != caller.pid
            and agent in agents
            and _is_agent(process, agent)
            and (
                not aged
                or all(
                    started_no_later(process.started, each.started)
                    for each in caller_branch(table, caller, process)
                )
            )
        ):
            return process.pid, agent
    return None, None


def started_by(
    table: Mapping[int, Process], agents: Sequence[str], process: Process, env: Mapping[str, str]
) -> StartedBy | None:
    """Return the agent process that started a process, found as for a caller (see
    `find_agent`) from `env`, the environment of the process.

    When the pid variable names a known agent whose process no longer runs, the result says that
    it has ended: the agent ran when it started the process, so a process that has its pid now
    and started after the process got the pid after the agent ended.
    """
    pid, agent = find_agent(table, agents, process.pid, env)
    if pid is not None and agent is not None:
        return StartedBy(agent, pid)
    for agent, variable in PID_VARIABLES.items():
        named = _pid_value(env.get(variable, ""))
        if named is None or named <= 1 or agent not in agents:
            continue
        now = table.get(named)
        if now is None or (
            started_no_later(process.started, now.started)
            and not started_no_later(now.started, process.started)
        ):
            return StartedBy(agent, named, running=False)
    return None


def _pid_value(value: str) -> int | None:
    if not (value.isascii() and value.isdigit() and len(value) <= _MAX_PID_DIGITS):
        return None
    return int(value)


def caller_branch(
    table: Mapping[int, Process], caller: Process, agent: Process
) -> list[Process]:
    """Return the caller and its ancestors below the nearest ancestor that it shares with the
    process that its environment names as the agent process.

    When that process is the agent that set the variable, every process of the branch descends
    from it, so each started after it. The init process, or the nearest ancestor that adopts
    orphans, adopted the top of the branch, and it is an ancestor of the agent too. A pid that
    the system gave to a new process after the agent ended started after the top of the branch,
    unless the whole branch started after the agent ended.
    """
    shared = set(fencing.ancestors(table, agent.pid))
    found = [caller]
    for pid in fencing.ancestors(table, caller.pid):
        process = table.get(pid)
        if pid in shared or process is None:
            break
        found.append(process)
    return found


def agent_name(process: Process, agents: Sequence[str]) -> str | None:
    """Return the name of the program of a process when it is a known agent."""
    # macOS shows the full path of the executable, which for an agent that an app ships is a
    # path inside the app. The name is compared case-sensitively: the Claude app itself is
    # "Claude", and it is not the agent.
    name = os.path.basename(process.program)
    return name if name in agents else None


def _is_agent(process: Process, agent: str) -> bool:
    """Whether the program of a process is an agent, also when the agent gave the process a title
    such as "claude bg-spare"."""
    name = os.path.basename(process.program)
    return name == agent or name.startswith(agent + " ")


def purpose_text(text: str) -> str | None:
    """Return a purpose that is safe to show to people and to other agents, or None."""
    # Whitespace becomes one space before the control characters go, so that two lines stay two
    # words.
    cleaned = clean(" ".join(text.split())).strip()
    return cleaned[:MAX_PURPOSE_LENGTH].rstrip() or None


def _read_small(path: Path) -> str:
    with open(path, "rb") as file:
        return file.read(_MAX_GIT_FILE).decode()

