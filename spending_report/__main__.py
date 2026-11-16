"""소비습관 개선 보고서 생성.

사용법:
    python -m spending_report kim_doyun [--version v2] [--provider none|local|server] [--model Qwen/Qwen3-4B-Instruct-2507]

output/에 spending_report_*.json, spending_report_*.html, ai_log_*.json을 쓰고 module_results(module='spending')에 저장한다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .inputs import load_input
from .llm import get_client
from .render import to_html
from .report import build_report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_id")
    parser.add_argument("--version")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--out", default=Path("output"), type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    inp = load_input(conn, args.case_id, args.version)
    client = get_client(args.provider, args.model)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{inp.case_id}_{inp.profile_version}"
    log_path = args.out / f"ai_log_{stem}.json"
    report, ai_log = build_report(inp, client, log_file=log_path.name)

    json_path = args.out / f"spending_report_{stem}.json"
    html_path = args.out / f"spending_report_{stem}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(to_html(report), encoding="utf-8")
    log_path.write_text(json.dumps(ai_log, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.execute("INSERT OR REPLACE INTO module_results VALUES (?,?,?,?,?,?)",
                 (inp.case_id, inp.profile_version, "spending", report["status"],
                  json.dumps(report, ensure_ascii=False), report["generated_at"]))
    conn.commit()

    effect = report["calculations"]["effect"]
    print(json.dumps({"status": report["status"], "json": str(json_path), "html": str(html_path), "ai_log": str(log_path),
                      "monthly_freed_won": effect["monthly_freed_won"], "annual_freed_won": effect["annual_freed_won"],
                      "documents": {k: v["status"] for k, v in report["evidence"]["documents"].items()},
                      "sentences": {"llm": report["ai_usage"]["sentences_llm"],
                                    "template": report["ai_usage"]["sentences_template"]}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
