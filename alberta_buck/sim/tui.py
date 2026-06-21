"""Curses inspector for the Alberta Buck web3 sim.

A live, navigable view of every on-chain component and agent while the sim
runs.  The screen is a master list (left) of components + agent groups, each
showing a one-line summary, and a detail pane (right) for the selected item.
You drive the timeline yourself: step a tick, step a day, or free-run.

Layered for extensibility (see the module docstrings):
  driver.py   -- SimDriver: deploy + resumable day/tick stepping (no curses)
  inspect.py  -- Node tree: maps live state -> summary/detail text (no curses)
  tui.py      -- this file: the curses front-end only

This release focuses on *display*.  Parameter adjustment (PID gains, agent
probabilities, prices) is a later layer that will hang off the same tree.

    python -m alberta_buck.sim.tui --scenario rebalancing --basket prorata

Keys:
  Up/Down PgUp/PgDn Home/End   move selection
  Right / Enter                expand group   Left   collapse group
  Space  step one tick    d  step one day    r  run/pause    q  quit
  w  write snapshot vector to test/vectors/<scenario>-sim.json
"""

from __future__ import annotations

import argparse
import collections
import curses
import sys

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.driver import SimDriver
from alberta_buck.sim.inspect import build_tree, flatten
from alberta_buck.sim.scenario import SCENARIOS


class _LineCapture:
    """Stdout/stderr sink: agent `print()`s during stepping would otherwise
    scribble over the curses screen.  We swallow them into a ring buffer and
    surface the tail in the footer."""

    def __init__(self, maxlines: int = 1000):
        self.lines: collections.deque = collections.deque(maxlen=maxlines)
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.lines.append(line.rstrip())
        return len(s)

    def flush(self) -> None:
        pass

    def last(self) -> str:
        return self.lines[-1] if self.lines else ""


class TuiApp:
    LIST_MAX_W = 52        # cap the master-list width; rest is the detail pane

    def __init__(self, drv: SimDriver):
        self.drv = drv
        self.root = build_tree(drv)
        self.sel = 0           # selection index into the flattened visible rows
        self.top = 0           # first visible row (vertical scroll of the list)
        self.running = False
        self.status = "ready -- [space] step tick  [d] step day  [r] run  [q] quit"
        self.cap = _LineCapture()

    # -- geometry ------------------------------------------------------ #

    def _layout(self, scr):
        rows, cols = scr.getmaxyx()
        list_w = min(self.LIST_MAX_W, max(24, cols // 2))
        body_top = 3
        body_bot = rows - 2          # footer occupies rows-1
        return rows, cols, list_w, body_top, body_bot

    # -- drawing ------------------------------------------------------- #

    @staticmethod
    def _put(scr, r, c, text, attr=0, width=None):
        rows, cols = scr.getmaxyx()
        if r < 0 or r >= rows or c < 0 or c >= cols:
            return
        if width is None:
            width = cols - c
        try:
            scr.addnstr(r, c, text, max(0, min(width, cols - c)), attr)
        except curses.error:
            pass

    def _draw_header(self, scr, cols):
        d = self.drv
        state = "DONE " if d.done else ("RUN  " if self.running else "PAUSE")
        title = (f" Alberta Buck Sim  |  {d.scenario.name} [{d.basket_impl}]  "
                 f"|  day {d.day}/{d.days}  tick {d.tick}/{d.ticks_per_day}  "
                 f"|  {state}  {100 * d.progress:5.1f}% ")
        self._put(scr, 0, 0, title.ljust(cols), curses.A_REVERSE | curses.A_BOLD)
        keys = ("  Nav: arrows/PgUp/PgDn/Home/End   Right/Left expand/collapse"
                "   Step: [space]tick [d]day [r]run   [w]rite  [q]uit")
        self._put(scr, 1, 0, keys, curses.A_DIM)
        try:
            scr.hline(2, 0, curses.ACS_HLINE, cols)
        except curses.error:
            pass

    def _draw_list(self, scr, rows_, list_w, body_top, body_bot):
        visible = flatten(self.root)
        n = len(visible)
        self.sel = max(0, min(self.sel, n - 1))
        height = body_bot - body_top
        # Keep the selection within the viewport.
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + height:
            self.top = self.sel - height + 1
        self.top = max(0, min(self.top, max(0, n - height)))

        for i in range(height):
            ri = self.top + i
            if ri >= n:
                break
            depth, node = visible[ri]
            r = body_top + i
            sel = ri == self.sel
            attr = curses.A_REVERSE if sel else 0
            indent = "  " * depth
            if node.is_group:
                marker = "-" if node.expanded else "+"
                label = f"{indent}{marker} {node.label}"
                attr |= curses.A_BOLD
            else:
                label = f"{indent}  {node.label}"
            # label column, then the live summary filling the rest.
            label_w = min(len(label) + 1, list_w - 1)
            self._put(scr, r, 0, label.ljust(list_w), attr, width=list_w)
            summ = node.summary(self.drv)
            if summ and not sel:
                # Render the summary after the label (dim) within the list col.
                start = max(label_w, 16)
                self._put(scr, r, start, summ, curses.A_DIM,
                          width=list_w - start - 1)
            elif summ:
                start = max(label_w, 16)
                self._put(scr, r, start, summ, attr, width=list_w - start - 1)

        # scrollbar hint
        if n > height:
            self._put(scr, body_bot - 1, list_w - 1,
                      f"{self.sel + 1}/{n}", curses.A_DIM)

    def _draw_detail(self, scr, cols, list_w, body_top, body_bot):
        x0 = list_w + 1
        for r in range(body_top, body_bot):
            try:
                scr.vline(r, list_w, curses.ACS_VLINE, 1)
            except curses.error:
                pass
        visible = flatten(self.root)
        if not visible:
            return
        _, node = visible[max(0, min(self.sel, len(visible) - 1))]
        lines = node.details(self.drv)
        height = body_bot - body_top
        self._put(scr, body_top, x0, node.label or node.key,
                  curses.A_BOLD, width=cols - x0)
        for i, line in enumerate(lines[: height - 2]):
            self._put(scr, body_top + 2 + i, x0, line, width=cols - x0)
        if len(lines) > height - 2:
            self._put(scr, body_bot - 1, x0, "... (detail truncated)",
                      curses.A_DIM, width=cols - x0)

    def _draw_footer(self, scr, rows_, cols):
        last = self.drv.last_error or self.cap.last()
        msg = self.status if not last else f"{self.status}   ::  {last}"
        try:
            scr.hline(rows_ - 2, 0, curses.ACS_HLINE, cols)
        except curses.error:
            pass
        self._put(scr, rows_ - 1, 0, (" " + msg).ljust(cols), curses.A_REVERSE)

    def _render(self, scr):
        scr.erase()
        rows_, cols, list_w, body_top, body_bot = self._layout(scr)
        if rows_ < 8 or cols < 40:
            self._put(scr, 0, 0, "Terminal too small; enlarge the window.")
            scr.refresh()
            return
        self._draw_header(scr, cols)
        self._draw_list(scr, rows_, list_w, body_top, body_bot)
        self._draw_detail(scr, cols, list_w, body_top, body_bot)
        self._draw_footer(scr, rows_, cols)
        scr.noutrefresh()
        curses.doupdate()

    # -- input --------------------------------------------------------- #

    def _selected_node(self):
        visible = flatten(self.root)
        if not visible:
            return None
        return visible[max(0, min(self.sel, len(visible) - 1))][1]

    def _move(self, delta):
        n = len(flatten(self.root))
        if n:
            self.sel = max(0, min(self.sel + delta, n - 1))

    def _handle(self, scr, ch) -> bool:
        """Return False to quit."""
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_DOWN, ord("j")):
            self._move(1)
        elif ch in (curses.KEY_UP, ord("k")):
            self._move(-1)
        elif ch == curses.KEY_NPAGE:
            self._move(10)
        elif ch == curses.KEY_PPAGE:
            self._move(-10)
        elif ch == curses.KEY_HOME:
            self.sel = 0
        elif ch == curses.KEY_END:
            self.sel = len(flatten(self.root)) - 1
        elif ch in (curses.KEY_RIGHT, ord("\n"), curses.KEY_ENTER, ord("l")):
            node = self._selected_node()
            if node and node.is_group:
                node.expanded = True
        elif ch in (curses.KEY_LEFT, ord("h")):
            node = self._selected_node()
            if node and node.is_group and node.expanded:
                node.expanded = False
        elif ch == ord(" "):
            self.running = False
            self._step_tick()
        elif ch in (ord("d"), ord("D")):
            self.running = False
            self.status = "stepping one day..."
            self._render(scr)
            self.drv.step_day()
            self._after_step()
        elif ch in (ord("r"), ord("R")):
            self.running = not self.drv.done and not self.running
            self.status = "running..." if self.running else "paused"
        elif ch in (ord("w"), ord("W")):
            try:
                p = self.drv.write()
                self.status = f"wrote {p}"
            except Exception as e:
                self.status = f"write failed: {e!r}"[:120]
        return True

    def _step_tick(self):
        self.drv.step_tick()
        self._after_step()

    def _after_step(self):
        if self.drv.done:
            self.running = False
            self.status = "sim complete -- [w]rite vector, [q]uit"
        else:
            self.status = (f"day {self.drv.day} tick {self.drv.tick}  "
                           f"cyc {self.drv.ctr.get('cycleTrades', 0)}")

    # -- main loop ----------------------------------------------------- #

    def run(self, scr):
        curses.curs_set(0)
        scr.keypad(True)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = self.cap
        try:
            while True:
                self._render(scr)
                scr.timeout(0 if self.running else -1)
                ch = scr.getch()
                if ch == -1:
                    # No key: in run mode, advance one tick and loop to redraw.
                    if self.running and not self.drv.done:
                        self._step_tick()
                    continue
                if ch in (curses.KEY_RESIZE,):
                    continue
                if not self._handle(scr, ch):
                    break
        finally:
            sys.stdout, sys.stderr = old_out, old_err


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.tui")
    ap.add_argument("--scenario", default="rebalancing",
                    choices=sorted(SCENARIOS))
    ap.add_argument("--basket", default="prorata",
                    choices=["legacy", "prorata"])
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--ticks-per-day", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args(argv)

    sc = SCENARIOS[a.scenario]
    if a.days is not None:
        sc.days = min(a.days, sc.prices.days)
    if a.ticks_per_day is not None:
        sc.ticks_per_day = a.ticks_per_day
    if a.seed is not None:
        sc.seed = a.seed

    print(f"[tui] deploying '{sc.name}' with {a.basket} basket "
          f"({sc.days}d x {sc.ticks_per_day} ticks)...", flush=True)
    with Anvil(port=a.port) as anvil:
        drv = SimDriver(sc, anvil, basket_impl=a.basket, verbose=False)
        print("[tui] ready; launching inspector...", flush=True)
        curses.wrapper(TuiApp(drv).run)
    print("[tui] anvil stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
