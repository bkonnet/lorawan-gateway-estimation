"""Lightweight performance instrumentation for the Streamlit estimator.

This module is intentionally dependency-free. It can be used from coverage and UI
code without changing the RF model or numerical results.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Iterator


@dataclass
class PerformanceProfile:
    """Collect elapsed times and workload counters for one estimation run."""

    timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + (perf_counter() - start)

    def set_counter(self, name: str, value: int) -> None:
        self.counters[name] = int(value)

    @property
    def total_seconds(self) -> float:
        return sum(self.timings.values())

    def rows(self) -> list[dict[str, float | int | str]]:
        rows: list[dict[str, float | int | str]] = [
            {"Etapa": name, "Tiempo (s)": round(seconds, 3), "Valor": ""}
            for name, seconds in self.timings.items()
        ]
        rows.extend(
            {"Etapa": name, "Tiempo (s)": "", "Valor": value}
            for name, value in self.counters.items()
        )
        return rows
