import sqlite3

import pytest


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "my dir" / "shop.sqlite"  # the real repo path has a space too
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, note TEXT);
        INSERT INTO items VALUES (1, 'apple', 'red'), (2, 'pear', 'green'),
                                 (3, 'fig', 'x'), (4, 'kiwi', 'y');
        INSERT INTO items VALUES (5, 'bad', CAST(x'ff' AS TEXT));
        CREATE TABLE "order lines" (item_id INTEGER, qty INTEGER);
        INSERT INTO "order lines" VALUES (1, 2);
        """
    )
    conn.execute("INSERT INTO items VALUES (6, 'long', ?)", ("a" * 500,))
    conn.commit()
    conn.close()
    return path
