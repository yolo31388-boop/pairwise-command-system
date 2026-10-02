"""Test bridge: some test classes use unittest assertions without inheriting
from ``unittest.TestCase``. Inject the missing assertion helpers at
collection time so those tests can run (test files are left untouched)."""

import unittest


class _AssertionBridge(unittest.TestCase):
    def runTest(self):  # pragma: no cover - never executed
        pass


_bridge = _AssertionBridge()

_ASSERTION_NAMES = (
    "assertEqual", "assertEquals", "assertNotEqual",
    "assertAlmostEqual", "assertNotAlmostEqual",
    "assertTrue", "assertFalse",
    "assertIs", "assertIsNot", "assertIsNone", "assertIsNotNone",
    "assertIn", "assertNotIn",
    "assertIsInstance", "assertNotIsInstance",
    "assertRaises", "assertRaisesRegex",
    "assertGreater", "assertGreaterEqual", "assertLess", "assertLessEqual",
    "assertRegex", "assertNotRegex",
    "assertDictEqual", "assertListEqual", "assertTupleEqual",
    "assertCountEqual",
)


def pytest_collection_modifyitems(items):
    patched = set()
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None or issubclass(cls, unittest.TestCase):
            continue
        if id(cls) in patched:
            continue
        for name in _ASSERTION_NAMES:
            if not hasattr(cls, name):
                method = getattr(_bridge, name, None)
                if method is not None:
                    setattr(cls, name, method)
        patched.add(id(cls))
