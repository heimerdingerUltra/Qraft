import pytest

from qraft_eval.safety import UnsafeSQL, check_read_only, has_top_level_order_by


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
        "SELECT a FROM t UNION ALL SELECT a FROM u",
    ],
)
def test_reads_are_accepted(sql):
    check_read_only(sql, "duckdb")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM clients",
        "DROP TABLE clients",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "SELECT 1; DELETE FROM clients",
        "SELECT * FROM read_csv('secret.csv')",
        "SELECT pg_sleep(10)",
        "CREATE TABLE x AS SELECT 1",
    ],
)
def test_writes_and_side_effects_are_rejected(sql):
    with pytest.raises(UnsafeSQL):
        check_read_only(sql, "duckdb")


def test_select_into_is_rejected_in_postgres():
    with pytest.raises(UnsafeSQL):
        check_read_only("SELECT * INTO copie FROM clients", "postgres")


def test_order_by_detection():
    assert has_top_level_order_by(check_read_only("SELECT a FROM t ORDER BY a", "duckdb"))
    assert not has_top_level_order_by(check_read_only("SELECT a FROM (SELECT a FROM t ORDER BY a) s", "duckdb"))
