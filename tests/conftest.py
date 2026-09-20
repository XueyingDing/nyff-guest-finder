"""Test helpers shared by the offline regression tests.

These tests never touch the network, and never read or write anything under
results/ or the repository root -- every script constant that points at a file
(IN_FILE / OUT_FILE / CACHE_FILE / ...) is monkeypatched to a path under
pytest's per-test tmp_path fixture.
"""
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")


def load_script(filename):
    """Import a scripts/*.py file (its name isn't a valid Python identifier, so a
    plain `import` statement can't reach it) as a fresh module each time it's called."""
    path = os.path.join(SCRIPTS_DIR, filename)
    mod_name = "_test_loaded_" + os.path.splitext(filename)[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)  # runs top-level code only; main() is behind `if __name__ == "__main__"`
    return module
