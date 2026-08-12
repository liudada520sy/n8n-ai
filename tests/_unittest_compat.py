"""Compatibility helpers for legacy function-style tests.

The project baseline uses ``unittest discover`` as its single test entry point.
These helpers preserve the existing function-style cases without requiring
pytest in the runtime environment.
"""

from __future__ import annotations

import inspect
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace


class _Raises:
    def __init__(self, expected_exception):
        self._context = unittest.TestCase().assertRaises(expected_exception)

    def __enter__(self):
        return self._context.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        return self._context.__exit__(exc_type, exc_value, traceback)


class PytestCompat:
    @staticmethod
    def raises(expected_exception):
        return _Raises(expected_exception)


pytest = PytestCompat()


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self._undo.append((target, name, original))

    def undo(self):
        while self._undo:
            target, name, original = self._undo.pop()
            setattr(target, name, original)


class CaptureFixture:
    def __init__(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def readouterr(self):
        return SimpleNamespace(
            out=self.stdout.getvalue(),
            err=self.stderr.getvalue(),
        )


def load_function_tests(module_globals):
    """Return a unittest suite for top-level ``test_*`` functions."""
    suite = unittest.TestSuite()
    Path("work").mkdir(exist_ok=True)

    for name, test_function in sorted(module_globals.items()):
        if not name.startswith("test_") or not callable(test_function):
            continue

        def run_test(function=test_function):
            monkeypatch = MonkeyPatch()
            capsys = CaptureFixture()
            fixtures = {
                "monkeypatch": monkeypatch,
                "capsys": capsys,
            }
            parameters = inspect.signature(function).parameters
            kwargs = {name: fixtures[name] for name in parameters}
            try:
                with redirect_stdout(capsys.stdout), redirect_stderr(capsys.stderr):
                    function(**kwargs)
            finally:
                monkeypatch.undo()

        suite.addTest(unittest.FunctionTestCase(run_test, description=name))

    return suite
