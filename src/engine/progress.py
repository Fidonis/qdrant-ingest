"""What a run reports about itself while it works.

The runner keeps its counters on the in-memory ``RunRow`` and used to write them once, when
the run ended. A client polling ``GET /v1/runs/{id}`` therefore saw zeros for the whole
duration of a long run. ``ProgressFlusher`` writes the row at every phase change and, inside
the per-file loop, at most once per interval.

Writing progress is a courtesy to the reader and never part of the run's correctness: a
failed flush is logged and the run goes on.
"""

import logging
import time
from collections.abc import Callable

from state import RunRow, StateStore
from state.models import RunPhase

log = logging.getLogger("engine")

DEFAULT_INTERVAL = 2.0


class ProgressFlusher:
    def __init__(
        self,
        state: StateStore,
        run: RunRow,
        *,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = state
        self._run = run
        self._interval = interval
        self._clock = clock
        self._last = clock()

    def phase(self, phase: RunPhase) -> None:
        """Enter a phase and write it now: a phase is the thing a reader waits for."""
        self._run.phase = phase
        self.flush()

    def tick(self, current: str | None = None) -> None:
        """Note the file being processed and write the row if the interval has passed."""
        self._run.current = current
        if self._clock() - self._last >= self._interval:
            self.flush()

    def flush(self) -> None:
        self._last = self._clock()
        try:
            self._state.update_run(self._run)
        except Exception:  # noqa: BLE001 - progress must never fail a run
            log.warning("could not write the progress of run %s", self._run.run_id, exc_info=True)
