"""The risk matrix — a time-stamped, severity-ranked alert log.

Consumed by both the viewer and any generated report, so it is plain structured
data. The solver/risk loop appends alerts as thresholds are crossed; here we
define the record and a small collecting log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    CRITICAL = 2


@dataclass
class Alert:
    time_s: float
    severity: Severity
    location: str
    message: str


@dataclass
class AlertLog:
    """An ordered collection of alerts."""

    alerts: list[Alert] = field(default_factory=list)

    def add(self, time_s: float, severity: Severity, location: str, message: str) -> None:
        self.alerts.append(Alert(time_s, severity, location, message))

    def ranked(self) -> list[Alert]:
        """Most severe first, then earliest — the order a responder wants."""
        return sorted(self.alerts, key=lambda a: (-a.severity, a.time_s))

    def to_records(self) -> list[dict]:
        """JSON-serializable form for the viewer / report export."""
        return [
            {
                "time_s": a.time_s,
                "severity": a.severity.name,
                "location": a.location,
                "message": a.message,
            }
            for a in self.ranked()
        ]
