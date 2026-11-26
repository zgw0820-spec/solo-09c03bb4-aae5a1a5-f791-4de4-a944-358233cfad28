#!/usr/bin/env python3
"""Run the full offline test suite with only the Python standard library."""

import os
import sys
import unittest

if __name__ == "__main__":
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(repo_root, "tests"),
        pattern="test_*.py",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
