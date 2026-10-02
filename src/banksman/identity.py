"""Holder identity: who asks for a lease, found from facts that every caller has.

The owner is the worktree. The agent is the nearest ancestor process whose program is a known
agent, and that process decides liveness. The issue comes from the caller, otherwise from the
branch name or the worktree directory name. It works the same for any agent and for a person
at a terminal, and the caller types none of it.
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
from banksman.system import Process

# The variable in which an agent gives the id of its session, for the agents that set one. It
# is only for display, so an agent without one loses nothing.
SESSION_VARIABLES = {"claude": "CLAUDE_CODE_SESSION_ID"}
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
    agent_pid, agent = find_agent(table, rules.agents, os.getpid() if pid is None else pid)
    if issue is not None and ISSUE.fullmatch(issue) is None:
        raise BanksmanError(
            "an issue id has 1 to 64 letters, digits, and the characters '#', '.', '_', '/',"
            " and '-'"
        )
    if issue is None:
        issue = issue_of(rules, branch(owner), owner.name)
    variable = SESSION_VARIABLES.get(agent or "")
    session = (os.environ if env is None else env).get(variable) if variable else None
    return Holder(
        owner=str(owner),
        owner_pid=agent_pid if owner_pid is None else owner_pid,
        issue=issue,
        agent=agent,
        session=session if session and SESSION.fullmatch(session) else None,
        purpose=None if purpose is None else purpose_text(purpose),
    )


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
    table: Mapping[int, Process], agents: Sequence[str], pid: int
) -> tuple[int | None, str | None]:
    """Return the nearest ancestor of a process whose program is a known agent, and its name."""
    for ancestor in fencing.ancestors(table, pid):
        process = table.get(ancestor)
        if process is None:
            break
        # macOS shows the full path of the executable, which for an agent that an app ships is
        # a path inside the app. The name is compared case-sensitively: the Claude app itself is
        # "Claude", and it is not the agent.
        name = os.path.basename(process.program)
        if name in agents:
            return ancestor, name
    return None, None


def purpose_text(text: str) -> str | None:
    """Return a purpose that is safe to show to people and to other agents, or None."""
    # Whitespace becomes one space before the control characters go, so that two lines stay two
    # words.
    cleaned = clean(" ".join(text.split())).strip()
    return cleaned[:MAX_PURPOSE_LENGTH].rstrip() or None


def _read_small(path: Path) -> str:
    with open(path, "rb") as file:
        return file.read(_MAX_GIT_FILE).decode()

