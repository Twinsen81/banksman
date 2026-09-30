"""Errors that a command reports to its caller."""


class BanksmanError(Exception):
    """An error that a command prints as one line, with exit status 1."""

