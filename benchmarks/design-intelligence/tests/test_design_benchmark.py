"""Unit tests for Design Benchmark (R18 / Correction R1)."""

import pytest
import sys
from pathlib import Path

benchmark_dir = Path(__file__).parent.parent
sys.path.insert(0, str(benchmark_dir))

from design_benchmark import DesignBenchmarkRunner, BENCHMARK_FIXTURES


def test_expanded_design_benchmark_corpus_performance():
    runner = DesignBenchmarkRunner()
    results = runner.run_benchmark()

    assert len(results) == 24
    passed_count = sum(1 for r in results if r.passed)
    assert passed_count == 24  # 100% accuracy on the 24-fixture benchmark corpus

    total_fps = sum(len(r.false_positives) for r in results)
    total_fns = sum(len(r.false_negatives) for r in results)
    assert total_fps == 0
    assert total_fns == 0
