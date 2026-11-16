"""보고서 입력 읽기. 고객 프로필과 정책 요약본은 반드시 같은 profile_version을 쓴다."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class ReportInput:
    case_id: str
    profile_version: str
    as_of_date: str
    is_synthetic: bool
    customer_type: str
    facts: dict[str, dict]
    income_cashflow: list[dict]
    income_items: list[dict]
    cashflow: list[dict]
    assets: list[dict]
    liabilities: list[dict]
    tax_items: list[dict]
    goals: list[dict]
    handoff: dict

    def fact(self, field: str):
        return self.facts.get(field, {}).get("value")


def _rows(conn: sqlite3.Connection, sql: str, key: tuple) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, key)]


def load_input(conn: sqlite3.Connection, case_id: str, version: str | None = None) -> ReportInput:
    conn.row_factory = sqlite3.Row
    if version is None:
        found = conn.execute("SELECT profile_version FROM module_results WHERE case_id=? AND module='policy_to_spending' "
                             "ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        if found is None:
            raise SystemExit(f"{case_id}의 정책 요약본이 없습니다. 먼저 실행하세요:\n"
                             f"  python -m customer.policy_match {case_id}\n"
                             f"  python -m customer.spending_handoff {case_id}")
        version = found[0]
    key = (case_id, version)
    profile = conn.execute("SELECT * FROM profiles WHERE case_id=? AND profile_version=?", key).fetchone()
    handoff = conn.execute("SELECT result_json FROM module_results WHERE case_id=? AND profile_version=? "
                           "AND module='policy_to_spending'", key).fetchone()
    if profile is None or handoff is None:
        raise SystemExit(f"{key}의 프로필 또는 정책 요약본이 없습니다.")

    facts = {}
    for r in conn.execute("SELECT * FROM profile_facts WHERE case_id=? AND profile_version=?", key):
        value = r["value_num"] if r["value_num"] is not None else {"true": True, "false": False}.get(r["value_text"], r["value_text"])
        facts[r["field"]] = {"value": value, "status": r["status"], "source_ref": r["source_ref"]}

    where = "WHERE case_id=? AND profile_version=? ORDER BY rowid"
    income_items = _rows(conn, f"SELECT * FROM income_items {where}", key)
    return ReportInput(
        case_id=case_id, profile_version=version, as_of_date=profile["as_of_date"],
        is_synthetic=bool(profile["is_synthetic"]), customer_type=profile["customer_type"], facts=facts,
        income_cashflow=[r for r in income_items if r["in_cashflow"]], income_items=income_items,
        cashflow=_rows(conn, f"SELECT * FROM cashflow_items {where}", key),
        assets=_rows(conn, f"SELECT * FROM assets {where}", key),
        liabilities=_rows(conn, f"SELECT * FROM liabilities {where}", key),
        tax_items=_rows(conn, f"SELECT * FROM tax_items {where}", key),
        goals=_rows(conn, f"SELECT * FROM goals {where}", key),
        handoff=json.loads(handoff["result_json"]),
    )
