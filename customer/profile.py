"""(라) 고객 입력: 케이스 JSON을 읽어 검증하고 customer_profiles.db에 버전별로 저장한다.

조립 설명서 규칙:
- 금액은 원 단위. 값마다 status / source_ref / assumptions를 함께 보관한다.
- 모르는 값은 None + '미확인'. 0원은 확인된 0원일 때만.
- 같은 돈을 두 번 세지 않는다(지급처별 매출 vs 총수입금액, 저축 이체 vs 자산 잔액).
- 정책 판정용 값은 policy_inputs()가 소득 지표별로 따로 낸다. 매출을 총급여로 바꿔 넣지 않는다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
STATUSES = {"기재", "확인", "추정", "불일치", "미확인", "해당없음"}

_TABLE_KEYS = {
    "income_items": ["source_id", "kind", "payer", "basis", "period", "tax_year", "amount_won", "low_won", "high_won", "in_cashflow"],
    "tax_items": ["tax_year", "filing", "item", "period", "amount_won"],
    "assets": ["asset_id", "kind", "name", "institution", "opened_on", "matures_on", "principal_won", "balance_won",
               "monthly_contribution_won", "rate_pct", "earmark", "liquidity"],
    "liabilities": ["liability_id", "kind", "institution", "principal_won", "balance_won", "started_on", "matures_on",
                    "repayment", "rate_pct", "monthly_payment_won"],
    "cashflow_items": ["section", "item", "monthly_won", "low_won", "high_won", "purpose", "frequency", "necessity",
                       "linked_asset_id", "detail"],
    "insurance_contracts": ["contract_id", "name", "contractor", "insured", "payer", "monthly_premium_won",
                            "coverage_summary", "started_on", "pay_term", "coverage_end", "renewal", "surrender_value_won"],
    "goals": ["goal_id", "term", "name", "due", "amount_won", "amount_basis", "value_basis", "priority"],
}
# 표마다 source_ref가 없을 때 쓸 기본 출처 키
_SOURCE_DEFAULT = {"tax_items": "tax_source", "cashflow_items": "cashflow_source"}


def load_case(path: str | Path) -> dict:
    """케이스 JSON → 기본값(status '기재', 출처 파일)을 채운 프로필 dict."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    default_src = raw.get("default_source", "")
    profile = {k: raw[k] for k in ("case_id", "profile_version", "as_of_date", "is_synthetic")}
    profile["note"] = raw.get("note")
    profile["facts"] = {}
    for field, fact in raw.get("facts", {}).items():
        value = fact.get("value")
        profile["facts"][field] = {
            "value": value,
            "unit": fact.get("unit"),
            "status": fact.get("status") or ("미확인" if value is None else "기재"),
            "source_ref": _ref(default_src, fact.get("source_ref")),
            "assumptions": fact.get("assumptions"),
        }
    for table, cols in _TABLE_KEYS.items():
        table_src = raw.get(_SOURCE_DEFAULT.get(table, ""), default_src)
        rows = []
        for item in raw.get(table, []):
            row = {c: item.get(c) for c in cols}
            amount = item.get("amount_won", item.get("monthly_won", item.get("balance_won")))
            row["status"] = item.get("status") or ("미확인" if amount is None and "amount_won" in item else "기재")
            row["source_ref"] = _ref(table_src, item.get("source_ref"))
            row["assumptions"] = item.get("assumptions")
            if table == "income_items":
                row["in_cashflow"] = int(bool(row["in_cashflow"]))
            if table == "tax_items":
                row["period"] = row["period"] or "연"
            if table == "goals":  # 문서 원문(있으면). DB 열에는 없고 계획 표시용으로만 둔다
                row["raw"] = item.get("raw")
            rows.append(row)
        profile[table] = rows
    return profile


def _ref(file_ref: str, where: str | None) -> str:
    if where and where.startswith("가상고객데이터/"):
        return where
    return f"{file_ref} {where}".strip() if where else file_ref


def fact(profile: dict, field: str):
    return profile["facts"].get(field, {}).get("value")


def customer_type(profile: dict) -> str:
    kinds = {r["kind"] for r in profile["income_items"]}
    if {"근로", "사업"} <= kinds:
        return "겸업"
    if "사업" in kinds:
        return "사업·프리랜서"
    if "근로" in kinds:
        return "근로"
    return "미확인"


# ── 검증 ──────────────────────────────────────────────────────
def validate(profile: dict) -> list[dict]:
    """조립 설명서 10장 검증 목록 중 입력 단계에서 볼 수 있는 것. level: error / warning."""
    issues = []

    def add(level, check, message):
        issues.append({"level": level, "check": check, "message": message})

    for field, f in profile["facts"].items():
        if f["status"] not in STATUSES:
            add("error", "status", f"facts.{field}: 알 수 없는 상태 '{f['status']}'")
    for table in _TABLE_KEYS:
        for row in profile[table]:
            if row["status"] not in STATUSES:
                add("error", "status", f"{table}: 알 수 없는 상태 '{row['status']}'")

    # 현금흐름: 월 유입 = 월 유출(저축 포함)
    inflow = sum(r["amount_won"] or 0 for r in profile["income_items"] if r["in_cashflow"])
    outflow = sum(r["monthly_won"] or 0 for r in profile["cashflow_items"])
    flows = [(r["amount_won"], r["low_won"], r["high_won"]) for r in profile["income_items"] if r["in_cashflow"]]
    flows += [(r["monthly_won"], r["low_won"], r["high_won"]) for r in profile["cashflow_items"]]
    incomplete = any(won is None or low != high for won, low, high in flows)  # 빈칸이나 범위가 있으면 합이 맞을 수 없다
    if inflow and abs(inflow - outflow) > 100:
        add("warning" if incomplete else "error", "cashflow_balance",
            f"월 유입 {inflow:,}원 ≠ 월 유출 {outflow:,}원 (차이 {inflow - outflow:,}원)")
    elif not inflow:
        add("warning", "cashflow_balance", "현금흐름 유입 행(in_cashflow)이 없음")

    # 총자산: 자산 행 합계 = 문서 기재 총자산(같은 돈 이중 계상 여부)
    stated = fact(profile, "stated_total_assets")
    total = sum(r["balance_won"] or 0 for r in profile["assets"])
    if stated is not None and total != stated:
        add("error", "asset_total", f"자산 합계 {total:,}원 ≠ 기재 총자산 {stated:,}원")

    stated_debt = fact(profile, "stated_total_liabilities")
    debt = sum(r["balance_won"] or 0 for r in profile["liabilities"])
    if stated_debt is not None and debt != stated_debt:
        add("error", "liability_total", f"부채 합계 {debt:,}원 ≠ 기재 부채 {stated_debt:,}원")

    # 지급처별 매출 합계 = 총수입금액 (SNS 등 중복 가산 방지)
    by_year: dict[tuple, int] = {}
    totals: dict[tuple, int] = {}
    for r in profile["income_items"]:
        key = (r["source_id"], r["tax_year"])
        if r["basis"] == "매출(지급처별)":
            by_year[key] = by_year.get(key, 0) + (r["amount_won"] or 0)
        if r["basis"] == "총수입금액":
            totals[key] = r["amount_won"] or 0
    for key, s in by_year.items():
        if key in totals and s != totals[key]:
            add("error", "revenue_breakdown", f"{key}: 지급처별 합계 {s:,}원 ≠ 총수입금액 {totals[key]:,}원")

    # 저축 이체가 가리키는 자산이 있어야 한다(이체액과 잔액을 따로 세지 않도록 연결)
    asset_ids = {a["asset_id"] for a in profile["assets"]}
    for r in profile["cashflow_items"]:
        if r["linked_asset_id"] and r["linked_asset_id"] not in asset_ids:
            add("error", "linked_asset", f"현금흐름 '{r['item']}'의 연결 자산 {r['linked_asset_id']} 없음")
        if r["section"] == "저축" and (r["monthly_won"] or 0) > 0 and not r["linked_asset_id"]:
            add("warning", "linked_asset", f"저축 '{r['item']}'에 연결 자산이 없음")

    # 판정에 꼭 필요한데 비어 있는 항목은 질문으로 넘긴다
    for field in ("birth_date", "region_code", "household_size", "home_owner", "marital_status"):
        if fact(profile, field) is None:
            add("warning", "missing", f"정책 판정 필수 항목 '{field}' 미확인")
    return issues


# ── 정책 판정용 입력 ─────────────────────────────────────────
INCOME_METRICS = {
    # 정책 소득 지표 → (소득 종류, basis)
    "총급여": ("근로", "총급여"),
    "근로소득금액": ("근로", "근로소득금액"),
    "사업 총수입금액": ("사업", "총수입금액"),
    "사업소득금액": ("사업", "사업소득금액"),
}


def age_on(birth: str, on: str) -> int:
    b, d = date.fromisoformat(birth), date.fromisoformat(on)
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def policy_inputs(profile: dict, on: str | None = None, median_monthly: dict[int, int] | None = None) -> dict:
    """정책 매칭에 넘길 값. 지표별 소득과 중위소득 비율을 따로 내고, 못 정한 값은 missing/questions로 돌린다."""
    on = on or profile["as_of_date"]
    birth = fact(profile, "birth_date")
    hh = fact(profile, "household_size")
    out = {
        "case_id": profile["case_id"], "profile_version": profile["profile_version"], "as_of": on,
        "age": age_on(birth, on) if birth else None,
        "region_code": fact(profile, "region_code"),
        "residence_since": fact(profile, "residence_since"),
        "household_size": hh,
        "marital_status": fact(profile, "marital_status"),
        "home_owner": fact(profile, "home_owner"),
        "employment_status": fact(profile, "employment_status"),
        "customer_type": customer_type(profile),
        "income_metrics": {},
        "missing_fields": [], "questions": [],
    }
    for metric, (kind, basis) in INCOME_METRICS.items():
        rows = [r for r in profile["income_items"] if r["kind"] == kind and r["basis"] == basis and r["period"] == "연"]
        if not rows:
            continue
        latest = max(rows, key=lambda r: r["tax_year"] or 0)
        entry = {"annual_won": latest["amount_won"], "tax_year": latest["tax_year"], "status": latest["status"]}
        if median_monthly and hh and latest["amount_won"] is not None:
            entry["median_pct_of_household"] = round(latest["amount_won"] / 12 / median_monthly[int(hh)] * 100, 1)
        out["income_metrics"][metric] = entry

    # 정책 판정에서 자주 쓰지만 이 입력만으로 못 정하는 값
    if out["customer_type"] in ("사업·프리랜서", "겸업"):
        out["questions"].append("종합소득금액(다른 소득 합산)과 정책별 소득 기준연도를 확인해주세요")
    for field, question in (
        ("military_service", "병역 이행 기간(청년 정책 연령 차감)을 알려주세요"),
        ("parents_household", "원가구(부모) 소득·재산 기준 정책 판정을 위해 원가구 정보를 알려주세요"),
        ("employer_size", "재직 회사가 중소기업인지 알려주세요"),
    ):
        if field == "employer_size" and out["customer_type"] not in ("근로", "겸업"):
            continue  # 재직 회사 질문은 근로소득이 있을 때만
        f = profile["facts"].get(field)
        if f is None or f["status"] == "미확인" or field == "parents_household" and f["value"] == "별도 거주":
            out["missing_fields"].append(field)
            out["questions"].append(question)
    if out["residence_since"] and profile["facts"]["residence_since"]["status"] == "추정":
        out["questions"].append("현 시군구 전입일(지자체 거주기간 요건 판정)을 알려주세요")
    if not any(t["item"].startswith("건강") and t["period"] == "월" for t in profile["tax_items"]):
        out["questions"].append("건강보험료 월 납부액(건보료 기준 정책 판정)을 알려주세요")
    return out


# ── 저장 ──────────────────────────────────────────────────────
def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table in ("income_items", "cashflow_items"):  # 범위 칸이 없던 기존 DB 보완
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column in {"low_won", "high_won"} - existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
    return conn


def store(conn: sqlite3.Connection, profile: dict) -> None:
    """같은 (case_id, profile_version)이 있으면 그 버전만 다시 쓴다. 다른 버전은 보존한다."""
    key = (profile["case_id"], profile["profile_version"])
    for table in ["profile_facts", *_TABLE_KEYS, "module_results", "profiles"]:
        conn.execute(f"DELETE FROM {table} WHERE case_id=? AND profile_version=?", key)
    conn.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?)",
                 (*key, profile["as_of_date"], int(profile["is_synthetic"]), customer_type(profile),
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile.get("note")))
    for field, f in profile["facts"].items():
        v = f["value"]
        num = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        text = None if num is not None or v is None else (str(v).lower() if isinstance(v, bool) else str(v))
        conn.execute("INSERT INTO profile_facts VALUES (?,?,?,?,?,?,?,?,?)",
                     (*key, field, text, num, f["unit"], f["status"], f["source_ref"], f["assumptions"]))
    for table, cols in _TABLE_KEYS.items():
        all_cols = ["case_id", "profile_version", *cols, "status", "source_ref", "assumptions"]
        conn.executemany(
            f"INSERT INTO {table} ({','.join(all_cols)}) VALUES ({','.join('?' * len(all_cols))})",
            [(*key, *[r[c] for c in cols], r["status"], r["source_ref"], r["assumptions"]) for r in profile[table]])
    conn.commit()
