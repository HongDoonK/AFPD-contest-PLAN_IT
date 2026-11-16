"""4대 생애목표(주거·결혼·양육·노후) 자산 분배 비율.

step2 JSON(만원 단위 p·q·r·s·M·deltaM)과 고객 프로필(facts)로
가용 자산 → 비상금 선공제 → 목표별 필요자금·부족액 → 분배 비율·금액 → 달성 가능성 표시까지
결정적으로 계산한다. 조정(목표 축소·기한 연장)은 하지 않고 표시만 한다.

v2: 목표 시점은 고객 목표 기한 우선, 양육 years = 출산까지 + 지출기간/2, 양육비 물가 환산,
    노후는 연금 개시(65세) 전후를 나눠 계산, 수도권 주담대 상한 반영.

사용법:
    python -X utf8 -m planning.allocation --step2 planning/cases/kim_doyun_step2.json \
        --case customer/cases/kim_doyun.json --out output/allocation_kim_doyun_v2.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date
from pathlib import Path

from customer.profile import age_on, fact, load_case

from .reference import REFERENCE

LOCKED = {"p": "주택청약", "q": "IRP", "r": "연금저축", "s": "변액보험"}
GOALS = ("housing", "wedding", "child", "retire")
GOAL_NAMES = {"housing": "주거", "wedding": "결혼", "child": "양육", "retire": "노후"}


# ── 작은 도구 ─────────────────────────────────────────────────
def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _num(x) -> float:
    return float(x or 0)


def _item_amount(text: str) -> float | None:
    """'정기예금 1500' → 1500. 문자열 끝의 숫자를 금액으로 본다."""
    found = re.findall(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(found[-1]) if found else None


def _by_sex(table: dict, sex: str | None, warnings: list, label: str) -> float:
    if sex in table:
        return table[sex]
    vals = [v for k, v in table.items() if k in ("남", "여")]
    warnings.append(f"{label}: 성별 미확인 → 남·여 평균 {sum(vals) / len(vals):.1f}세 사용")
    return sum(vals) / len(vals)


def _norm_sex(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    return "남" if s[0] in "남Mm" else "여" if s[0] in "여Ff" else None


def largest_remainder(weights: dict, total_units: int) -> dict:
    """가중치를 정수 단위(total_units)로 나누되 합이 정확히 total_units가 되게(최대 잔여법)."""
    wsum = sum(weights.values())
    raw = {k: w / wsum * total_units for k, w in weights.items()}
    base = {k: math.floor(v) for k, v in raw.items()}
    left = total_units - sum(base.values())
    for k in sorted(raw, key=lambda k: (raw[k] - base[k], weights[k]), reverse=True)[:left]:
        base[k] += 1
    return base


# ── 1. 가용 현금화 자산 ──────────────────────────────────────
def m_available(step2: dict, m_includes_locked: bool, warnings: list) -> dict:
    M = _num(step2.get("M"))
    locked_sum = sum(_num(step2.get(k)) for k in LOCKED)
    items = step2.get("_meta", {}).get("asset_detail", {}).get("M_items")
    items_sum = None
    if items is not None:
        amounts = [_item_amount(t) for t in items]
        if any(a is None for a in amounts):
            warnings.append("M_items 중 금액을 읽지 못한 항목이 있음")
        items_sum = sum(a for a in amounts if a is not None)
        if abs(items_sum - M) > 0.05:
            warnings.append(f"M_items 합 {items_sum:,.1f} ≠ M {M:,.1f}")
    excl, incl = M, M - locked_sum
    value = incl if m_includes_locked else excl
    if value < 0:
        warnings.append(f"M_avail이 음수({value:,.1f}) → 0으로 봄")
    return {
        "value": max(value, 0.0),
        "interpretation": "M에 p·q·r·s 포함 → M − (p+q+r+s)" if m_includes_locked
        else "M은 이미 p·q·r·s를 뺀 금액 → M_avail = M",
        "m_includes_locked": m_includes_locked,
        "if_m_excludes_locked": _r(excl), "if_m_includes_locked": _r(incl),
        "M": M, "locked_sum": _r(locked_sum), "M_items_sum": _r(items_sum),
    }


# ── 2. 보존 자산 표 ─────────────────────────────────────────
def locked_assets(step2: dict, as_of: str) -> list[dict]:
    rows = []
    for k, name in LOCKED.items():
        d = step2.get(f"{k}_date")
        elapsed = None
        if d:
            elapsed = round((date.fromisoformat(as_of) - date.fromisoformat(d)).days / 365.25, 2)
        rows.append({"key": k, "name": name, "balance": step2.get(k), "rate_pct": step2.get(f"{k}_rate"),
                     "opened_on": d, "elapsed_years": elapsed, "annual_contribution": step2.get(f"delta{k}")})
    return rows


# ── 3. 비상예비자금 ─────────────────────────────────────────
def emergency_fund(step2: dict, m_avail: float, multiple: float, low_multiple: float, warnings: list) -> dict:
    income = step2.get("_meta", {}).get("deltaM_detail", {}).get("income_monthly")
    if income is None:
        warnings.append("월 소득(income_monthly) 없음 → 비상금 0으로 계산")
        income = 0.0
    need = income * multiple
    held = min(need, m_avail)
    if need > m_avail:
        warnings.append(f"비상금 필요액 {need:,.1f}이 M_avail {m_avail:,.1f}보다 큼 → M_avail까지만 공제, 분배 대상 0")
    return {"income_monthly": income, "multiple": multiple, "need": _r(need),
            "range": [_r(income * low_multiple), _r(income * multiple)],
            "amount": _r(held), "capped": need > m_avail}


# ── 4-a. 목표 시점: 고객 목표 기한 우선, 없으면 통계 연령 ──────────
# 고객 goals의 이름에 아래 낱말이 들어 있으면 그 목표로 본다(앞선 목표 키가 우선).
GOAL_KEYWORDS = {
    "housing": ("주거", "주택", "내집", "내 집"),
    "wedding": ("결혼", "웨딩", "혼인"),
    "child": ("양육", "자녀", "육아", "출산"),
    "retire": ("은퇴", "노후"),
}


def _due_years(due, age: int, as_of: str):
    """기한 문자열 → (기준일부터 연수, 그때 나이). 'YYYY-MM-DD'·'YYYY-MM'·'YYYY'·'N세'. 못 읽으면 None."""
    s = str(due or "").strip()
    d = date.fromisoformat(as_of)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        years = (date(*map(int, m.groups())) - d).days / 365.25
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        years = ((int(m[1]) - d.year) * 12 + int(m[2]) - d.month) / 12
    elif m := re.fullmatch(r"(\d{4})", s):
        years = int(m[1]) - d.year
    elif m := re.search(r"(\d+(?:\.\d+)?)\s*세", s):
        return float(m[1]) - age, float(m[1])
    else:
        return None
    return years, age + years


def customer_goal(profile: dict, key: str) -> dict | None:
    """프로필 goals에서 key에 해당하는 첫 목표(기한이 가장 이른 것). 다른 키에 먼저 걸린 목표는 건너뛴다."""
    hits = []
    for g in profile.get("goals") or []:
        name = str(g.get("name") or "")
        owner = next((k for k, words in GOAL_KEYWORDS.items() if any(w in name for w in words)), None)
        if owner == key and g.get("due"):
            word = next(w for w in GOAL_KEYWORDS[key] if w in name)
            hits.append({**g, "_rule": f"목표 이름에 '{word}' 포함"})
    return min(hits, key=lambda g: str(g["due"])) if hits else None


def horizon(profile, key, stat_age, stat_key, age, as_of, warnings) -> dict:
    """{'target_age','years_raw','source'}. 고객 기한이 있으면 그것, 없으면 통계 연령."""
    g = customer_goal(profile, key)
    if g:
        parsed = _due_years(g["due"], age, as_of)
        if parsed:
            years, target = parsed
            return {"target_age": _r(target), "years_raw": years,
                    "source": {"source": "고객 목표", "goal_id": g.get("goal_id"), "goal_name": g.get("name"),
                               "due": g["due"], "rule": g["_rule"]}}
        warnings.append(f"{GOAL_NAMES[key]}: 고객 목표 기한 '{g['due']}'을 읽지 못함 → 통계 연령 사용")
    return {"target_age": stat_age, "years_raw": None if stat_age is None else stat_age - age,
            "source": {"source": "통계 연령", "ref_key": stat_key, "rule": "고객 목표에 해당 기한 없음"}}


# ── 4-b. 목표별 필요자금 ────────────────────────────────────
def _goal(key, need, existing, hz, active, basis, warnings, note=None, extra=None):
    gap = None if need is None else max(need - existing, 0.0)
    years = None if hz["years_raw"] is None else max(hz["years_raw"], 1)
    if active and hz["years_raw"] is not None and hz["years_raw"] < 1:
        warnings.append(f"{GOAL_NAMES[key]}: 목표 시점까지 {hz['years_raw']:.2f}년(1년 미만) → years=1")
    if active and need is None:
        warnings.append(f"{GOAL_NAMES[key]}: 기준값이 없어 필요자금을 계산하지 못함 → 분배에서 제외")
        active = False
        note = (note + "; " if note else "") + "기준값 없음"
    g = {"goal": key, "name": GOAL_NAMES[key], "need": _r(need), "existing": _r(existing), "gap": _r(gap),
         "target_age": hz["target_age"], "years": _r(years), "horizon_source": hz["source"],
         "active": active, "note": note, "basis": basis}
    g.update(extra or {})
    return g


def housing_goal(step2, profile, sido, home_owner, age, as_of, ref, opt, warnings):
    prices = ref["house_price_sido"]["value"]
    price, price_key = prices.get(sido) if sido else None, "house_price_sido"
    note = None
    if price is None:
        price, price_key = ref["house_price_national"]["value"], "house_price_national"
        note = f"시도({sido}) 평균가 없음 → 전국값 대체"
        warnings.append(f"주거: {note}" + ("" if price is not None else ", 전국값도 없음"))
    ltv, cost = opt["ltv"], opt["purchase_cost_rate"]
    cap = ref["mortgage_cap"]["value"] if sido in (ref["mortgage_cap_regions"]["value"] or []) else None
    loan = need = None
    if price is not None:
        loan = price * ltv if cap is None else min(price * ltv, cap)
        need = price - loan + price * cost
    active = home_owner is not True
    if home_owner is None:
        warnings.append("주거: 주택 보유 여부 미확인 → 무주택으로 봄")
    if home_owner is True:
        note = "주택 보유 → 제외"
    hz = horizon(profile, "housing", ref["first_home_age"]["value"], "first_home_age", age, as_of, warnings)
    return _goal("housing", need, _num(step2.get("p")), hz, active,
                 [price_key, "ltv", "purchase_cost_rate", "mortgage_cap", "mortgage_cap_regions", "first_home_age"],
                 warnings, note,
                 {"inputs": {"sido": sido, "house_price": price, "ltv": ltv, "loan_cap": cap, "loan": _r(loan),
                             "purchase_cost_rate": cost}})


def wedding_goal(profile, sex, marital, age, as_of, ref, opt, warnings):
    share = opt["wedding_self_share"]
    base = ref["wedding_cost_ex_housing"]["value"]
    low = ref["wedding_cost_low"]["value"]
    high = ref["wedding_cost_high"]["value"]
    need = None if base is None else base * share
    married = bool(marital) and str(marital).startswith("기혼")
    if marital is None:
        warnings.append("결혼: 혼인 상태 미확인 → 미혼으로 봄")
    rng = [_r(v * share) if v is not None else None for v in (low, high if high is not None else base)]
    hz = horizon(profile, "wedding", _by_sex(ref["first_marriage_age"]["value"], sex, warnings, "결혼"),
                 "first_marriage_age", age, as_of, warnings)
    return _goal("wedding", need, 0.0, hz, not married,
                 ["wedding_cost_ex_housing", "wedding_cost_low", "wedding_cost_high",
                  "wedding_self_share", "first_marriage_age"], warnings,
                 "기혼 → 제외" if married else None,
                 {"range": rng, "inputs": {"cost_ex_housing": base, "self_share": share}})


def cpi_factor(ref, warnings) -> tuple[float, dict]:
    """양육비 기준 연도 → 물가지수 최근 연도 환산 계수. 못 구하면 1.0."""
    cpi = ref["cpi_annual"]["value"] or {}
    base = str(ref["child_cost_to_college"]["year"])
    latest = max(cpi, default=None)
    if base not in cpi or latest is None or not cpi[base]:
        warnings.append(f"양육: {base}년 대비 물가지수를 못 찾음 → 물가 계수 1.0")
        return 1.0, {"base_year": base, "latest_year": latest, "factor": 1.0}
    f = cpi[latest] / cpi[base]
    return f, {"base_year": base, "base_index": cpi[base], "latest_year": latest,
               "latest_index": cpi[latest], "factor": round(f, 4)}


def child_goal(profile, sex, children, age, as_of, ref, opt, warnings):
    share = opt["child_self_share"]
    per_child = ref["child_cost_to_college"]["value"]
    factor, cpi_info = cpi_factor(ref, warnings)
    n = children if children else ref["default_children"]["value"]
    need = None if per_child is None else per_child * factor * share * n
    spend = ref["child_spend_years"]["value"]
    hz = horizon(profile, "child", _by_sex(ref["first_child_parent_age"]["value"], sex, warnings, "양육"),
                 "first_child_parent_age", age, as_of, warnings)
    if children:
        to_birth = 0.0
    else:
        to_birth = max(hz["years_raw"] or 0.0, 0.0)
        if (hz["years_raw"] or 0) < 0:
            warnings.append(f"양육: 첫 출산 목표 연령 {hz['target_age']}세가 이미 지남 → 출산까지 0년")
    hz = {**hz, "years_raw": to_birth + spend / 2,
          "source": {**hz["source"], "years_rule": f"출산까지 {to_birth:.2f}년 + 지출기간 {spend}년/2"}}
    return _goal("child", need, 0.0, hz, True,
                 ["child_cost_to_college", "cpi_annual", "child_self_share", "default_children",
                  "child_spend_years", "first_child_parent_age"],
                 warnings, None if children else f"자녀 없음/미확인 → {n}명 가정",
                 {"inputs": {"per_child_base": per_child, "cpi": cpi_info, "per_child_now": _r(
                     None if per_child is None else per_child * factor), "self_share": share, "children": n,
                     "years_to_birth": _r(to_birth), "spend_years": spend}})


def life_end_age(sex, ref, warnings) -> tuple[float, str]:
    at60 = ref["life_expectancy_at_60"]["value"]
    if at60:
        return 60 + _by_sex(at60, sex, warnings, "노후 60세 기대여명"), "life_expectancy_at_60"
    warnings.append("노후: 60세 기대여명 없음 → 출생 시 기대수명 사용")
    return _by_sex(ref["life_expectancy"]["value"], sex, warnings, "노후 기대수명"), "life_expectancy"


def retire_goal(step2, profile, sex, age, as_of, ref, opt, warnings, retire_age_explicit=False):
    """은퇴 연령: 인자로 주면 인자, 아니면 고객 목표 기한 'N세', 그것도 없으면 기준값 표 기본값."""
    retire_age = opt["retire_age"]
    retire_src = "인자" if retire_age_explicit else "기준값 표 기본값"
    hz = horizon(profile, "retire", retire_age, "retire_age", age, as_of, warnings)
    from_goal = hz["source"]["source"] == "고객 목표" and re.search(r"\d\s*세", str(hz["source"]["due"]))
    if from_goal and not retire_age_explicit:
        retire_age, retire_src = hz["target_age"], f"고객 목표 {hz['source']['goal_id']} '{hz['source']['due']}'"
    elif hz["source"]["source"] == "고객 목표" and hz["target_age"] != retire_age:
        warnings.append(f"노후: 고객 목표 은퇴 시점({hz['target_age']}세)으로 years를 잡았지만 "
                        f"필요자금은 은퇴 연령 {retire_age}세({retire_src})로 계산함")
    start = ref["pension_start_age"]["value"]
    end, life_key = life_end_age(sex, ref, warnings)
    pension = ref["nps_old_age_avg_monthly"]["value"]
    lo, hi = ref["retire_min_living_single"]["value"], ref["retire_adequate_living_single"]["value"]
    pre = max(start - retire_age, 0)               # 은퇴~연금 개시: 생활비 전액
    post = max(end - max(retire_age, start), 0)    # 연금 개시~기대수명: 생활비 − 연금

    def calc(living):
        if living is None or pension is None:
            return None
        return living * 12 * pre + max(living - pension, 0) * 12 * post

    existing = sum(_num(step2.get(k)) for k in ("q", "r", "s"))
    return _goal("retire", calc(lo), existing, hz, True,
                 ["retire_min_living_single", "retire_adequate_living_single", "nps_old_age_avg_monthly",
                  life_key, "pension_start_age", "retire_age"], warnings, None,
                 {"range": [_r(calc(lo)), _r(calc(hi))],
                  "inputs": {"living_min": lo, "living_adequate": hi, "pension": pension, "life_end_age": end,
                             "retire_age": retire_age, "retire_age_source": retire_src, "pension_start_age": start,
                             "years_before_pension": _r(pre), "years_with_pension": _r(post)}})


# ── 5·6. 분배와 달성 가능성 ──────────────────────────────────
def distribute(goals: list[dict], D: float, warnings: list) -> str | None:
    """goals에 ratio·ratio_by_need·amount를 채운다. 비율을 못 내면 사유를 돌려준다."""
    for g in goals:
        g.update(ratio=None, ratio_by_need=None, amount=None, coverage=None, monthly_needed=None, weight=None)
    act = [g for g in goals if g["active"]]
    if not act:
        reason = "active 목표 없음"
    elif sum(g["gap"] / g["years"] for g in act) <= 0:
        reason = "Σweight = 0 (모든 active 목표의 부족액이 0)"
    else:
        reason = None
    if reason:
        warnings.append(f"분배 비율 없음: {reason}")
        return reason
    weights = {g["goal"]: g["gap"] / g["years"] for g in act}
    pct = largest_remainder(weights, 1000)
    gaps = {g["goal"]: g["gap"] for g in act}
    pct_need = largest_remainder(gaps, 1000) if sum(gaps.values()) > 0 else {k: 0 for k in gaps}
    cents = largest_remainder(weights, round(D * 100)) if D > 0 else {k: 0 for k in weights}
    for g in act:
        k = g["goal"]
        g["weight"] = _r(weights[k])
        g["ratio"] = pct[k] / 10
        g["ratio_by_need"] = pct_need[k] / 10
        g["amount"] = cents[k] / 100
        g["coverage"] = _r(g["amount"] / g["gap"], 4) if g["gap"] else None
        g["monthly_needed"] = _r(max(g["gap"] - g["amount"], 0) / (g["years"] * 12))
    return None


def feasibility(goals: list[dict], deltaM) -> dict:
    needed = sum(g["monthly_needed"] or 0 for g in goals if g["active"])
    if deltaM is None:
        return {"monthly_needed_total": _r(needed), "monthly_surplus": None, "feasible": None, "shortfall_monthly": None}
    surplus = deltaM / 12
    return {"monthly_needed_total": _r(needed), "monthly_surplus": _r(surplus), "feasible": needed <= surplus,
            "shortfall_monthly": _r(max(needed - surplus, 0))}


# ── 본체 ──────────────────────────────────────────────────────
def allocate(step2: dict, profile: dict, ref: dict = REFERENCE, *, m_includes_locked: bool = False,
             emergency_multiple: float | None = None, ltv: float | None = None,
             purchase_cost_rate: float | None = None, wedding_self_share: float | None = None,
             child_self_share: float | None = None, retire_age: float | None = None,
             children: int | None = None, as_of: str | None = None, goal_basis: str = "statistical") -> dict:
    """step2(만원) + 프로필(customer.profile.load_case 결과) → 분배 결과 dict. 인자 None이면 기준값 표 기본값.

    goal_basis: "statistical"(기본, 4대 목표 통계 방식) 또는 "customer"(고객이 적은 재무목표 기준, planning.goals)."""
    if goal_basis not in ("statistical", "customer"):
        raise ValueError(f"goal_basis는 statistical 또는 customer: {goal_basis!r}")
    warnings: list[str] = []
    opt = {k: v if v is not None else ref[k]["value"] for k, v in (
        ("emergency_multiple", emergency_multiple), ("ltv", ltv), ("purchase_cost_rate", purchase_cost_rate),
        ("wedding_self_share", wedding_self_share), ("child_self_share", child_self_share),
        ("retire_age", retire_age))}
    as_of = as_of or profile.get("as_of_date") or date.today().isoformat()
    birth = fact(profile, "birth_date")
    if birth is None:
        raise ValueError("birth_date가 없어 나이를 계산할 수 없음")
    age = age_on(birth, as_of)
    sex = _norm_sex(fact(profile, "sex"))
    if children is None:
        children = fact(profile, "children_count")
        if children is None:
            warnings.append("자녀 수 미확인(facts.children_count 없음) → 0명으로 봄")

    m = m_available(step2, m_includes_locked, warnings)
    em = emergency_fund(step2, m["value"], opt["emergency_multiple"], ref["emergency_multiple_low"]["value"], warnings)
    D = max(m["value"] - em["amount"], 0.0)

    goals = [
        housing_goal(step2, profile, fact(profile, "region_sido"), fact(profile, "home_owner"), age, as_of,
                     ref, opt, warnings),
        wedding_goal(profile, sex, fact(profile, "marital_status"), age, as_of, ref, opt, warnings),
        child_goal(profile, sex, children, age, as_of, ref, opt, warnings),
        retire_goal(step2, profile, sex, age, as_of, ref, opt, warnings, retire_age_explicit=retire_age is not None),
    ]
    extra = {}
    if goal_basis == "customer":
        m, em, D, goals, extra = _customer_basis(step2, profile, ref, opt, age, sex, as_of, m, em, goals, warnings,
                                              retire_age is not None)
    reason = distribute(goals, D, warnings)
    used = sorted({k for g in goals for k in g["basis"]} | {"emergency_multiple", "emergency_multiple_low"})
    out = {
        "module": "allocation", "version": "v2", "unit": "만원",
        "inputs": {"case_id": profile.get("case_id"), "profile_version": profile.get("profile_version"),
                   "as_of": as_of, "age": age, "sex": sex, "region_sido": fact(profile, "region_sido"),
                   "marital_status": fact(profile, "marital_status"), "home_owner": fact(profile, "home_owner"),
                   "children": children, "deltaM": step2.get("deltaM"), "options": opt},
        "m_avail": m,
        "locked_assets": locked_assets(step2, as_of),
        "emergency": em,
        "distributable": _r(D),
        "allocation_method": "weight = gap / years (연간 필요 적립액), ratio = weight / Σweight (active 목표)",
        "no_ratio_reason": reason,
        "goals": goals,
        "feasibility": feasibility(goals, step2.get("deltaM")),
        "warnings": warnings,
        "reference_used": {k: ref[k] for k in used},
    }
    out.update(extra)
    return out


EARMARK_KINDS = ("세금준비금",)


def _earmarked(profile) -> tuple[float, list[dict]]:
    """납세 등 용도가 지정된 금융자산(M에 들어 있는 것). customer 모드에서 분배 대상에서 뺀다."""
    from .step2_build import classify_asset
    rows = [a for a in profile.get("assets", []) if classify_asset(a) == "M"
            and (a.get("earmark") or a.get("kind") in EARMARK_KINDS)]
    items = [{"asset_id": a.get("asset_id"), "kind": a.get("kind"), "earmark": a.get("earmark") or "납세 준비",
              "amount": _r((a.get("balance_won") or 0) / 10_000)} for a in rows]
    return sum(i["amount"] or 0 for i in items), items


def _irregular_income(profile) -> bool:
    """근로소득이 아닌 프리랜서·사업소득 고객(소득이 불규칙)."""
    emp = str(fact(profile, "employment_status") or "")
    kinds = {r.get("kind") for r in profile.get("income_items", [])}
    return "근로" not in kinds and ("프리랜서" in emp or "사업" in kinds)


INCOME_TAX_RE = re.compile(r"소득세|지방세|지방소득세|종합|세금\s*(?:적립|준비)|세금")


def _monthly_spending(profile) -> tuple[float, list[str], list[str]]:
    """소득이 끊겨도 나가는 월 지출(만원): 고정·보험·변동(생활비·사업 필요경비 포함)과 국민연금·건강보험.
    소득세·지방소득세 납부·적립(세금 준비)은 소득이 끊기면 생기지 않고 납세 준비금은 용도 지정으로 따로 두므로 뺀다.
    → (합계, 넣은 항목, 뺀 항목). 미확인 행은 0."""
    total, used, dropped = 0, [], []
    for r in profile.get("cashflow_items", []):
        sec, item = r.get("section"), str(r.get("item") or "")
        if sec not in ("고정지출", "보험", "변동지출", "세금·사회보험"):
            continue
        if INCOME_TAX_RE.search(item) and not re.search(r"연금|건강|장기요양|고용", item):
            if r.get("monthly_won"):
                dropped.append(f"{item} {r['monthly_won'] / 10_000:,.1f}")
            continue
        total += r.get("monthly_won") or 0
        if r.get("monthly_won"):
            used.append(item)
    return total / 10_000, used, dropped


def _customer_basis(step2, profile, ref, opt, age, sex, as_of, m, em, stat_goals, warnings, retire_explicit):
    """고객이 적은 재무목표 기준: 용도 지정 자산 제외, 비상금은 고객 금액(없고 소득이 불규칙하면 월 지출 × 12),
    목표는 고객 금액(없으면 통계 대체)."""
    from .goals import classify_goals, customer_goals
    from .reference import STRATEGY_REFERENCE
    earmarked, earmark_items = _earmarked(profile)
    if earmarked > 0:
        m = {**m, "value": max(m["value"] - earmarked, 0.0), "earmarked": _r(earmarked), "earmarked_items": earmark_items,
             "interpretation": m["interpretation"] + f". 용도 지정(납세 등) {earmarked:,.1f} 제외"}
        em = emergency_fund(step2, m["value"], opt["emergency_multiple"], ref["emergency_multiple_low"]["value"], [])
    items = classify_goals(profile, as_of, age)
    em_items = [g for g in items if g["category"] == "emergency" and g["amount"] is not None]
    if em_items:
        amount = max(g["amount"] for g in em_items)
        held = min(amount, m["value"])
        if amount > m["value"]:
            warnings.append(f"고객 비상금 목표 {amount:,.1f}이 M_avail {m['value']:,.1f}보다 큼 → M_avail까지만, 분배 대상 0")
        em = {**em, "need": _r(amount), "amount": _r(held), "capped": amount > m["value"], "source": "고객 목표",
              "customer_amount": _r(amount), "income_rule_amount": em["amount"]}
    elif _irregular_income(profile):
        months = STRATEGY_REFERENCE["irregular_emergency_months"]["value"]
        spend, used, dropped = _monthly_spending(profile)
        need = spend * months
        held = min(need, m["value"])
        if need > m["value"]:
            warnings.append(f"소득 불규칙 비상금 {need:,.1f}이 가용 자산 {m['value']:,.1f}보다 큼 → 가용 자산까지만")
        em = {**em, "need": _r(need), "amount": _r(held), "capped": need > m["value"],
              "source": f"소득 불규칙: 월 지출 × {months}개월", "monthly_spending": _r(spend), "months": months,
              "spending_excluded": dropped,
              "customer_amount": None, "income_rule_amount": em["amount"]}
    else:
        em = {**em, "source": "월 소득 × 배수", "customer_amount": None, "income_rule_amount": em["amount"]}
    D = max(m["value"] - em["amount"], 0.0)

    def stat_fn(cat, living):
        r2 = dict(ref)
        r2["retire_min_living_single"] = {**ref["retire_min_living_single"], "value": living}
        return retire_goal(step2, profile, sex, age, as_of, r2, opt, [], retire_age_explicit=retire_explicit)
    requests: list[dict] = []
    goals, unstated, items = customer_goals(step2, profile, {g["goal"]: g for g in stat_goals}, stat_fn, age, as_of,
                                            warnings, requests)
    for it in items:
        if it["category"] == "emergency" and it["amount"] is None and em["source"].startswith("소득 불규칙"):
            it["plan_use"] = "금액 미확인: 소득 불규칙이라 월 지출 12개월분을 비상금으로"
        if it["category"] == "emergency" and it["amount"] is None:
            requests.append({"item": "비상금 목표 금액", "reason": "고객 비상금 목표에 금액이 없어 "
                             + ("월 지출 12개월분으로 계산함" if em["source"].startswith("소득 불규칙") else "월 소득 기준으로 계산함")})
    from .goals import has_children
    kids = has_children(profile)
    return m, em, D, goals, {"goal_basis": "customer", "goals_input": items, "unstated_goals": unstated,
                          "data_requests": requests, "children_present": bool(kids),
                          "child_note": "이미 자녀가 있어 양육은 목표에서 뺐고, 양육비는 현재 지출에 들어 있다고 봄(추가 차감 없음)"
                          if kids else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step2", required=True, type=Path)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--m-includes-locked", action="store_true")
    parser.add_argument("--retire-age", type=float)
    parser.add_argument("--emergency-multiple", type=float)
    args = parser.parse_args()

    step2 = json.loads(args.step2.read_text(encoding="utf-8"))
    result = allocate(step2, load_case(args.case), m_includes_locked=args.m_includes_locked,
                      retire_age=args.retire_age, emergency_multiple=args.emergency_multiple)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"→ {args.out}")
    for g in result["goals"]:
        print(f"{g['name']}: need {g['need']} gap {g['gap']} years {g['years']} ({g['horizon_source']['source']}) "
              f"ratio {g['ratio']}% by_need {g['ratio_by_need']}% amount {g['amount']} monthly {g['monthly_needed']}")
    print("feasibility:", result["feasibility"])
    for w in result["warnings"]:
        print("  [경고]", w)


if __name__ == "__main__":
    main()
