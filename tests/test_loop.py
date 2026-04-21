from autocoder.loop import StalemateTracker


def test_stalemate_not_triggered_on_change():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "bbb")
    assert not t.note("bbb", "ccc")
    assert t.streak == 0


def test_stalemate_triggered_after_threshold_unchanged():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "aaa")  # streak=1, threshold=2 → not yet
    assert t.note("aaa", "aaa")  # streak=2 → stalemate


def test_stalemate_resets_on_change():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "aaa")  # streak=1
    assert not t.note("aaa", "bbb")  # reset to 0
    assert not t.note("bbb", "bbb")  # streak=1
    assert t.note("bbb", "bbb")  # streak=2


def test_stalemate_threshold_one_triggers_immediately():
    t = StalemateTracker(threshold=1)
    assert t.note("aaa", "aaa")


def test_stalemate_default_threshold_is_two():
    t = StalemateTracker()
    assert not t.note("a", "a")
    assert t.note("a", "a")
