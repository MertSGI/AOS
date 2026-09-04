"""Unit tests for Design Benchmark (R18)."""

import pytest
import sys
from pathlib import Path

# Add benchmarks/design-intelligence to sys.path for clean import
benchmark_dir = Path(__file__).parent.parent
sys.path.insert(0, str(benchmark_dir))

from design_benchmark import DesignBenchmarkRunner


def test_design_benchmark_runs_cleanly():
    runner = DesignBenchmarkRunner()
    results = runner.run_benchmark()

    assert len(results) == 6
    passed_count = sum(1 for r in results if r.passed)
    assert passed_count == 6  # 100% detection rate on adversarial fixtures

    # Ensure zero false positives & false negatives
    total_fps = sum(len(r.false_positives) for r in results)
    total_fns = sum(len(r.false_negatives) for r in results)
    assert total_fps == 0
    assert total_fns == 0
