"""Run P09 conformance tests from the exact checkout, including with python -I."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'workflows/p09/tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print('P09 conformance does not allow skipped tests', file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
