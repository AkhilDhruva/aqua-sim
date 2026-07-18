from aqua_sim import __version__
from aqua_sim.main import main


def test_version():
    assert __version__ == "0.1.0"


def test_main_runs():
    assert main([]) == 0
