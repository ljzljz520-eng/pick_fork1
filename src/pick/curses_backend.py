import curses
from typing import Optional, Tuple

from .backend import Backend


class CursesBackend(Backend):
    """Backend that uses the curses standard library."""

    def __init__(self, screen: Optional[curses.window] = None) -> None:
        self._screen = screen

    def setup(self) -> None:
        try:
            curses.use_default_colors()
            curses.curs_set(0)
        except Exception:
            curses.initscr()

    def teardown(self) -> None:
        pass  # curses.wrapper handles cleanup

    def clear(self) -> None:
        assert self._screen is not None
        self._screen.clear()

    def getmaxyx(self) -> Tuple[int, int]:
        assert self._screen is not None
        return self._screen.getmaxyx()

    def addnstr(self, y: int, x: int, s: str, n: int) -> None:
        assert self._screen is not None
        self._screen.addnstr(y, x, s, n)

    def getch(self) -> int:
        assert self._screen is not None
        return self._screen.getch()

    def getch_timeout(self, timeout: float) -> int:
        """Wait at most ``timeout`` seconds for a key; return -1 on timeout.

        Uses the per-window timeout so an embedded curses application's
        blocking behavior is restored afterwards.
        """
        assert self._screen is not None
        self._screen.timeout(int(timeout * 1000))
        try:
            return self._screen.getch()
        finally:
            self._screen.timeout(-1)

    def refresh(self) -> None:
        assert self._screen is not None
        self._screen.refresh()
