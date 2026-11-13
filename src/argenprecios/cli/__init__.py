"""Command-line interface package for ArgenPrecios."""

from __future__ import annotations

from argenprecios.cli.main import build_parser, main, run_cli

__all__ = [
    "build_parser",
    "main",
    "run_cli",
]
