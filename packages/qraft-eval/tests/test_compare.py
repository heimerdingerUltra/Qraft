from datetime import date, datetime
from decimal import Decimal

from qraft_eval.compare import compare_results, normalize_value


def test_numbers_and_dates_are_normalized():
    assert normalize_value(Decimal("12.50")) == normalize_value(12.5) == normalize_value(12.500000000001)
    assert normalize_value(3) == normalize_value(3.0)
    assert normalize_value(datetime(2025, 1, 1)) == normalize_value(date(2025, 1, 1)) == "2025-01-01"
    assert normalize_value(None) is None


def test_unordered_comparison_ignores_row_order():
    gold = [("IDF", 10), ("ARA", 5)]
    pred = [("ARA", 5), ("IDF", 10)]
    assert compare_results(gold, pred, ordered=False).match


def test_ordered_comparison_detects_wrong_order():
    gold = [("IDF", 10), ("ARA", 5)]
    pred = [("ARA", 5), ("IDF", 10)]
    result = compare_results(gold, pred, ordered=True)
    assert not result.match
    assert "ordre" in result.reason


def test_column_permutation_is_accepted():
    gold = [("IDF", 10.0, 3), ("ARA", 5.0, 1)]
    pred = [(3, "IDF", Decimal("10")), (1, "ARA", Decimal("5"))]
    result = compare_results(gold, pred, ordered=False)
    assert result.match
    assert "colonnes" in result.reason


def test_row_and_column_count_mismatches():
    assert "lignes" in compare_results([(1,)], [(1,), (2,)], ordered=False).reason
    assert "colonnes" in compare_results([(1, 2)], [(1,)], ordered=False).reason


def test_duplicates_matter():
    assert not compare_results([(1,), (1,)], [(1,), (2,)], ordered=False).match


def test_empty_results_match():
    assert compare_results([], [], ordered=False).match
