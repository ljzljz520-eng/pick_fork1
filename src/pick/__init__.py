import curses
import queue
import textwrap
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any, Container, Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar, Union

import cadule

from .backend import Backend
from .blessed_backend import BlessedBackend
from .curses_backend import CursesBackend

__all__ = [
    "Picker",
    "pick",
    "Option",
    "Position",
    "Backend",
    "CursesBackend",
    "BlessedBackend",
    "__call__",
    "SYMBOL_CIRCLE_FILLED",
    "SYMBOL_CIRCLE_EMPTY",
]


@dataclass
class Option:
    label: str
    value: Any = None
    description: Optional[str] = None
    enabled: bool = True
    key: Any = None
    """Explicit stable identity. Must be hashable. ``None`` means the option
    keeps the default value-based (or positional) identity."""


KEYS_ENTER = (curses.KEY_ENTER, ord("\n"), ord("\r"))
KEYS_UP = (curses.KEY_UP, ord("k"))
KEYS_DOWN = (curses.KEY_DOWN, ord("j"))
KEYS_SELECT = (curses.KEY_RIGHT, ord(" "))

SYMBOL_CIRCLE_FILLED = "(x)"
SYMBOL_CIRCLE_EMPTY = "( )"

#: How long the input loop waits for a key before re-checking the option
#: event queue. Update events therefore wake a blocked loop within this delay.
INPUT_POLL_TIMEOUT = 0.05

#: Shown in place of the option list when the current snapshot is empty.
NO_OPTIONS_TEXT = "(no options available)"

OPTION_T = TypeVar("OPTION_T", str, Option)
PICK_RETURN_T = Tuple[OPTION_T, int]

Position = namedtuple('Position', ['y', 'x'])

# Namespace tags used inside the tuple-based identity keys. They keep explicit
# keys, value-derived keys and positional fallbacks from ever colliding.
_KEY_TAG_EXPLICIT = "key"
_KEY_TAG_VALUE = "value"
_KEY_TAG_POSITION = "index"


@dataclass(frozen=True)
class _Entry:
    """One item of an immutable option snapshot."""

    key: Any
    option: Any
    positional: bool = False


@dataclass(frozen=True)
class _OptionsEvent:
    """A queued option-list mutation applied on the loop thread."""

    kind: str  # "replace" | "update"
    entries: Tuple[_Entry, ...]


def _is_hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _build_entries(options: Sequence[Any]) -> Tuple[_Entry, ...]:
    """Turn a caller-provided sequence into an immutable, validated snapshot.

    Identity strategy, in order:

    * ``Option`` with an explicit, non-``None`` ``key`` -> the key itself.
      Duplicate explicit keys raise ``ValueError`` (a broken promise that must
      be fixed by the caller).
    * Otherwise the option value (the raw item, or ``Option.value``): when it
      is hashable it is the identity. For an ``Option`` with no explicit value
      the label is used instead, so label-only options stay reconciled across
      reorders. Two equal identities sharing one snapshot fall back to
      positional identity for the later one, so legacy lists with duplicated
      values keep working exactly as before.
    * Unhashable values (e.g. dicts) use positional identity, matching the
      historical index-based behavior.
    """
    seen: set = set()
    entries: List[_Entry] = []
    for position, option in enumerate(options):
        if isinstance(option, Option) and option.key is not None:
            if not _is_hashable(option.key):
                raise TypeError(
                    f"Option.key must be hashable, got: {option.key!r}"
                )
            key = (_KEY_TAG_EXPLICIT, option.key)
            if key in seen:
                raise ValueError(f"duplicate option key: {option.key!r}")
            positional = False
        else:
            if isinstance(option, Option):
                value = option.value if option.value is not None else option.label
            else:
                value = option
            if _is_hashable(value):
                key = (_KEY_TAG_VALUE, value)
                positional = False
            else:
                key = (_KEY_TAG_POSITION, position)
                positional = True
            if not positional and key in seen:
                # Implicit value identities collide (e.g. ["a", "a"]): the
                # later item keeps working by falling back to its position.
                key = (_KEY_TAG_POSITION, position)
                positional = True
        seen.add(key)
        entries.append(_Entry(key, option, positional))
    return tuple(entries)


@dataclass
class Picker(Generic[OPTION_T]):
    options: Sequence[OPTION_T]
    title: Optional[str] = None
    indicator: str = "*"
    default_index: int = 0
    multiselect: bool = False
    min_selection_count: int = 0
    selected_indexes: List[int] = field(init=False, default_factory=list)
    index: int = field(init=False, default=0)
    screen: Optional[curses.window] = None
    position: Position = Position(0, 0)
    clear_screen: bool = True
    quit_keys: Optional[Union[Container[int], Iterable[int]]] = None
    backend: Union[str, Backend] = "curses"

    # Immutable snapshot the Picker actually renders against; never aliased to
    # a caller-owned mutable list.
    _entries: Tuple[_Entry, ...] = field(init=False)
    # Authoritative focus/selection state, keyed by stable identity.
    _focus_key: Any = field(init=False, default=None)
    _selected_keys: List[Any] = field(init=False, default_factory=list)
    # FIFO of option-list mutations, safe to feed from any thread.
    _events: "queue.Queue[_OptionsEvent]" = field(init=False, default_factory=queue.Queue)
    _loop_running: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if len(self.options) == 0:
            raise ValueError("options should not be an empty list")

        if self.default_index >= len(self.options):
            raise ValueError("default_index should be less than the length of options")

        if self.multiselect and self.min_selection_count > len(self.options):
            raise ValueError(
                "min_selection_count is bigger than the available options, you will not be able to make any selection"
            )

        entries = _build_entries(self.options)

        if all(not self._entry_enabled(entry) for entry in entries):
            raise ValueError(
                "all given options are disabled, you must at least have one enabled option."
            )

        self._entries = entries
        # Detach from the caller's sequence: later caller-side mutations can
        # no longer drift the rendered data source.
        self.options = tuple(entry.option for entry in entries)

        self.index = self.default_index
        self._focus_key = entries[self.index].key
        if not self._entry_enabled(entries[self.index]):
            self.move_down()

    # ------------------------------------------------------------------
    # Dynamic data source
    # ------------------------------------------------------------------
    def replace_options(self, options: Sequence[Any]) -> None:
        """Replace the whole candidate set (e.g. after device rediscovery).

        The new sequence becomes a fresh immutable snapshot and focus,
        selection, disabled state, descriptions and indexes are reconciled by
        stable identity in a single state transition. Options that disappeared
        are removed from the selection; an empty sequence is legal while the
        picker is running (unlike construction time).

        Safe to call from any thread: the change is queued and applied on the
        input-loop thread, waking a loop blocked waiting for a key.
        """
        self._emit(_OptionsEvent("replace", _build_entries(options)))

    def update_options(self, options: Sequence[Any]) -> None:
        """Update options by identity without removing anything.

        Each incoming option is matched against the current snapshot by key:
        matching options are replaced in place (new label/value/description/
        enabled state), unknown keys are appended, and options not present in
        the update are left untouched. Use this for status refreshes (tasks
        toggling enabled state, remote descriptions changing); use
        :meth:`replace_options` when the candidate set grows, shrinks or is
        reordered.

        Safe to call from any thread, like :meth:`replace_options`.
        """
        self._emit(_OptionsEvent("update", _build_entries(options)))

    def _emit(self, event: _OptionsEvent) -> None:
        self._events.put(event)
        if not self._loop_running:
            # No loop consuming the queue yet (or ever): apply synchronously
            # so callers see the change immediately.
            self._process_events()

    def _process_events(self) -> None:
        """Drain queued option events on the loop thread, in FIFO order."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "replace":
                new_entries = event.entries
            else:
                new_entries = self._merge_entries(self._entries, event.entries)
            self._apply_snapshot(new_entries)

    @staticmethod
    def _merge_entries(
        current: Tuple[_Entry, ...], updates: Tuple[_Entry, ...]
    ) -> Tuple[_Entry, ...]:
        """Apply an update event: replace by key, append unknown, never delete."""
        merged = list(current)
        positions = {entry.key: i for i, entry in enumerate(merged)}

        for incoming in updates:
            if incoming.positional:
                # Positional items address a snapshot index directly.
                pos = incoming.key[1]
                if 0 <= pos < len(merged):
                    del positions[merged[pos].key]
                    merged[pos] = incoming
                    positions[incoming.key] = pos
                else:
                    entry = _Entry((_KEY_TAG_POSITION, len(merged)), incoming.option, True)
                    positions[entry.key] = len(merged)
                    merged.append(entry)
            elif incoming.key in positions:
                merged[positions[incoming.key]] = incoming
            else:
                positions[incoming.key] = len(merged)
                merged.append(incoming)
        return tuple(merged)

    def _apply_snapshot(self, new_entries: Tuple[_Entry, ...]) -> None:
        """Single atomic transition from the old snapshot to ``new_entries``.

        All reconciliation (focus, multiselect set, disabled state, indexes)
        happens here against the new snapshot, so rendering, description
        panel and :meth:`get_selected` can never observe mixed state.
        """
        old_focus_key = self._focus_key
        old_selected_keys = list(self._selected_keys)
        old_index = self.index

        self._entries = new_entries
        self.options = tuple(entry.option for entry in new_entries)

        positions = {entry.key: i for i, entry in enumerate(new_entries)}
        size = len(new_entries)

        # Reconcile focus by identity.
        if size == 0:
            # Empty snapshot: no current item.
            self.index = -1
            self._focus_key = None
        else:
            new_index = -1
            if old_focus_key is not None:
                if old_focus_key[0] == _KEY_TAG_POSITION:
                    pos = old_focus_key[1]
                    if 0 <= pos < size:
                        new_index = pos
                else:
                    new_index = positions.get(old_focus_key, -1)
            if new_index < 0:
                # Current item was removed (or focus never existed): prefer
                # the option now sitting at the old position, enabled first.
                new_index = self._land_on_enabled(new_entries, old_index)
            self.index = new_index
            self._focus_key = new_entries[new_index].key

        # Reconcile the multiselect set: keys that disappeared or became
        # disabled are dropped; surviving keys keep their selection order.
        kept_keys: List[Any] = []
        for key in old_selected_keys:
            pos = positions.get(key)
            if pos is not None and self._entry_enabled(new_entries[pos]):
                kept_keys.append(key)
        self._selected_keys = kept_keys
        self._refresh_selected_indexes()

    @staticmethod
    def _land_on_enabled(entries: Tuple[_Entry, ...], preferred_index: int) -> int:
        size = len(entries)
        if size == 0:
            return -1
        start = min(preferred_index, size - 1) if preferred_index >= 0 else 0
        for step in range(size):
            idx = (start + step) % size
            if Picker._entry_enabled(entries[idx]):
                return idx
        # Everything is disabled: stay visible at a concrete index; the loop
        # guards marking/confirming disabled items.
        return start

    @staticmethod
    def _entry_enabled(entry: _Entry) -> bool:
        option = entry.option
        return not isinstance(option, Option) or option.enabled

    def _refresh_selected_indexes(self) -> None:
        positions = {entry.key: i for i, entry in enumerate(self._entries)}
        indexes: List[int] = []
        for key in self._selected_keys:
            pos = positions.get(key)
            if pos is not None and self._entry_enabled(self._entries[pos]):
                indexes.append(pos)
        self.selected_indexes = indexes

    # ------------------------------------------------------------------
    # Navigation and selection
    # ------------------------------------------------------------------
    def move_up(self) -> None:
        size = len(self._entries)
        if size == 0:
            return
        if not any(self._entry_enabled(entry) for entry in self._entries):
            return
        while True:
            self.index -= 1
            if self.index < 0:
                self.index = size - 1
            if self._entry_enabled(self._entries[self.index]):
                break
        self._focus_key = self._entries[self.index].key

    def move_down(self) -> None:
        size = len(self._entries)
        if size == 0:
            return
        if not any(self._entry_enabled(entry) for entry in self._entries):
            return
        while True:
            self.index += 1
            if self.index >= size:
                self.index = 0
            if self._entry_enabled(self._entries[self.index]):
                break
        self._focus_key = self._entries[self.index].key

    def mark_index(self) -> None:
        if not self.multiselect or self.index < 0:
            return
        entry = self._entries[self.index]
        if not self._entry_enabled(entry):
            return
        if entry.key in self._selected_keys:
            self._selected_keys.remove(entry.key)
        else:
            self._selected_keys.append(entry.key)
        self._refresh_selected_indexes()

    def get_selected(self) -> Union[List[PICK_RETURN_T], PICK_RETURN_T]:
        """return the current selected option as a tuple: (option, index)
        or as a list of tuples (in case multiselect==True)

        Indexes always refer to the current snapshot. With an empty snapshot
        the single-select form returns ``(None, -1)`` and multiselect ``[]``.
        """
        if self.multiselect:
            return_tuples = []
            for selected in self.selected_indexes:
                return_tuples.append((self._entries[selected].option, selected))
            return return_tuples
        elif self.index < 0:
            return None, -1
        else:
            return self._entries[self.index].option, self.index

    def get_title_lines(self, *, max_width: int = 80) -> List[str]:
        if not self.title:
            return []

        if "\n" in self.title:
            lines = self.title.split("\n")
        else:
            lines = textwrap.fill(self.title, max_width - 2, drop_whitespace=False).split("\n")
        return lines + [""]

    def get_option_lines(self) -> List[str]:
        lines: List[str] = []
        if not self._entries:
            return [f"{len(self.indicator) * ' '} {NO_OPTIONS_TEXT}"]
        for index, entry in enumerate(self._entries):
            if index == self.index:
                prefix = self.indicator
            else:
                prefix = len(self.indicator) * " "

            if self.multiselect:
                symbol = (
                    SYMBOL_CIRCLE_FILLED
                    if index in self.selected_indexes
                    else SYMBOL_CIRCLE_EMPTY
                )
                prefix = f"{prefix} {symbol}"

            option = entry.option
            option_as_str = option.label if isinstance(option, Option) else option
            lines.append(f"{prefix} {option_as_str}")

        return lines

    def get_lines(self, *, max_width: int = 80) -> Tuple[List[str], int]:
        title_lines = self.get_title_lines(max_width=max_width)
        option_lines = self.get_option_lines()
        lines = title_lines + option_lines
        current_line = self.index + len(title_lines) + 1 if self.index >= 0 else len(title_lines)
        return lines, current_line

    def draw(self, screen: Backend) -> None:
        """draw the UI on the screen, handle scroll if needed"""
        if self.clear_screen:
            screen.clear()

        y, x = self.position  # start point

        max_y, max_x = screen.getmaxyx()
        max_rows = max_y - y  # the max rows we can draw

        lines, current_line = self.get_lines(max_width=max_x)

        # calculate how many lines we should scroll, relative to the top
        scroll_top = 0
        if current_line > max_rows:
            scroll_top = current_line - max_rows

        lines_to_draw = lines[scroll_top : scroll_top + max_rows]

        description_present = False
        for entry in self._entries:
            option = entry.option
            if isinstance(option, Option) and option.description is not None:
                description_present = True
                break

        title_length = len(self.get_title_lines(max_width=max_x))

        for i, line in enumerate(lines_to_draw):
            if description_present and i > title_length:
                screen.addnstr(y, x, line, max_x // 2 - 2)
            else:
                screen.addnstr(y, x, line, max_x - 2)
            y += 1

        if self.index >= 0:
            option = self._entries[self.index].option
            if isinstance(option, Option) and option.description is not None:
                description_lines = textwrap.fill(option.description, max_x // 2 - 2).split('\n')

                for i, line in enumerate(description_lines):
                    screen.addnstr(i + title_length, max_x // 2, line, max_x - 2)

        screen.refresh()

    def run_loop(
        self, screen: Backend, position: Position
    ) -> Union[List[PICK_RETURN_T], PICK_RETURN_T]:
        self._loop_running = True
        try:
            while True:
                self._process_events()
                self.draw(screen)
                c = self._read_input(screen)
                if c == -1:
                    # Poll timeout: option events above may have changed the
                    # snapshot; simply redraw on the next iteration.
                    continue
                if self.quit_keys is not None and c in self.quit_keys:
                    if self.multiselect:
                        return []
                    else:
                        return None, -1
                elif c in KEYS_UP:
                    self.move_up()
                elif c in KEYS_DOWN:
                    self.move_down()
                elif c in KEYS_ENTER:
                    if (
                        self.multiselect
                        and len(self.selected_indexes) < self.min_selection_count
                    ):
                        continue
                    if (
                        not self.multiselect
                        and (self.index < 0 or not self._entry_enabled(self._entries[self.index]))
                    ):
                        # No selectable current item (empty snapshot or an
                        # option that just became disabled).
                        continue
                    return self.get_selected()
                elif c in KEYS_SELECT and self.multiselect:
                    self.mark_index()
        finally:
            self._loop_running = False

    def _read_input(self, screen: Backend) -> int:
        """Read one key, waking from the block periodically to serve events."""
        getch_timeout = getattr(screen, "getch_timeout", None)
        if getch_timeout is not None:
            return int(getch_timeout(INPUT_POLL_TIMEOUT))
        return screen.getch()

    def _resolve_backend(self) -> Backend:
        if isinstance(self.backend, Backend):
            return self.backend
        if self.backend == "curses":
            return CursesBackend(screen=self.screen)
        if self.backend == "blessed":
            return BlessedBackend()
        raise ValueError(
            f"Unknown backend: {self.backend!r}. "
            "Use 'curses', 'blessed', or a Backend instance."
        )

    def config_curses(self) -> None:
        try:
            # use the default colors of the terminal
            curses.use_default_colors()
            # hide the cursor
            curses.curs_set(0)
        except Exception:
            # Curses failed to initialize color support, eg. when TERM=vt100
            curses.initscr()

    def _start(self, screen: curses.window):
        self.config_curses()
        return self.run_loop(CursesBackend(screen=screen), self.position)

    def start(self):
        backend = self._resolve_backend()
        if isinstance(backend, CursesBackend) and backend._screen is not None:
            # Embedded in an existing curses application (backward-compatible)
            last_cur = curses.curs_set(0)
            ret = self.run_loop(backend, self.position)
            if last_cur:
                curses.curs_set(last_cur)
            return ret
        elif isinstance(backend, CursesBackend):
            # Standalone curses mode
            def _curses_main(screen: curses.window):
                backend._screen = screen
                backend.setup()
                return self.run_loop(backend, self.position)
            return curses.wrapper(_curses_main)
        else:
            # Other backends (e.g. blessed)
            backend.setup()
            try:
                return self.run_loop(backend, self.position)
            finally:
                backend.teardown()


def pick(
    options: Sequence[OPTION_T],
    title: Optional[str] = None,
    indicator: str = "*",
    default_index: int = 0,
    multiselect: bool = False,
    min_selection_count: int = 0,
    screen: Optional[curses.window] = None,
    position: Position = Position(0, 0),
    clear_screen: bool = True,
    quit_keys: Optional[Union[Container[int], Iterable[int]]] = None,
    backend: Union[str, Backend] = "curses",
):
    picker: Picker = Picker(
        options,
        title,
        indicator,
        default_index,
        multiselect,
        min_selection_count,
        screen,
        position,
        clear_screen,
        quit_keys,
        backend,
    )
    return picker.start()


@cadule
def __call__(
    options: Sequence[OPTION_T],
    title: Optional[str] = None,
    indicator: str = "*",
    default_index: int = 0,
    multiselect: bool = False,
    min_selection_count: int = 0,
    screen: Optional[curses.window] = None,
    position: Position = Position(0, 0),
    clear_screen: bool = True,
    quit_keys: Optional[Union[Container[int], Iterable[int]]] = None,
    backend: Union[str, Backend] = "curses",
):
    return pick(
        options,
        title,
        indicator,
        default_index,
        multiselect,
        min_selection_count,
        screen,
        position,
        clear_screen,
        quit_keys,
        backend,
    )
