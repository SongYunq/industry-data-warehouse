"""Install the versioned H&M analysis views in Doris."""

from __future__ import annotations

import re

from warehouse.config import load_settings
from warehouse.db import connect_doris

DATABASE = "hm_retail_dw"
VIEW_HEADER = re.compile(r"CREATE VIEW IF NOT EXISTS (\w+) AS", re.IGNORECASE)


def main() -> None:
    settings = load_settings()
    path = settings.project_root / "sql" / "hm_recommendations" / "002_analytics.sql"
    script = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                       if not line.lstrip().startswith("--"))
    views = [statement.strip() for statement in script.split(";") if statement.strip()]
    with connect_doris(settings, DATABASE) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
        existing = {next(iter(row.values())) for row in cursor.fetchall()}
        for statement in views:
            header = VIEW_HEADER.match(statement)
            if header is None:
                raise ValueError("分析脚本含无法识别的视图定义")
            name = header[1]
            if name in existing:
                statement = f"ALTER VIEW {name} AS" + statement[header.end():]
            cursor.execute(statement)
            print(name)


if __name__ == "__main__":
    main()
