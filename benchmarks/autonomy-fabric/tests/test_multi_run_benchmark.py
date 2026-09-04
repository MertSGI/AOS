"""Unit tests for Multi-Run Autonomy Benchmark (R19)."""

import pytest
import sys
from pathlib import Path

benchmark_dir = Path(__file__).parent.parent
sys.path.insert(0, str(benchmark_dir))

from multi_run_benchmark import MultiRunAutonomyBenchmarkRunner


def test_multi_run_autonomy_benchmark():
    runner = MultiRunAutonomyBenchmarkRunner()
    report = runner.run_benchmark()

    assert report.runs_isolated is True
    assert report.workspaces_isolated is True
    assert report.authorities_isolated is True
    assert report.waiting_human_non_blocking is True
    assert report.waiting_authority_non_blocking is True
    assert report.recovery_successful is True
    assert report.all_verifications_passed is True
