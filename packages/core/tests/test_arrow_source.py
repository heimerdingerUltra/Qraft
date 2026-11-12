import sqlite3

import pytest

from qraft.execute import ArrowSource, Source
from qraft.verify import same_results


def test_duckdb_arrow_source_matches_sqlalchemy(demo_url):
    pytest.importorskip("pyarrow")
    sql = "SELECT segment, COUNT(*) AS n FROM clients GROUP BY segment ORDER BY n DESC"
    arrow_res = ArrowSource(demo_url).query(sql)
    plain_res = Source(demo_url).query(sql)
    assert arrow_res.arrow is not None and arrow_res.to_arrow().num_rows == 3
    assert arrow_res.rows == plain_res.rows  # ordre conservé


def test_row_cap_is_pushed_down(demo_url):
    res = ArrowSource(demo_url).query("SELECT * FROM lignes_commande", max_rows=10)
    assert len(res.rows) == 10 and res.truncated


def test_connectorx_on_sqlite(demo_url, tmp_path):
    pytest.importorskip("connectorx")
    rows = Source(demo_url).query("SELECT client_id, nom, segment FROM clients").rows
    path = tmp_path / "clients.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE clients (client_id INTEGER PRIMARY KEY, nom TEXT, segment TEXT)")
    con.executemany("INSERT INTO clients VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    source = ArrowSource(f"sqlite:///{path.as_posix()}")
    assert source._backend() == "connectorx"
    res = source.query("SELECT segment, COUNT(*) AS n FROM clients GROUP BY segment")
    expected = Source(f"sqlite:///{path.as_posix()}").query("SELECT segment, COUNT(*) AS n FROM clients GROUP BY segment")
    assert same_results(res.rows, expected.rows)
