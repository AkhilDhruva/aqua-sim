import math

import pytest

from aqua_sim.config import GRAVITY
from aqua_sim.physics.stability import cfl_timestep


def test_cfl_matches_formula():
    dx, h, u, cfl = 5.0, 0.5, 1.0, 0.7
    expected = cfl * dx / (u + math.sqrt(GRAVITY * h))
    assert cfl_timestep(dx, h, u, cfl=cfl) == pytest.approx(expected)


def test_deeper_water_needs_smaller_step():
    shallow = cfl_timestep(5.0, 0.1, 0.0)
    deep = cfl_timestep(5.0, 5.0, 0.0)
    assert deep < shallow


def test_dry_domain_falls_back_to_cap():
    assert cfl_timestep(5.0, 0.0, 0.0, max_dt=60.0) == 60.0


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        cfl_timestep(0.0, 1.0)
    with pytest.raises(ValueError):
        cfl_timestep(5.0, 1.0, cfl=1.5)
