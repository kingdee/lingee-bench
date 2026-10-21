"""Entry point of the erp-mock-mcp command (``[project.scripts]`` in pyproject.toml).

``erp-mock-mcp <command> ...`` is one group with the four commands of requirement 22 under
it, and this module is the group: what every command shares before it starts - the log on
stderr - and what every command shares when it ends, which is its exit code. The options
the commands have in common are :mod:`erp_mock.cli.options`, and each command is a module
of its own.

The exit codes, which requirement 22.8 fixes and this module is where they are decided:

====  ==============================================  ==============================
code  what happened                                   who answers it
====  ==============================================  ==============================
0     the command did what it was asked               the command
1     a validation step reported errors               :class:`ConfigErrorsReport`,
      (requirements 2.2, 22.5 and 22.7)               over :class:`ConfigErrors`
1     a Snapshot could not be built, or a port        the command (tasks 24.2, 24.3)
      could not be bound
2     a usage error: ``--instance`` missing, an       click, which answers every
      option that does not exist, an option value     :class:`click.UsageError`
      that is not valid (a ``--port`` outside 1 to    this way
      65535, an ``--anchor-date`` of requirement
      16.13), a variable of requirement 23.9 that
      is not valid (requirement 23.10), a profile
      or dataset the instance does not have
      (requirements 14.12 and 22.9)
====  ==============================================  ==============================

Why the group catches :class:`ConfigErrors` rather than each command: loading raises it
from wherever the failing step was reached - before a port is bound, before a sandbox is
created, before a dataset is built - and every one of the four commands loads. Catching it
in one place is what makes "the error list on stderr, exit code 1" a property of the
command line rather than something each command has to remember (requirement 22.7).
:class:`ConfigErrorsReport` prints the list of requirement 2.2 as the loader formatted it,
one error per line and nothing around it, so the output of a failed validation is the same
whichever command was running.

Everything else a command does with an exception is the command's own: the gateway returns
an exit code rather than raising (:func:`~erp_mock.gateway.serve.serve`), and a Snapshot
build that fails during seed is reported by seed (task 24.2).
"""

from typing import IO, Any, Final

import click

from erp_mock import FRAMEWORK_VERSION
from erp_mock.cli.options import EXIT_FAILED, setup_logging
from erp_mock.cli.seed import seed
from erp_mock.cli.serve import serve
from erp_mock.cli.stdio import stdio
from erp_mock.cli.validate import validate
from erp_mock.loader.errors import ConfigErrors

__all__ = ["COMMANDS", "PROGRAM_NAME", "ConfigErrorsReport", "ErpMockGroup", "main"]

# The name of the command, as ``[project.scripts]`` installs it; click would take the
# function name otherwise, and usage lines and ``--version`` would read "main".
PROGRAM_NAME: Final = "erp-mock-mcp"


class ConfigErrorsReport(click.ClickException):
    """The error list of the validation step that failed (requirements 2.2 and 22.7).

    A :class:`ClickException` so that click does the exiting: it writes the report through
    :meth:`show` and ends the process with :attr:`exit_code`, which is
    :data:`~erp_mock.cli.options.EXIT_FAILED`.
    """

    exit_code = EXIT_FAILED

    def __init__(self, errors: ConfigErrors) -> None:
        super().__init__(str(errors))
        self.errors = errors

    def show(self, file: IO[Any] | None = None) -> None:
        """Write one line per error to stderr, in the order the loader sorted them.

        The lines of :meth:`ConfigErrors.lines` and nothing else: no ``Error:`` prefix of
        click's own and no summary, so the report is the error list of requirement 2.2 and
        can be read, diffed and pasted as one.
        """
        for line in self.errors.lines():
            click.echo(line, file=file, err=True)


class ErpMockGroup(click.Group):
    """The group of the four commands, which answers a failed validation with exit code 1.

    :class:`ConfigErrors` may be raised while a command parses its arguments or while it
    runs, and both happen inside :meth:`invoke`, so one ``except`` covers every command and
    every step of loading (module docstring).
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Invoke the command of ``ctx``, turning ConfigErrors into a report of its own."""
        try:
            return super().invoke(ctx)
        except ConfigErrors as errors:
            raise ConfigErrorsReport(errors) from errors


@click.group(name=PROGRAM_NAME, cls=ErpMockGroup)
@click.version_option(version=FRAMEWORK_VERSION, prog_name=PROGRAM_NAME)
def main() -> None:
    """ERP-Mock-MCP: a configurable ERP mock served over MCP."""
    # Before anything a command logs, and before any command runs: the log of the service
    # points at stderr alone, because the stdout of stdio carries MCP messages
    # (requirement 22.2) and the stdout of seed one line per dataset (requirement 22.10).
    setup_logging()


# The four commands of requirement 22, registered here and nowhere else, so that what
# ``erp-mock-mcp --help`` lists is one list in one file.
COMMANDS: Final[tuple[click.Command, ...]] = (validate, seed, serve, stdio)

for _command in COMMANDS:
    main.add_command(_command)
