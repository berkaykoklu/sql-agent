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
        CREATE TABLE "order lines" (item_id INTEGER REFERENCES items(id), qty INTEGER);
        INSERT INTO "order lines" VALUES (1, 2);
        CREATE TABLE regions (name TEXT);
        INSERT INTO regions VALUES ('east Bohemia'), ('east Bohemia'), ('north Bohemia'),
                                   ('Prague'), ('50%_off');
        """
    )
    conn.execute("INSERT INTO items VALUES (6, 'long', ?)", ("a" * 500,))
    conn.commit()
    conn.close()
    notes = path.parent / "database_description"
    notes.mkdir()
    (notes / "Items.csv").write_text(  # file-name case differs from the table, like BIRD's student_club
        "original_column_name,column_name,column_description,data_format,value_description\n"
        "id,,,integer,\n"
        "name,name,name of the fruit,text,\n"
        'note,note,,text,"""x"" stands for\nunknown"\n',
        encoding="utf-8",
    )
    return path
