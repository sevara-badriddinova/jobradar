import time

import pytest

from jobradar.cycle import cycle_lock


def test_cycle_overlap_is_skipped_and_lock_released(engine):
    with cycle_lock(engine) as first:
        assert first
        with cycle_lock(engine) as second:
            assert not second
    # The server releases a dropped session's advisory lock asynchronously.
    for _ in range(50):
        with cycle_lock(engine) as third:
            if third:
                break
        time.sleep(0.05)
    assert third


def test_cycle_lock_released_after_failure(engine):
    with pytest.raises(RuntimeError):
        with cycle_lock(engine) as acquired:
            assert acquired
            raise RuntimeError("stage failed")
    with cycle_lock(engine) as acquired:
        assert acquired
