"""Pytest fixtures for the test suite.

The tests in tests/test_command.py call ``self.assertIsNotNone`` but the
test class does not inherit from ``unittest.TestCase``. Inject the unittest
assertion methods so the tests can run unmodified.
"""

import unittest

from tests.test_command import TestCommandSystem

for _name in dir(unittest.TestCase):
    if _name.startswith("assert") and not hasattr(TestCommandSystem, _name):
        setattr(TestCommandSystem, _name, getattr(unittest.TestCase, _name))
