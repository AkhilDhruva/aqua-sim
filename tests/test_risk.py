import math

import pytest

from aqua_sim.config import GRAVITY
from aqua_sim.risk.alerts import AlertLog, Severity
from aqua_sim.risk.hazard import HazardClass, classify_hazard
from aqua_sim.risk.sink_nodes import SinkNode, orifice_inflow, time_to_fill


def test_dry_cell_has_no_hazard():
    assert classify_hazard(0.0, 5.0) == HazardClass.NONE


def test_deep_fast_water_is_extreme_and_worse_than_shallow_slow():
    assert classify_hazard(1.0, 2.0) == HazardClass.EXTREME
    assert classify_hazard(0.1, 0.1) < classify_hazard(1.0, 2.0)


def test_orifice_inflow_only_above_threshold():
    node = SinkNode("N", 0, 0, threshold_elevation=100.0, opening_area_m2=2.0)
    assert orifice_inflow(node, 99.5) == 0.0
    q = orifice_inflow(node, 101.0)  # 1.0 m head
    assert q == pytest.approx(0.6 * 2.0 * math.sqrt(2 * GRAVITY * 1.0))


def test_time_to_fill_none_when_dry():
    node = SinkNode("N", 0, 0, threshold_elevation=100.0, opening_area_m2=2.0, capacity_m3=500.0)
    assert time_to_fill(node, 99.0) is None
    assert time_to_fill(node, 101.0) > 0


def test_alert_log_ranks_critical_first():
    log = AlertLog()
    log.add(10.0, Severity.WARNING, "A", "rising")
    log.add(20.0, Severity.CRITICAL, "B", "breach")
    ranked = log.ranked()
    assert ranked[0].severity == Severity.CRITICAL
    assert log.to_records()[0]["severity"] == "CRITICAL"
