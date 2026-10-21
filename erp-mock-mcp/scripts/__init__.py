"""Developer scripts, each run as ``uv run --locked python scripts/<name>.py``.

This file makes ``scripts`` a regular package so that tests import a script's functions
as ``from scripts.<name> import ...``: pytest puts the repository root on sys.path, and
mypy checks the files as ``scripts.<name>`` modules. Unlike a namespace package, a regular
package at the front of sys.path cannot be displaced by an unrelated top-level
``scripts`` package installed in site-packages.
"""
