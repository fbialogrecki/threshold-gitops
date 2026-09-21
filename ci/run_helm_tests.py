"""Run every CI test, rejecting missing/empty required suites and skips."""
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('HELM_BIN', 'helm')
root = Path(__file__).resolve().parent
loader = unittest.TestLoader()
for name in ('test_helm_checks', 'test_ci_contract'):
    if not (root / (name + '.py')).is_file():
        sys.exit('required test module missing: ' + name)
    suite = loader.loadTestsFromName(name)
    if suite.countTestCases() == 0:
        sys.exit('required test module empty: ' + name)
suite = loader.discover(str(root), pattern='test_*.py')
if not suite.countTestCases():
    sys.exit('test discovery empty')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
