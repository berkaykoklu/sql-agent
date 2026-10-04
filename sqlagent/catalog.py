"""What the user tells the agent about their data: {"tables": {name: {"description": str, "columns": {col: str}}}}."""
import csv
import io
import json
import sys
from pathlib import Path

Catalog = dict


def load(path: Path | str) -> Catalog:
    return json.loads(Path(path).read_text())


def _entry(catalog: Catalog | None, table: str) -> dict:
    tables = (catalog or {}).get("tables", {})
    return next((v for k, v in tables.items() if k.lower() == table.lower()), {})


def table_note(catalog: Catalog | None, table: str) -> str:
    return _entry(catalog, table).get("description", "")


def column_note(catalog: Catalog | None, table: str, column: str) -> str:
    columns = _entry(catalog, table).get("columns", {})
    return next((v for k, v in columns.items() if k.lower() == column.lower()), "")


def from_bird_csv(folder: Path | str) -> Catalog:
    tables = {}
    for path in sorted(Path(folder).glob("*.csv")):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")  # 4 BIRD files are not UTF-8
        columns = {}
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("original_column_name") or "").strip()
            parts: list[str] = []
            for key in ("column_name", "column_description"):
                value = " ".join((row.get(key) or "").split())
                if value and value.lower() != name.lower() and value not in parts:
                    parts.append(value)
            values = " ".join((row.get("value_description") or "").split())
            if values:
                parts.append(f"values: {values}")
            if name and parts:
                columns[name] = "; ".join(parts)
        tables[path.stem] = {"columns": columns}  # file names don't always match the table's case
    return {"tables": tables}


if __name__ == "__main__":
    json.dump(from_bird_csv(sys.argv[1]), sys.stdout, indent=1, ensure_ascii=False)
