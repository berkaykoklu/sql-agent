import json

from sqlagent.catalog import column_note, from_bird_csv, load, table_note


def test_bird_csv_becomes_a_catalog_with_case_insensitive_lookup(db):
    c = from_bird_csv(db.parent / "database_description")
    assert column_note(c, "ITEMS", "note") == 'values: "x" stands for unknown'
    assert column_note(c, "items", "NAME") == "name of the fruit"
    assert column_note(c, "items", "id") == "" and column_note(c, "nope", "x") == ""


def test_user_catalog_loads_from_json(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"tables": {"Orders": {"description": "one row per order",
                                                      "columns": {"amount": "total in EUR"}}}}))
    c = load(path)
    assert table_note(c, "orders") == "one row per order" and column_note(c, "ORDERS", "Amount") == "total in EUR"
    assert table_note(None, "orders") == "" and column_note(None, "orders", "amount") == ""


def test_text_under_an_unnamed_trailing_column_is_kept(tmp_path):
    # BIRD's financial/account.csv has an extra empty header field that holds the value codes
    (tmp_path / "account.csv").write_text(
        "original_column_name,column_name,column_description,data_format,value_description,\n"
        'frequency,frequency,frequency of the acount,text,,"""POPLATEK PO OBRATU"" stands for issuance after transaction"\n')
    note = column_note(from_bird_csv(tmp_path), "account", "frequency")
    assert note == 'frequency of the acount; values: "POPLATEK PO OBRATU" stands for issuance after transaction'
