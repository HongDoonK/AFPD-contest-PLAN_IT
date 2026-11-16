"""Fact Finding '2. 재무목표'(고객이 적은 목표)를 계획 목표로 바꾼다.

- classify_goals(): 프로필 goals → 종류(category)·고객 표기 기간·기한·금액(만원)·원문. 순수 함수.
- customer_goals(): allocation.allocate(goal_basis="customer")가 부른다. 4대 목표(주거·결혼·양육·노후)는 항상 기본으로
  넣고(고객 금액·시점, 없으면 통계값), 고객이 추가로 적은 목표(부채 상환·기타·반복 목표)를 그 위에 같은 공식으로 분배한다.
"""
from __future__ import annotations

import re
from datetime import date

CATEGORY_NAMES = {"emergency": "비상금", "debt": "부채 상환", "housing": "주거", "wedding": "결혼", "child": "양육",
                  "retire": "노후", "other": "기타 목표"}
# 앞에 있는 종류가 먼저 걸린다(예: '대출 상환'은 부채, '교육자금'은 자녀)
CATEGORY_WORDS = [
    ("emergency", ("비상금", "예비금", "비상자금")),
    ("debt", ("대출", "상환", "부채")),
    ("child", ("양육", "자녀", "육아", "출산", "교육자금", "학자금")),
    ("housing", ("주거", "주택", "내집", "내 집", "자가", "아파트", "집 마련")),
    ("wedding", ("결혼", "웨딩", "혼인")),
    ("retire", ("은퇴", "노후")),
]
FOUR = ("housing", "wedding", "child", "retire")
TERMS = ("단기", "중기", "장기")


def category_of(name: str) -> str:
    text = str(name or "")
    return next((c for c, words in CATEGORY_WORDS if any(w in text for w in words)), "other")


def _man(won):
    return None if won is None else round(won / 10_000, 2)


def parse_due(due, age: int | None, as_of: str) -> dict:
    """고객 기한 표기 → {'years','target_age','note'}. 'YYYY-MM', 'YYYY', 'N세', 'YYYY~YYYY', 'N~M년쯤', 'N년'.
    범위는 이른 쪽을 쓰고 note에 남긴다. 못 읽으면 years=None."""
    from .allocation import _due_years
    s = str(due or "").strip()
    out = {"years": None, "target_age": None, "note": None}
    if not s:
        out["note"] = "기한 미기재"
        return out
    if age is not None and (parsed := _due_years(s, age, as_of)):
        out["years"], out["target_age"] = round(parsed[0], 2), round(parsed[1], 2)
        return out
    d = date.fromisoformat(as_of)
    if m := re.search(r"(\d{4})\s*[~\-]\s*(\d{4})", s):
        out.update(years=float(int(m[1]) - d.year), note=f"기한 범위 '{s}' 중 이른 해({m[1]})로 계산",
                   years_late=float(int(m[2]) - d.year), late_label=m[2])
    elif m := re.search(r"(\d+)\s*~\s*(\d+)\s*년", s):
        out.update(years=float(m[1]), note=f"기한 범위 '{s}' 중 짧은 쪽({m[1]}년)으로 계산",
                   years_late=float(m[2]), late_label=f"{m[2]}년 뒤")
    elif m := re.search(r"(\d+)\s*년", s):
        out.update(years=float(m[1]))
    else:
        out["note"] = f"기한 '{s}'를 읽지 못함"
        return out
    if age is not None and out["years"] is not None:
        out["target_age"] = round(age + out["years"], 2)
    return out


NO_PLAN_RE = re.compile(r"없음|없다|비혼|딩크|안\s*함|하지\s*않|안\s*낳")
HEDGE_RE = re.compile(r"당장|아직|미정|생각|고민|나중|보류|모르")


def plan_state(value) -> str | None:
    """'none'(단정: 없음·계획 없음·비혼·딩크), 'hedged'(유보: 당장·아직·미정·생각 중), None(값 없음·그 밖)."""
    text = str(value or "").strip()
    if not text:
        return None
    if HEDGE_RE.search(text):
        return "hedged"
    return "none" if NO_PLAN_RE.search(text) else None
RECURRING_WORDS = ("매년", "1년에 한 번", "1년에 한번", "일 년에 한 번", "연 1회", "연1회", "해마다", "매해")


def _item(goal_id, name, cat, term, due, parsed, amount, amount_basis, status, raw, source_ref, source):
    label = "교육" if cat == "child" and "교육" in name else CATEGORY_NAMES[cat]
    text = f"{name} {raw or ''} {due or ''}"
    return {"goal_id": goal_id, "category": cat, "category_name": label, "name": name, "term_stated": term,
            "due": due, "years_raw": parsed["years"], "target_age": parsed["target_age"], "due_note": parsed["note"],
            "amount": amount, "amount_basis": amount_basis, "status": status, "raw": raw, "source_ref": source_ref,
            "source": source, "recurrence": "annual" if any(w in text for w in RECURRING_WORDS) else None,
            "years_late": parsed.get("years_late"), "late_label": parsed.get("late_label")}


def classify_goals(profile: dict, as_of: str | None = None, age: int | None = None) -> list[dict]:
    """프로필 goals(Fact Finding 2번 등) → 분류 목록. 금액은 만원, 모르는 값은 None 그대로."""
    as_of = as_of or profile.get("as_of_date") or date.today().isoformat()
    out = []
    for g in profile.get("goals") or []:
        name = str(g.get("name") or "")
        term = g.get("term") if g.get("term") in TERMS else None
        out.append(_item(g.get("goal_id"), name, category_of(name), term, g.get("due"),
                         parse_due(g.get("due"), age, as_of), _man(g.get("amount_won")), g.get("amount_basis"),
                         g.get("status") or ("미확인" if g.get("amount_won") is None else "기재"), g.get("raw"),
                         g.get("source_ref"), "고객 Fact Finding"))
    return out


def _term_rule(years):
    if years is None:
        return None
    return "단기" if years <= 3 else "중기" if years <= 10 else "장기"


def has_children(profile: dict) -> bool | None:
    facts = profile.get("facts", {})
    n = facts.get("children_count", {}).get("value")
    if n is not None:
        return n > 0
    married = str(facts.get("marital_status", {}).get("value") or "").startswith("기혼")
    size = facts.get("household_size", {}).get("value")
    if married and size is not None and size >= 3:
        return True
    return None


def _from_item(it, cat, key, name, kind, step2, profile, stat, stat_fn, warnings, data_requests):
    reference = stat["need"] if stat is not None else None
    if cat == "child" and "교육" in it["name"] and kind == "extra":
        reference = None  # 출생~대학 양육비 통계는 별도 교육자금과 비교할 수 없다
    need, need_source, existing, extra = None, None, 0.0, {}
    if cat == "retire" and it["amount"] is not None and "월" in str(it["amount_basis"] or ""):
        g2 = stat_fn("retire", it["amount"])
        need, need_source, existing = g2["need"], "고객 금액", g2["existing"]
        extra = {"inputs": g2.get("inputs"), "range": g2.get("range")}
    elif it["amount"] is not None:
        need, need_source = it["amount"], "고객 금액"
        if kind == "base" and cat == "housing":
            existing = float(step2.get("p") or 0)
            extra = {"inputs": (stat or {}).get("inputs")}  # 매매가·대출은 통계 기준 참고값
        elif kind == "base" and cat == "retire":
            existing = sum(float(step2.get(k) or 0) for k in ("q", "r", "s"))
    elif kind == "base" and stat is not None and stat.get("need") is not None:
        need, need_source, existing = stat["need"], "통계 대체", stat["existing"]
        extra = {"inputs": stat.get("inputs"), "range": stat.get("range")}
        data_requests.append({"item": f"{name} 목표 금액", "reason": "고객 목표에 금액이 없어 통계값으로 계산함"})
    else:
        data_requests.append({"item": f"{name} 목표 금액", "reason": "금액이 없어 분배에서 뺌"})
    if cat == "debt" and need is not None:
        debt = sum(_man(l.get("balance_won")) or 0 for l in profile.get("liabilities", []))
        if need > debt + 0.01:
            warnings.append(f"부채 상환 목표 {need:,.0f}만원이 부채 잔액 합 {debt:,.0f}만원보다 큼")
    years_raw = it["years_raw"]
    hsrc = {"source": "고객 목표", "goal_id": it["goal_id"], "goal_name": it["name"], "due": it["due"],
            "rule": it["due_note"] or "고객이 적은 기한"}
    if years_raw is None and it["recurrence"]:
        years_raw, hsrc["rule"] = 1.0, "매년 반복: 다음 시점까지 1년으로 계산"
    if years_raw is None:
        if kind == "base" and stat is not None and stat.get("years") is not None:
            years_raw, hsrc = stat["years"], {**(stat.get("horizon_source") or {}), "source": "통계 연령",
                                             "rule": f"고객 기한 '{it['due']}'를 읽지 못해 통계 시점 사용"}
        else:
            data_requests.append({"item": f"{name} 목표 기한", "reason": "기한을 읽지 못함"})
    years = None if years_raw is None else max(years_raw, 1)
    active = need is not None and years is not None
    gap = None if need is None else max(need - existing, 0.0)
    g = {"goal": key, "name": name, "need": None if need is None else round(need, 2), "existing": round(existing, 2),
         "gap": None if gap is None else round(gap, 2), "target_age": it["target_age"],
         "years": None if years is None else round(years, 2), "horizon_source": hsrc, "active": active,
         "note": None if active else "금액 또는 기한 미확인 → 분배에서 제외", "basis": [],
         "category": cat, "kind": kind, "label": it["name"], "goal_id": it["goal_id"], "term_stated": it["term_stated"],
         "term_rule": _term_rule(years_raw), "need_source": need_source,
         "reference_need": None if reference is None else round(reference, 2),
         "need_diff": None if reference is None or need is None else round(need - reference, 2),
         "amount_basis": it["amount_basis"], "raw": it["raw"], "due": it["due"], "recurrence": it["recurrence"],
         "recurring_monthly": round(need / 12, 2) if it["recurrence"] and need is not None else None,
         "years_late": it.get("years_late"), "late_label": it.get("late_label"), **extra}
    if it["recurrence"] and need is None:
        data_requests.append({"item": f"{name} 반복 목표 금액", "reason": "매년 반복 목표의 1회 금액이 필요함"})
    it["plan_key"] = key
    use = "기본 목표로 분배" if kind == "base" else "추가 목표로 분배"
    if it["recurrence"]:
        use += "(매년 반복)"
    if need_source == "통계 대체":
        use += ", 금액은 통계 대체"
    it["plan_use"] = use if active else "금액·기한 미확인: 분배 제외"
    it["reference_need"] = g["reference_need"]
    it["kind"] = kind
    return g


def customer_goals(step2: dict, profile: dict, stat_goals: dict, stat_fn, age: int, as_of: str, warnings: list,
                   data_requests: list) -> tuple[list[dict], list[dict], list[dict]]:
    """분배 목표 = 4대 기본 목표(항상, 기혼→결혼·주택 보유→주거 제외 규칙은 유지) + 고객이 추가로 적은 목표.

    4대 목표는 고객이 적었으면 고객 금액·시점, 아니면 통계값·통계 연령(need_source='통계 기본값').
    stat_goals: 통계 방식 4대 목표 {key: goal}. stat_fn(category, amount): 노후 월 생활비를 넣어 다시 계산.
    반환: (목표 목록, 고객 미기재 기본 목표 목록, 분류 결과 goals_input). 비상금은 allocate가 따로 반영."""
    items = classify_goals(profile, as_of, age)
    kids = has_children(profile)
    plan_facts = {cat: (profile.get("facts", {}).get(field) or {}).get("value")
                  for cat, field in (("wedding", "marriage_plan"), ("child", "children_plan"))}
    no_plan = {cat for cat, v in plan_facts.items() if plan_state(v) == "none"}
    hedged = {cat: v for cat, v in plan_facts.items() if plan_state(v) == "hedged"}
    for cat, v in hedged.items():
        data_requests.append({"item": f"{CATEGORY_NAMES[cat]} 계획(고객 유보 표현 '{v}')",
                              "reason": "계획 없음으로 보지 않고 기본 목표로 유지함. 고객에게 확인"})
    base_item: dict[str, dict] = {}
    extras = []
    for it in items:
        cat = it["category"]
        if cat == "emergency":
            it["kind"] = "emergency"
            it["plan_use"] = "비상금으로 반영" if it["amount"] is not None else "금액 미확인: 소득 기준 비상금 사용"
            it["reference_need"] = None
            continue
        education = cat == "child" and "교육" in it["name"]
        if cat == "child" and kids and not education:
            it["kind"] = "excluded"
            it["plan_use"] = "자녀 있음: 양육비는 현재 지출에 포함(목표에서 제외)"
            it["reference_need"] = None
            continue
        if cat in no_plan and not education:
            it["kind"] = "excluded"
            it["plan_use"] = "고객이 계획 없음으로 기재해 제외"
            it["reference_need"] = None
            continue
        if cat in FOUR and cat not in base_item and not education:
            base_item[cat] = it
        else:
            extras.append(it)  # 교육자금은 양육과 따로 '교육자금' 추가 목표
    goals, unstated = [], []
    for cat in FOUR:
        stat = stat_goals.get(cat)
        it = base_item.get(cat)
        if it is not None:
            goals.append(_from_item(it, cat, cat, CATEGORY_NAMES[cat], "base", step2, profile, stat, stat_fn,
                                    warnings, data_requests))
            continue
        if stat is None or not stat["active"] or stat.get("need") is None:
            continue  # 기혼 → 결혼, 주택 보유 → 주거 제외(기존 규칙)
        if cat == "child" and kids:
            continue  # 이미 자녀가 있으면 양육은 처음부터 목표에서 뺀다(양육비는 현재 지출에 들어 있음)
        if cat in no_plan:
            field = "marriage_plan" if cat == "wedding" else "children_plan"
            items.append({"goal_id": None, "category": cat, "category_name": CATEGORY_NAMES[cat],
                          "name": CATEGORY_NAMES[cat], "term_stated": None, "due": None, "years_raw": None,
                          "target_age": None, "due_note": None, "amount": None, "amount_basis": None, "status": "기재",
                          "raw": f"{CATEGORY_NAMES[cat]} 계획: {profile['facts'][field]['value']}", "source_ref": field,
                          "source": "고객 Fact Finding", "recurrence": None, "kind": "excluded",
                          "plan_use": "고객이 계획 없음으로 기재해 제외", "reference_need": stat.get("need")})
            continue
        g = {**stat, "kind": "base", "category": cat, "label": None, "goal_id": None, "term_stated": None,
             "term_rule": _term_rule(stat["years"]), "need_source": "통계 기본값", "reference_need": stat["need"],
             "need_diff": 0.0, "due": None, "raw": None, "recurrence": None, "recurring_monthly": None}
        goals.append(g)
        unstated.append({"goal": cat, "name": stat["name"], "need": stat["need"], "existing": stat["existing"],
                         "gap": stat["gap"], "years": stat["years"], "target_age": stat["target_age"],
                         "horizon_source": stat.get("horizon_source"), "hedge": hedged.get(cat),
                         "note": "기본 목표(고객 미기재, 통계값)로 분배에 포함"})
    used = {g["goal"] for g in goals}
    counter: dict[str, int] = {}
    for it in extras:
        cat = it["category"]
        counter[cat] = counter.get(cat, 0) + 1
        key = cat if cat not in used else f"{cat}_{counter[cat] + 1}"
        while key in used:
            counter[cat] += 1
            key = f"{cat}_{counter[cat] + 1}"
        used.add(key)
        if cat == "other":
            name = f"기타 목표 {counter[cat]}"
        elif cat == "child" and "교육" in it["name"]:
            name = "교육자금"
        else:
            name = it["category_name"]
        goals.append(_from_item(it, cat, key, name, "extra", step2, profile, stat_goals.get(cat), stat_fn,
                                warnings, data_requests))
    return goals, unstated, items
