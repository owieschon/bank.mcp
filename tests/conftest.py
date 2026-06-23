"""Make the package importable when running tests from a clean clone without
`pip install -e .` (CI installs the package; this is a convenience for local runs)."""
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
