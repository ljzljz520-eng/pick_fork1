import threading
import time
from collections import deque
from typing import Any, Deque

import pytest

from pick import Backend, Option, Picker


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeBackend(Backend):
    """Scripted in-memory backend; no real terminal involved."""

    def __init__(self, keys: Any = ()) -> None:
        self.keys: Deque[Any] = deque(keys)

    # Backend API
    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def getmaxyx(self) -> Any:
        return (40, 120)

    def addnstr(self, y: int, x: int, s: str, n: int) -> None:
        pass

    def getch(self) -> int:
        return int(self.keys.popleft())

    def getch_timeout(self, timeout: float) -> int:
        if self.keys:
            key = self.keys.popleft()
            return int(key(self) if callable(key) else key)
        return -1

    def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Original behavior
# ---------------------------------------------------------------------------
def test_move_up_down():
    title = "Please choose an option: "
    options = ["option1", "option2", "option3"]
    picker = Picker(options, title)
    picker.move_up()
    assert picker.get_selected() == ("option3", 2)
    picker.move_down()
    picker.move_down()
    assert picker.get_selected() == ("option2", 1)


def test_default_index():
    title = "Please choose an option: "
    options = ["option1", "option2", "option3"]
    picker = Picker(options, title, default_index=1)
    assert picker.get_selected() == ("option2", 1)


def test_get_lines():
    title = "Please choose an option: "
    options = ["option1", "option2", "option3"]
    picker = Picker(options, title, indicator="*")
    lines, current_line = picker.get_lines()
    assert lines == [title, "", "* option1", "  option2", "  option3"]
    assert current_line == 3


def test_no_title():
    options = ["option1", "option2", "option3"]
    picker = Picker(options)
    _, current_line = picker.get_lines()
    assert current_line == 1


def test_pick_list_of_non_str_and_option():
    # More details: https://github.com/aisk/pick/issues/120
    options = [{"key1": "value1"}, {"key2": "value2"}]
    picker = Picker(options)  # type: ignore
    lines, _ = picker.get_lines()
    assert lines == ["* {'key1': 'value1'}", "  {'key2': 'value2'}"]


def test_multi_select():
    title = "Please choose an option: "
    options = ["option1", "option2", "option3"]
    picker = Picker(options, title, multiselect=True, min_selection_count=1)
    assert picker.get_selected() == []
    picker.mark_index()
    assert picker.get_selected() == [("option1", 0)]
    picker.move_down()
    picker.mark_index()
    assert picker.get_selected() == [("option1", 0), ("option2", 1)]


def test_option():
    options = [Option("option1", 101, "description1"), Option("option2", 102),
               Option("option3", description="description3"), Option("option4")]
    picker = Picker(options, multiselect=True)
    for _ in range(4):
        picker.mark_index()
        picker.move_down()
    selected_options = picker.get_selected()
    for option in selected_options:
        assert isinstance(option, tuple)
        assert isinstance(option[0], Option)
    option = selected_options[0]
    assert option[0].label == "option1"
    assert option[0].value == 101
    assert option[0].description == "description1"


def test_disabled_option():
    options = [Option("option1"), Option("option2", enabled=False), Option("option3")]
    picker = Picker(options)
    assert picker.get_selected() == (Option("option1"), 0)
    picker.move_down()
    assert picker.get_selected() == (Option("option3"), 2)


def test_mark_index_disabled_option():
    options = [Option("option1"), Option("option2", enabled=False), Option("option3")]
    picker = Picker(options, multiselect=True)
    picker.index = 1  # Point directly to disabled option2
    picker.mark_index()
    assert picker.get_selected() == []  # Disabled option should NOT be marked


# ---------------------------------------------------------------------------
# Immutable snapshots
# ---------------------------------------------------------------------------
def test_snapshot_detached_from_caller_list():
    source = ["option1", "option2", "option3"]
    picker = Picker(source)
    source.append("option4")
    source[0] = "mutated"
    assert isinstance(picker.options, tuple)
    assert len(picker.options) == 3
    assert picker.options[0] == "option1"


def test_constructor_still_rejects_empty_list():
    with pytest.raises(ValueError):
        Picker([])


# ---------------------------------------------------------------------------
# Stable identity: explicit keys
# ---------------------------------------------------------------------------
def test_focus_follows_explicit_key_through_reorder():
    options = [Option("A", key="a"), Option("B", key="b"), Option("C", key="c")]
    picker = Picker(options)
    picker.move_down()
    assert picker.get_selected() == (Option("B", key="b"), 1)

    picker.replace_options(
        [Option("C", key="c"), Option("A", key="a"), Option("B", key="b")]
    )
    assert picker.get_selected() == (Option("B", key="b"), 2)


def test_focus_follows_explicit_key_through_insert():
    options = [Option("A", key="a"), Option("B", key="b")]
    picker = Picker(options)
    picker.move_down()  # B at 1

    picker.replace_options(
        [Option("A", key="a"), Option("X", key="x"), Option("B", key="b")]
    )
    assert picker.get_selected() == (Option("B", key="b"), 2)


def test_selection_follows_explicit_keys_through_reorder():
    options = [Option("A", key="a"), Option("B", key="b"), Option("C", key="c")]
    picker = Picker(options, multiselect=True)
    picker.mark_index()  # A
    picker.move_down()
    picker.mark_index()  # B

    picker.replace_options(
        [Option("C", key="c"), Option("A", key="a"), Option("B", key="b")]
    )
    # Selection order is preserved; indexes remapped onto the new snapshot.
    assert picker.get_selected() == [
        (Option("A", key="a"), 1),
        (Option("B", key="b"), 2),
    ]


# ---------------------------------------------------------------------------
# Stable identity: plain values
# ---------------------------------------------------------------------------
def test_focus_follows_plain_value_identity():
    picker = Picker(["a", "b", "c"])
    picker.move_down()  # b at 1
    picker.replace_options(["c", "a", "b"])
    assert picker.get_selected() == ("b", 2)


def test_selection_follows_plain_value_identity():
    picker = Picker(["a", "b", "c"], multiselect=True)
    picker.mark_index()  # a
    picker.move_down()
    picker.mark_index()  # b

    picker.replace_options(["c", "a", "b"])
    assert picker.get_selected() == [("a", 1), ("b", 2)]


def test_unhashable_values_use_positional_identity():
    options = [{"id": 1}, {"id": 2}]
    picker = Picker(options)  # type: ignore
    picker.replace_options([{"id": 2}, {"id": 1}])  # type: ignore
    # No value identity: focus stays at its position, matching legacy behavior.
    assert picker.get_selected() == ({"id": 2}, 0)


# ---------------------------------------------------------------------------
# Current item deleted
# ---------------------------------------------------------------------------
def test_focus_lands_on_successor_when_current_deleted():
    picker = Picker(["a", "b", "c"])
    picker.move_down()  # b at 1
    picker.replace_options(["a", "c"])
    assert picker.get_selected() == ("c", 1)


def test_focus_lands_on_predecessor_when_last_deleted():
    picker = Picker(["a", "b", "c"])
    picker.move_down()
    picker.move_down()  # c at 2
    picker.replace_options(["a", "b"])
    assert picker.get_selected() == ("b", 1)


def test_focus_skips_disabled_successor_after_delete():
    options = [Option("a"), Option("b"), Option("c"), Option("d")]
    picker = Picker(options)
    picker.move_down()  # b at 1
    picker.replace_options(
        [Option("a"), Option("c", enabled=False), Option("d")]
    )
    assert picker.get_selected() == (Option("d"), 2)


# ---------------------------------------------------------------------------
# Selected items deleted / disabled
# ---------------------------------------------------------------------------
def test_selected_item_deleted_is_removed_from_selection():
    picker = Picker(["a", "b", "c"], multiselect=True)
    picker.mark_index()  # a
    picker.move_down()
    picker.mark_index()  # b

    picker.replace_options(["b", "c"])
    assert picker.get_selected() == [("b", 0)]


def test_selected_item_disabled_is_removed_from_selection():
    options = [Option("a", "a"), Option("b", "b"), Option("c", "c")]
    picker = Picker(options, multiselect=True)
    picker.mark_index()  # a
    picker.move_down()
    picker.mark_index()  # b

    picker.update_options([Option("b", "b", enabled=False)])
    assert picker.get_selected() == [(Option("a", "a"), 0)]


def test_current_item_disabled_keeps_focus_consistent():
    options = [Option("a", "a"), Option("b", "b"), Option("c", "c")]
    picker = Picker(options)
    picker.move_down()  # b at 1

    picker.update_options([Option("b", "b", enabled=False)])
    # Rendering and get_selected still agree on the same (disabled) object.
    assert picker.index == 1
    assert picker.get_selected() == (Option("b", "b", enabled=False), 1)

    picker.move_down()
    assert picker.get_selected() == (Option("c", "c"), 2)


# ---------------------------------------------------------------------------
# Empty snapshots
# ---------------------------------------------------------------------------
def test_empty_snapshot_single_select():
    picker = Picker(["a", "b"], title="title")
    picker.replace_options([])

    assert picker.index == -1
    assert picker.get_selected() == (None, -1)
    lines, current_line = picker.get_lines()
    assert lines == ["title", "", "  (no options available)"]
    assert current_line == 2
    # draw must not crash or go out of bounds
    picker.draw(FakeBackend())


def test_empty_snapshot_multi_select_clears_selection():
    picker = Picker(["a", "b"], multiselect=True)
    picker.mark_index()
    picker.replace_options([])

    assert picker.selected_indexes == []
    assert picker.get_selected() == []


def test_recovery_from_empty_snapshot():
    picker = Picker(["a", "b"])
    picker.replace_options([])
    picker.replace_options(["c", "d"])

    assert picker.index == 0
    assert picker.get_selected() == ("c", 0)


# ---------------------------------------------------------------------------
# update_options semantics
# ---------------------------------------------------------------------------
def test_update_options_refreshes_fields_by_identity():
    options = [Option("a", "A"), Option("b", "B")]
    picker = Picker(options)
    picker.move_down()  # B at 1

    picker.update_options([Option("new-b", "B", description="desc-b")])

    assert picker.get_selected() == (
        Option("new-b", "B", description="desc-b"),
        1,
    )
    # Untouched options stay in place; nothing is deleted.
    assert picker.options[0] == Option("a", "A")


def test_update_options_appends_unknown_keys():
    picker = Picker(["a", "b"])
    picker.update_options(["c"])
    assert picker.options == ("a", "b", "c")
    picker.update_options([])
    assert picker.options == ("a", "b", "c")


def test_update_options_replaces_unhashable_by_position():
    picker = Picker([{"id": 1}, {"id": 2}])  # type: ignore
    picker.update_options([{"id": 10}])
    assert picker.options[0] == {"id": 10}
    assert picker.options[1] == {"id": 2}


# ---------------------------------------------------------------------------
# Duplicate keys
# ---------------------------------------------------------------------------
def test_duplicate_explicit_keys_rejected_at_construction():
    with pytest.raises(ValueError):
        Picker([Option("a", key="x"), Option("b", key="x")])


def test_duplicate_explicit_keys_rejected_at_replace():
    picker = Picker(["a", "b"])
    with pytest.raises(ValueError):
        picker.replace_options([Option("a", key=1), Option("b", key=1)])


def test_unhashable_explicit_key_rejected():
    with pytest.raises(TypeError):
        Picker([Option("a", key=["x"])])  # type: ignore


def test_duplicate_plain_values_remain_supported():
    picker = Picker(["a", "a", "b"], multiselect=True)
    picker.mark_index()  # first a at 0
    picker.move_down()
    picker.mark_index()  # second a at 1

    assert picker.get_selected() == [("a", 0), ("a", 1)]

    picker.replace_options(["x", "a", "a"])
    # The value-identified "a" follows to index 1; the positional one loses
    # its slot because index 1 now has a value-identified occupant.
    assert picker.get_selected() == [("a", 1)]


# ---------------------------------------------------------------------------
# Event-driven loop: wakeup and confirmation guards
# ---------------------------------------------------------------------------
def test_loop_enter_and_quit_via_backend():
    enter_backend = FakeBackend([ord("\n")])
    picker = Picker(["a", "b"], backend=enter_backend)
    assert picker.start() == ("a", 0)

    quit_backend = FakeBackend([ord("q")])
    picker = Picker(["a", "b"], quit_keys=[ord("q")], backend=quit_backend)
    assert picker.start() == (None, -1)


def test_loop_multiselect_mark_and_enter():
    backend = FakeBackend([ord(" "), ord("\n")])
    picker = Picker(
        ["a", "b"], multiselect=True, min_selection_count=1, backend=backend
    )
    assert picker.start() == [("a", 0)]


def test_loop_blocks_enter_on_disabled_current():
    options = [Option("a", "a"), Option("b", "b"), Option("c", "c")]
    backend = FakeBackend([ord("\n"), ord("j"), ord("\n")])
    picker = Picker(options, backend=backend)
    picker.move_down()  # b at 1
    picker.update_options([Option("b", "b", enabled=False)])

    result = picker.start()
    assert result == (Option("c", "c"), 2)


def test_loop_blocks_enter_on_empty_snapshot_until_recovery():
    def recover(_: FakeBackend) -> int:
        picker.replace_options(["x"])
        return -1

    backend = FakeBackend([ord("\n"), recover, ord("\n")])
    picker = Picker(["a"], backend=backend)
    picker.replace_options([])

    assert picker.start() == ("x", 0)


class _ThreadedWakeBackend(FakeBackend):
    def __init__(self, picker: Picker, new_options: Any) -> None:
        super().__init__()
        self.picker = picker
        self.new_options = new_options
        self.calls = 0
        self.redrawn_lines: Any = None

    def getch_timeout(self, timeout: float) -> int:
        self.calls += 1
        if self.calls == 1:
            def worker() -> None:
                # The loop is blocked waiting for input right now.
                time.sleep(0.05)
                self.picker.replace_options(self.new_options)

            threading.Thread(target=worker, daemon=True).start()
            return -1

        if tuple(self.picker.options) == tuple(self.new_options):
            self.redrawn_lines, _ = self.picker.get_lines()
            return ord("\n")

        if self.calls > 100:
            raise AssertionError("replace_options event never woke the loop")
        # Emulate a blocking wait so the call count stays bounded.
        time.sleep(0.01)
        return -1


def test_update_event_wakes_loop_blocked_on_input():
    backend = _ThreadedWakeBackend(None, ["x", "y", "z"])  # type: ignore
    picker = Picker(["a", "b"], backend=backend)
    backend.picker = picker

    result = picker.start()

    assert result == ("x", 0)
    assert backend.redrawn_lines is not None
    assert "* x" in backend.redrawn_lines
