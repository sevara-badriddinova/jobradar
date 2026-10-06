from pathlib import Path

import pytest

_TESTS = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Mark tests by folder, so `pytest -m unit` / `-m integration` just work."""
    for item in items:
        parts = Path(item.fspath).relative_to(_TESTS).parts
        if parts and parts[0] in ("unit", "integration"):
            item.add_marker(getattr(pytest.mark, parts[0]))
