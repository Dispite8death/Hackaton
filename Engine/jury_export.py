"""Build a self-contained HTML snapshot using only public report fields."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from . import config, dashboard_data
from ai_audit_web import reports

ROOT = Path(__file__).resolve().parents[1]


def fields(row, names):
    return {name:row.get(name) for name in names.split()}


def snapshot():
    uri = Path(config.DB_PATH).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        data = dashboard_data.dashboard(conn)
    orgs = [fields(row,"id name site_url kind score quality coverage") for row in data["institutions"]]
    checks = [fields(row,"organization_id title status severity detector category description recommendation url") for row in data["findings"]]
    quick = []
    for row in reports.latest():
        item = fields(row["site"],"name url type")
        item.update(fields(row["result"],"score finished_at llm_summary ai_explanation"))
        item["findings"] = [fields(f,"message severity recommendation url") for f in row["findings"]]
        quick.append(item)
    return {"when":data["as_of"],"orgs":orgs,"checks":checks,"quick":quick,
            "labels":dashboard_data.LABELS, "levels":reports.LEVELS}


def write_snapshot(target):
    data = snapshot()
    template = (ROOT/"web"/"jury.html").read_text(encoding="utf8")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",",":")).replace("<","\\u003c")
    target.write_text(template.replace("<!--SNAPSHOT-->",encoded),encoding="utf8")
    return len(data["orgs"]),len(data["checks"]),len(data["quick"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(write_snapshot(args.output))
