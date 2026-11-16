"""결정적 전략 엔진. LLM 없이 대시보드의 모든 숫자를 만든다(만원).

입력: 반영본 step2(reflect.build 결과, `_allocation` 포함), 고객 프로필(load_case), 정책 판정 결과, spending_cut 결과.
출력: 목표별 전망(goals), 두 가지 적립 방식 비교(options), 자산 목적별 배정(asset_mapping), 전략 카드(cards),
요약(summary), 설계사 판단 사항(decisions), 경고, 가정값 요약, 고객에게 받을 자료(data_requests).

- 모든 전망은 오늘 가치 기준 실질 수익률 (1+r)/(1+물가)−1로 계산해 allocation의 need(오늘 가치)와 직접 비교한다.
- 수익률은 목표 종류와 남은 기간으로 정한다(사용자 지정): 단기 2~3%, 성장(주거·양육 등 3년 초과) 5~8%,
  노후는 은퇴까지 20년 초과 70:30·10% 내외, 10~20년 50:50·7~9%, 10년 이하 30:70·5%. 하한·기준·상한 세 경로.
- 월 복리, 월말 적립. FV는 월 적립에 선형이므로 필요 월 적립은 해석적으로 구한다.
- 카드는 검토 제안이고 판정이 아니다. 숫자가 없으면 카드를 만들지 않거나 confirm으로만 남긴다.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

from customer.profile import age_on, fact

from .allocation import GOAL_NAMES, allocate, largest_remainder
from .reference import REFERENCE, RETIRE_STAGE_YEARS, SHORT_HORIZON_YEARS, STRATEGY_REFERENCE
from .retire_tax import retire_tax_plan
from .step2_build import classify_asset

TERMS = ("단기", "중기", "장기")
MAX_MONTHS = 720
LIQUIDITY_ORDER = {"즉시": 0, "만기": 1}
EMERGENCY_RE = re.compile(r"비상금|예비금")
DEPOSIT_KINDS = ("입출금", "파킹", "CMA", "세금준비금", "MMF", "외화예금")
INVEST_KINDS = ("ETF", "펀드", "주식", "ISA")  # 가격이 변하는 자산. ISA는 운용 상품을 몰라 투자로 봄
SCENARIOS = ("low", "base", "high")
GOAL_CARD_ID = {"housing": "M1", "retire": "L2", "child": "L3", "wedding": "S6"}
GOAL_LABEL = {"housing": "주거", "wedding": "결혼", "child": "양육", "retire": "노후", "debt": "부채 상환"}


GUIDE_PATH = Path(__file__).with_name("knowledge") / "strategy_guide.json"


def load_guide(path: Path = GUIDE_PATH) -> dict:
    """팀 예시 보고서에서 간추린 전략 원칙(P01~). 파일이 없으면 빈 목록."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"source": None, "principles": [], "report_flow": []}


def card_tokens(card: dict) -> set[str]:
    """카드가 해당하는 원칙 적용 대상(applies_to) 낱말: 카드 번호, 목표 종류, 성격."""
    tokens = {card["id"]}
    goal = card.get("goal")
    if goal:
        cat = goal.split("_")[0]
        tokens |= {goal, cat}
        if "충족률(%)" in card["numbers"]:  # 목표 운용 카드(전망이 있는 카드)
            tokens.add("projection")
            if card["term"] == "단기":
                tokens.add("short_goal")
            if cat in ("housing", "child") and card["term"] != "단기":
                tokens.add("glide")
            if cat == "retire":
                tokens |= {"retire_glide", "locked_assets", "risk_profile"}
    if card["id"] in ("S4", "S5"):
        tokens.add("policy")
    if card["id"] == "M3":
        tokens |= {"glide", "risk_profile"}
    return tokens


def principles_for(tokens: set[str], guide: dict) -> list[str]:
    return [p["id"] for p in guide.get("principles", []) if tokens & set(p.get("applies_to") or [])]


# ── 작은 도구 ─────────────────────────────────────────────────
def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _man(won):
    return None if won is None else round(won / 10_000, 2)


def _n(x) -> float:
    return float(x or 0)


def fm(x, nd=1) -> str:
    """만원 표기(천 단위 구분). None은 '미확인'."""
    if x is None:
        return "미확인"
    text = f"{x:,.{nd}f}".rstrip("0").rstrip(".") if nd else f"{x:,.0f}"
    return f"{text}만원"


def term_of(years) -> str | None:
    if years is None:
        return None
    return "단기" if years <= 3 else "중기" if years <= 10 else "장기"


def add_months(d: date, months: int) -> str:
    y, m = divmod(d.month - 1 + months, 12)
    return f"{d.year + y:04d}-{m + 1:02d}"


def assumptions_of(overrides: dict | None = None) -> dict:
    a = {k: v["value"] for k, v in STRATEGY_REFERENCE.items()}
    a.update(overrides or {})
    return a


def real_rate(nominal: float, inflation: float) -> float:
    return (1 + nominal) / (1 + inflation) - 1


# ── 수익률 가정: 목표 종류와 남은 기간으로 정한다(사용자 지정 2026-11-15) ──────────
def rate_kind(category: str | None) -> str:
    """retire(노후 단계별), short(비상금·부채 상환), growth(그 밖, 남은 기간 3년 이하가 되면 단기)."""
    if category == "retire":
        return "retire"
    if category in ("debt", "emergency"):
        return "short"
    return "growth"


def stage_of(kind: str, rem_years: float, a: dict) -> dict:
    """남은 기간 → 운용 단계 {'name','rate'{low,base,high},'risk'(노후만),'rate_text'}."""
    if kind == "retire":
        for st in a["retire_stages"]:
            if rem_years > st["above_years"]:
                return st
        return a["retire_stages"][-1]
    if kind == "growth" and rem_years > SHORT_HORIZON_YEARS:
        return {"name": "성장 운용", "rate": a["growth_return"], "risk": None, "rate_text": "5~8%"}
    return {"name": "단기 운용", "rate": a["short_return"], "risk": None, "rate_text": "2~3%"}


def _monthly_real(nominal: float, a: dict) -> float:
    return (1 + real_rate(nominal, a["inflation"])) ** (1 / 12) - 1


def project(start: float, monthly: float, months: int, kind: str, a: dict, scenario: str = "base",
            horizon: int | None = None, schedule=None):
    """월 복리·월말 적립. 매달 남은 기간(horizon − 경과 달)으로 운용 단계를 정한다.
    schedule(m)이 있으면 달마다 적립액을 그것으로(단계별 월 적립). horizon 기본은 months.
    → (months 뒤 잔액, 해마다 잔액 목록(첫 값은 지금), 달마다 잔액 목록(길이 months+1))."""
    H = months if horizon is None else horizon
    bal, series, path = float(start), [float(start)], [float(start)]
    for m in range(months):
        r = _monthly_real(stage_of(kind, (H - m) / 12, a)["rate"][scenario], a)
        bal = bal * (1 + r) + (schedule(m) if schedule is not None else monthly)
        path.append(bal)
        if (m + 1) % 12 == 0 or m + 1 == months:
            series.append(bal)
    return bal, series, path


def required_monthly(start: float, need: float, months: int, kind: str, a: dict, scenario: str = "base") -> float | None:
    """FV(start, x) = A + x·B → need에 닿는 x. 이미 닿으면 0."""
    if months <= 0:
        return None
    A = project(start, 0.0, months, kind, a, scenario)[0]
    B = project(0.0, 1.0, months, kind, a, scenario)[0]
    return max((need - A) / B, 0.0) if B > 0 else None


def rate_plan(months: int, kind: str, a: dict, d: date, age: float | None, path: dict | None = None,
              horizon: int | None = None) -> list[dict]:
    """구간별 운용 계획: 단계 이름, 시작~끝 연월, 나이, 명목 수익률(하한·기준·상한), 실질 기준, 위험:안전(노후),
    구간 말 예상 잔액(path: {scenario: 달마다 잔액})."""
    segs: list[dict] = []
    H = months if horizon is None else horizon
    for m in range(months):
        st = stage_of(kind, (H - m) / 12, a)
        if not segs or segs[-1]["stage"] != st["name"]:
            segs.append({"stage": st["name"], "m0": m, "rate": st["rate"], "risk": st["risk"],
                         "rate_text": st["rate_text"]})
        segs[-1]["m1"] = m + 1
    out = []
    for s in segs:
        risk = s["risk"]
        out.append({
            "stage": s["stage"], "start": add_months(d, s["m0"]), "end": add_months(d, s["m1"]),
            "months": s["m1"] - s["m0"],
            "start_age": None if age is None else round(age + s["m0"] / 12, 1),
            "end_age": None if age is None else round(age + s["m1"] / 12, 1),
            "nominal": {k: round(v * 100, 2) for k, v in s["rate"].items()}, "rate_text": s["rate_text"],
            "real_base": round(real_rate(s["rate"]["base"], a["inflation"]) * 100, 2),
            "risk_ratio": None if risk is None else f"{round(risk * 100)}:{round((1 - risk) * 100)}",
            "end_balance": None if path is None else {k: _r(p[s["m1"]]) for k, p in path.items()},
        })
    return out


def simulate(goals: list[dict], contribution, a: dict, max_months: int = MAX_MONTHS) -> list[int | None]:
    """goals: [{'start','need','months','rate_kind'}]. contribution(m, achieved) → 목표별 이번 달 적립 목록.
    각 목표는 자기 목표 시점까지 남은 기간으로 운용 단계를 정한다(기준 수익률). → need에 닿은 달, 못 닿으면 None."""
    bal = [float(g["start"]) for g in goals]
    achieved: list[int | None] = [None] * len(goals)
    for m in range(max_months + 1):
        for i, g in enumerate(goals):
            if achieved[i] is None and bal[i] >= g["need"] - 1e-9:
                achieved[i] = m
        if all(x is not None for x in achieved) or m == max_months:
            break
        add = contribution(m, achieved)
        for i, g in enumerate(goals):
            st = stage_of(g.get("rate_kind", "growth"), (g.get("horizon", g["months"]) - m) / 12, a)
            bal[i] = bal[i] * (1 + _monthly_real(st["rate"]["base"], a)) + add[i]
    return achieved


def sequential(goals: list[dict], monthly_total, a: dict) -> list[int | None]:
    """월 잉여 전액을 목표 시점이 가까운 순으로 하나씩 채운다. monthly_total은 숫자 또는 달 → 금액 함수(단계별 잉여)."""
    order = sorted(range(len(goals)), key=lambda i: goals[i]["months"])
    total = monthly_total if callable(monthly_total) else (lambda m: monthly_total)

    def contrib(m, achieved):
        focus = next((i for i in order if achieved[i] is None), None)
        return [total(m) if i == focus else 0.0 for i in range(len(goals))]
    return simulate(goals, contrib, a)


def by_ratio(goals: list[dict], a: dict) -> list[int | None]:
    """목표마다 계획 월 적립을 계속 넣는다(단계별 적립 schedule이 있으면 그것)."""
    return simulate(goals, lambda m, ach: [g["schedule"](m) if g.get("schedule") else g["planned_monthly"]
                                           for g in goals], a)


def annuity_payment(principal: float, annual_rate: float, years: int) -> float:
    n, r = years * 12, annual_rate / 12
    return principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)


def pension_credit(deltaq, deltar, planned_annual, rate, decided_tax, a: dict) -> dict:
    """연금계좌 세액공제(만원/년). 현재 납입과 계획 적립을 연금계좌로 넣을 때. 추가 공제는 결정세액을 넘지 못함."""
    r_limit, total_limit = a["pension_saving_limit"], a["pension_total_limit"]
    cur_q, cur_r = _n(deltaq), _n(deltar)
    eligible = min(min(cur_r, r_limit) + cur_q, total_limit)
    out = {"current_annual": _r(cur_q + cur_r), "current_eligible": _r(eligible),
           "remaining_limit": _r(total_limit - eligible), "credit_rate": rate,
           "current_credit": None if rate is None else _r(eligible * rate),
           "planned_annual": _r(planned_annual), "planned_eligible": None, "planned_credit": None,
           "additional_credit_raw": None, "decided_tax": _r(decided_tax), "additional_credit": None,
           "capped_by_tax": None}
    if rate is None or planned_annual is None:
        return out
    p_elig = min(max(planned_annual, 0.0), total_limit)
    p_credit = p_elig * rate
    add_raw = max(p_credit - eligible * rate, 0.0)
    add = add_raw if decided_tax is None else min(add_raw, max(decided_tax, 0.0))
    out.update(planned_eligible=_r(p_elig), planned_credit=_r(p_credit), additional_credit_raw=_r(add_raw),
               additional_credit=_r(add), capped_by_tax=None if decided_tax is None else add < add_raw - 1e-9)
    return out


# ── 프로필에서 읽기 ───────────────────────────────────────────
def _income_annual(profile: dict, basis: str) -> float | None:
    rows = [r for r in profile.get("income_items", []) if r.get("basis") == basis and r.get("period") == "연"
            and r.get("amount_won") is not None]
    if not rows:
        return None
    return _man(max(rows, key=lambda r: r.get("tax_year") or 0)["amount_won"])


def _decided_tax(profile: dict) -> float | None:
    rows = [t for t in profile.get("tax_items", []) if str(t.get("item", "")).startswith("결정세액")
            and t.get("amount_won") is not None]
    return _man(sum(t["amount_won"] for t in rows)) if rows else None


def _liquidity_key(asset: dict):
    earmarked = 1 if asset.get("earmark") else 0
    return (earmarked, LIQUIDITY_ORDER.get(asset.get("liquidity"), 2), asset.get("matures_on") or "9999-12-31")


def m_assets(profile: dict) -> list[dict]:
    return [a for a in profile.get("assets", []) if classify_asset(a) == "M"]


def asset_label(a: dict) -> str:
    kind, name = a.get("kind") or "", a.get("name") or ""
    return kind if not name or name == kind else f"{kind}({name})"


SAFE_RULE = ("비상금 → 3년 안 목표(목표 시점 전 만기 우선)는 안전 자산에서만. 투자 자산은 남은 기간이 긴 목표부터, "
             "남는 안전 자산은 3년 넘는 목표 중 시점이 빠른 순. 보존자산은 주택청약 → 주거, IRP·연금저축·변액 → 노후")


def asset_class(asset: dict) -> str:
    """'투자'(가격 변동) 또는 '안전'(원금보전)."""
    return "투자" if asset.get("kind") in INVEST_KINDS else "안전"


def _fill(rows: list[dict], key: str, amount: float, *, prefer=None, converted: bool = False) -> float:
    """rows의 남은 잔액에서 key 목표를 amount만큼 채운다. prefer(row)가 참인 행을 먼저. → 못 채운 금액."""
    need = amount
    order = sorted(rows, key=lambda r: 0 if prefer is None or prefer(r) else 1)
    for r in order:
        if need <= 1e-9:
            break
        x = min(r["left"], need)
        if x <= 1e-9:
            continue
        bucket = r["converted"] if converted else r["assigned"]
        bucket[key] = round(bucket.get(key, 0.0) + x, 2)
        r["left"] -= x
        need -= x
    return need


def asset_mapping(assets: list[dict], emergency: float, goals: list[dict], locked: list[dict] | None = None,
                  excluded_ids: set | None = None) -> dict:
    """자산 목적별 배정. goals: [{'key','name','amount','months','target'(YYYY-MM)}], locked: 보존자산
    [{'label','balance','goal','annual'}]. 같은 돈을 두 목표에 겹쳐 배정하지 않는다(합계 = 금융자산 합)."""
    rows = []
    for a in sorted(assets, key=_liquidity_key):
        bal = _man(a.get("balance_won")) or 0.0
        held = a.get("asset_id") in (excluded_ids or set())
        rows.append({"asset_id": a.get("asset_id"), "asset": asset_label(a), "kind": a.get("kind"),
                     "class": "용도 지정" if held else asset_class(a), "balance": bal, "matures_on": a.get("matures_on"),
                     "liquidity": a.get("liquidity"), "earmark": a.get("earmark") or ("납세 준비" if held else None),
                     "balance_known": a.get("balance_won") is not None, "assigned": {}, "converted": {},
                     "left": 0.0 if held else bal})
    safe = [r for r in rows if r["class"] == "안전"]
    invest = [r for r in rows if r["class"] == "투자"]
    short = sorted([g for g in goals if g["months"] <= SHORT_HORIZON_YEARS * 12 and _n(g["amount"]) > 0],
                   key=lambda g: g["months"])
    long_ = [g for g in goals if g["months"] > SHORT_HORIZON_YEARS * 12 and _n(g["amount"]) > 0]
    rebalance, unfilled = [], {}

    def to_safe(key, name, amount):
        left = _fill(invest, key, amount, converted=True)
        moved = amount - left
        if moved > 1e-9:
            rebalance.append({"type": "안전 전환", "goal": key, "goal_name": name, "amount": _r(moved),
                              "text": f"투자 자산 {fm(moved)}을 안전 자산으로 전환 필요({name})"})
        if left > 1e-9:
            unfilled[key] = _r(left)
    left = _fill(safe, "emergency", _n(emergency))
    if left > 1e-9:
        to_safe("emergency", "비상금", left)
    for g in short:
        target = g.get("target") or "9999-12"
        left = _fill(safe, g["key"], _n(g["amount"]),
                     prefer=lambda r, t=target: r["liquidity"] == "즉시" or (r["matures_on"] or "9999")[:7] <= t)
        if left > 1e-9:
            to_safe(g["key"], g["name"], left)
    need = {g["key"]: _n(g["amount"]) for g in long_}
    for g in sorted(long_, key=lambda g: -g["months"]):
        need[g["key"]] = _fill(invest, g["key"], need[g["key"]])
    for g in sorted(long_, key=lambda g: g["months"]):
        need[g["key"]] = _fill(safe, g["key"], need[g["key"]])
        if need[g["key"]] > 1e-9:
            unfilled[g["key"]] = _r(need[g["key"]])
    for g in long_:
        from_invest = sum(r["assigned"].get(g["key"], 0.0) for r in invest)
        from_safe = sum(r["assigned"].get(g["key"], 0.0) for r in safe)
        if from_safe > 1e-9 and from_invest <= 1e-9:
            rebalance.append({"type": "투자 전환 검토", "goal": g["key"], "goal_name": g["name"], "amount": _r(from_safe),
                              "text": f"장기 목표분 {fm(from_safe)}은 단계별 비중에 맞춰 투자 전환 검토({g['name']})"})
    for r in rows:
        if r["left"] > 1e-9:
            r["assigned"]["unassigned"] = round(r["left"], 2)
        r.pop("left")
    locked_rows = [{"asset": l["label"], "kind": l["label"], "class": "보존", "balance": _r(l["balance"]),
                    "annual": l.get("annual"), "locked_goal": l["goal"], "matures_on": None, "liquidity": "해지손실",
                    "earmark": None, "balance_known": True, "assigned": {}, "converted": {}}
                   for l in (locked or []) if _n(l["balance"]) > 0 or _n(l.get("annual")) > 0]
    keys = ["emergency"] + [g["key"] for g in sorted(goals, key=lambda g: g["months"])]
    names = {"emergency": "비상금", **{g["key"]: g["name"] for g in goals}}
    cols = [{"key": k, "name": names[k]} for k in keys]
    if any("unassigned" in r["assigned"] for r in rows):
        cols.append({"key": "unassigned", "name": "미배정"})
    totals = {c["key"]: round(sum(r["assigned"].get(c["key"], 0.0) + r["converted"].get(c["key"], 0.0)
                                  for r in rows), 2) for c in cols}
    locked_totals = {}
    for l in locked_rows:
        locked_totals[l["locked_goal"]] = round(locked_totals.get(l["locked_goal"], 0.0) + _n(l["balance"]), 2)
    return {"columns": cols, "rows": rows, "locked_rows": locked_rows, "totals": totals,
            "total": round(sum(r["balance"] for r in rows), 2),
            "locked_totals": locked_totals, "locked_total": round(sum(_n(l["balance"]) for l in locked_rows), 2),
            "safe_total": round(sum(r["balance"] for r in safe), 2),
            "earmarked_total": round(sum(r["balance"] for r in rows if r["class"] == "용도 지정"), 2),
            "invest_total": round(sum(r["balance"] for r in invest), 2),
            "unfilled": unfilled, "rebalance": rebalance, "rule": SAFE_RULE, "principles": ["P01", "P03"]}


# ── 본체 ──────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, step2, profile, policy, cut, as_of, a, allocation):
        self.step2, self.profile, self.policy, self.cut, self.a = step2, profile, policy or {}, cut or {}, a
        self.as_of = as_of
        self.d = date.fromisoformat(as_of)
        self.alloc = allocation
        self.warnings: list[str] = []
        self.data_requests: list[dict] = []
        self.decisions: list[dict] = []
        self.detail = (step2.get("_meta") or {}).get("deltaM_detail") or {}
        self.age = None
        self.transition = None
        self.phases: list[dict] = []
        self.surplus_fn = lambda m: 0.0
        self.sim = None

    def ask(self, item, reason):
        if not any(d["item"] == item for d in self.data_requests):
            self.data_requests.append({"item": item, "reason": reason})


def _risk_profile(c: _Ctx) -> str:
    """투자성향은 계산에 쓰지 않는다. 위험 비중이 성향보다 높을 수 있다는 확인 사항에만 쓴다."""
    prof = fact(c.profile, "risk_profile")
    if prof is None:
        c.ask("투자성향", "단계별 위험 비중이 고객 손실 감내 수준에 맞는지 확인에 필요")
        return "미기재"
    return str(prof)


def _pre_tax_monthly(c: _Ctx) -> tuple[float | None, str]:
    """세전 월소득(만원)과 출처. 근로자는 총급여 ÷ 12, 사업자는 총수입금액(없으면 지급총액·사업소득금액) ÷ 12."""
    for basis, label in (("총급여", "총급여 ÷ 12"), ("총수입금액", "사업 총수입금액 ÷ 12"),
                         ("지급총액(원천징수영수증)", "사업 지급총액 ÷ 12"), ("사업소득금액", "사업소득금액 ÷ 12")):
        v = _income_annual(c.profile, basis)
        if v:
            return v / 12, label
    after = c.detail.get("income_monthly")
    c.warnings.append("세전 소득이 없어 양육비 구간을 세후 월소득으로 잡음")
    return after, "세후 월소득(세전 소득 없음)"


def child_support(c: _Ctx, children: int = 1) -> dict:
    """서울가정법원 양육비 산정기준표(2021 개정)로 월 양육비(만원) = 여섯 나이 구간 평균 × 자녀 수 계수 × 본인 부담."""
    ref = c.a["child_support_table"]
    income, src = _pre_tax_monthly(c)
    income = income or 0.0
    idx = max(i for i, lo in enumerate(ref["brackets"]) if income >= lo)
    hi = ref["brackets"][idx + 1] - 1 if idx + 1 < len(ref["brackets"]) else None
    by_age = {age: ref["table"][age][idx] for age in ref["ages"]}  # 천원
    avg = sum(by_age.values()) / len(by_age) / 10  # 만원
    key = "1" if children <= 1 else "2" if children == 2 else "3"
    factor = c.a["child_support_children_factor"][key]
    share = c.a["child_support_self_share"]
    monthly = avg * factor * share
    bracket = f"{ref['brackets'][idx]:,}~{hi:,}만원" if hi is not None else f"{ref['brackets'][idx]:,}만원 이상"
    return {"income_monthly": _r(income), "income_source": src, "bracket": bracket,
            "by_age_thousand_won": by_age, "average": _r(avg, 2), "children": children, "children_factor": factor,
            "self_share": share, "monthly": _r(monthly),
            "basis": f"세전 월소득 {fm(income)}({src}) → {bracket} 구간, 나이 구간 6개 평균 {fm(avg, 2)} × 자녀 수 계수 "
                     f"{factor:g} × 본인 부담 {share:g} = 월 {fm(monthly, 2)}",
            "source": "서울가정법원 양육비 산정기준표(2021 개정, 2026년 적용 기준)"}


def _transition(c: _Ctx) -> dict | None:
    """양육 전환 시점 T_c: 고객이 적은 양육·출산 시점, 없으면 통계상 출산 예상 시점. 자녀가 있으면 None."""
    if c.alloc.get("children_present"):
        return None
    g = next((x for x in c.alloc["goals"] if x.get("category", x["goal"]) == "child" and x.get("kind", "base") == "base"
              and x["active"]), None)
    if g is None:
        return None
    hs = g.get("horizon_source") or {}
    if hs.get("source") == "고객 목표" and g.get("need_source") != "통계 기본값":
        years, source = g["years"], "고객 목표"
    else:
        years = ((g.get("inputs") or {}).get("years_to_birth"))
        if years is None:
            years = g["years"]
        source = "통계 기본값(확인 필요)"
    month = max(round(years * 12), 1)
    return {"goal": g["goal"], "month": month, "date": add_months(c.d, month), "source": source}


EXTRA_KEY = "retire_extra"
OVERFLOW_EVENT = "주거 추가 적립(목표 시점 이후)"


def rebalance_phases(goals: list[dict], surplus: float, a: dict, *, child_cost: float = 0.0, tc: int | None = None,
                     t3: int | None = None, initial_ratios: dict | None = None, d: date | None = None,
                     overflow_key: str | None = None, extra_horizon: int | None = None) -> dict:
    """사건(목표 적립 완료·목표 시점 도래·양육 전환·자녀 19세)마다 남은 목표로 비율을 다시 계산하는 월 단위 모의.

    goals: [{'key','name','start','need','months'(목표 시점 달, 양육은 전환 달),'horizon'(수익률 단계용),'kind','child'}].
    사건 판정은 기준(base) 경로로 하고, 같은 달별 적립 일정을 하한·상한 경로에도 적용한다.
    단계 시작 비율: gap = need − 그 시점 기준 잔액(음수 0), years = 원래 목표 시점까지(하한 1년), weight = gap/years,
    0.1% 단위 최대 잔여법. 첫 단계는 initial_ratios(분배 비율)를 쓰되, 시작 달에 사건이 있으면 공식으로 다시 계산.
    남은 목표가 없으면: overflow_key(주거)가 목표 시점에 모자랐다면 그 잔액에 계속 적립(지연 달성까지, 단기 수익률),
    그 뒤(또는 주거가 없으면) extra_horizon(노후 시점)이 있으면 '추가 노후 재원'으로 쌓는다.
    → {'phases': [...], 'goals': {key: 결과}, 'extra': 추가 노후 재원}."""
    d = d or date.today()
    n = len(goals)
    bal = {sc: [float(g["start"]) for g in goals] for sc in SCENARIOS}
    path = {sc: [[float(g["start"])] for g in goals] for sc in SCENARIOS}
    contrib = [[] for _ in goals]
    active = set(range(n))
    res = [{"achieved_month": None, "exit_month": None, "exit_reason": None, "due_balance": None,
            "shortfall_at_due": None, "late_funded_month": None} for _ in goals]
    end = max([g["months"] for g in goals] + [t3 or 0, 1])
    phases: list[dict] = []
    ratios: dict[int, float] = {}
    hidx = next((i for i, g in enumerate(goals) if g["key"] == overflow_key), None)
    mode = "normal"
    extra = {"bal": {sc: 0.0 for sc in SCENARIOS}, "contrib": [], "start_month": None}

    grow = []  # 달 m부터 목표 시점까지 적립 없이 굴릴 때의 성장 배수(기준 경로)
    for g in goals:
        arr = [1.0] * (g["months"] + 1)
        for k in range(g["months"] - 1, -1, -1):
            r = _monthly_real(stage_of(g["kind"], (g["horizon"] - k) / 12, a)["rate"]["base"], a)
            arr[k] = arr[k + 1] * (1 + r)
        grow.append(arr)

    def cost_at(m):
        return child_cost if tc is not None and tc <= m < (t3 if t3 is not None else 10 ** 9) else 0.0

    for m in range(end + 1):
        events = []
        for i in sorted(active):
            g = goals[i]
            if not g.get("child") and m <= g["months"] and bal["base"][i] * grow[i][m] >= g["need"] - 1e-9:
                active.discard(i)
                res[i].update(achieved_month=m, exit_month=m, exit_reason="적립 완료")
                events.append(f"{g['name']} 적립 완료")
            elif m >= g["months"]:
                active.discard(i)
                res[i].update(exit_month=m, exit_reason="양육 전환" if g.get("child") else "목표 시점 도래")
                events.append("양육 전환" if g.get("child") else f"{g['name']} 목표 시점")
        for i, g in enumerate(goals):  # 목표 시점 잔액(적립 완료한 목표도 시점까지 굴린 값)
            if m == g["months"]:
                res[i]["due_balance"] = {sc: bal[sc][i] for sc in SCENARIOS}
                res[i]["shortfall_at_due"] = {sc: max(g["need"] - bal[sc][i], 0.0) for sc in SCENARIOS}
        if mode == "housing" and bal["base"][hidx] >= goals[hidx]["need"] - 1e-9:
            res[hidx]["late_funded_month"] = m
            events.append(f"{goals[hidx]['name']} 지연 달성")
            mode = "done_housing"
        if tc is not None and m == tc and "양육 전환" not in events:
            events.append("양육 전환")
        if t3 is not None and m == t3:
            events.append("자녀 19세(양육비 종료)")
        if m > end - 1 and not events:
            break
        if m == 0 or events:
            if m == 0 and not events and initial_ratios:
                use = {i: initial_ratios.get(goals[i]["key"], 0.0) for i in active}
                total = sum(use.values())
                ratios = {i: v for i, v in use.items() if v > 0} if abs(total - 100) < 0.05 else {}
            else:
                ratios = {}
            if not ratios and active:
                # years는 원래 목표 시점까지(양육도 전환 시점이 아니라 원래 양육 목표 시점, 첫 단계 분배와 같은 기준)
                weights = {i: max(goals[i]["need"] - bal["base"][i], 0.0) / max((goals[i]["horizon"] - m) / 12, 1)
                           for i in active}
                weights = {i: w for i, w in weights.items() if w > 0}
                if weights:
                    ratios = {i: v / 10 for i, v in largest_remainder(weights, 1000).items()}
            if not ratios:  # 남은 목표가 없으면 주거 추가 적립 → 추가 노후 재원
                if mode in ("normal", "housing") and hidx is not None and m >= goals[hidx]["months"] \
                        and bal["base"][hidx] < goals[hidx]["need"] - 1e-9:
                    if mode == "normal":
                        events.append(OVERFLOW_EVENT)
                    mode = "housing"
                    ratios = {hidx: 100.0}
                elif extra_horizon is not None and m < extra_horizon:
                    if mode != "extra":
                        events.append("추가 노후 재원 적립")
                        extra["start_month"] = m
                    mode = "extra"
                    ratios = {-1: 100.0}
            s = max(surplus - cost_at(m), 0.0)
            if phases:
                phases[-1].update(end_month=m, end=add_months(d, m))
            key_of = (lambda i: EXTRA_KEY if i == -1 else goals[i]["key"])
            phases.append({"phase": len(phases) + 1, "start_month": m, "start": add_months(d, m), "end_month": None,
                           "end": None, "events": events or ["시작"], "surplus": _r(s), "child_cost": _r(cost_at(m)),
                           "mode": mode,
                           "ratios": {key_of(i): ratios[i] for i in sorted(ratios)},
                           "monthly": {key_of(i): _r(s * ratios[i] / 100) for i in sorted(ratios)},
                           "unassigned": _r(s) if not ratios else 0.0,
                           "start_balances": {goals[i]["key"]: {sc: _r(bal[sc][i]) for sc in SCENARIOS}
                                              for i in range(n) if m < goals[i]["months"]
                                              or (mode == "housing" and i == hidx)}})
        if m >= end:
            break
        s = max(surplus - cost_at(m), 0.0)
        for i, g in enumerate(goals):
            rolling_after = mode == "housing" and i == hidx
            if m >= g["months"] and not rolling_after:
                continue
            add = s * ratios.get(i, 0.0) / 100 if (i in active or rolling_after) else 0.0
            contrib[i].append(add)
            for sc in SCENARIOS:
                r = _monthly_real(stage_of(g["kind"], (g["horizon"] - m) / 12, a)["rate"][sc], a)
                bal[sc][i] = bal[sc][i] * (1 + r) + add
                path[sc][i].append(bal[sc][i])
        if mode == "extra" and m < extra_horizon:
            add = s * ratios.get(-1, 0.0) / 100
            extra["contrib"].append((m, add))
            for sc in SCENARIOS:
                r = _monthly_real(stage_of("retire", (extra_horizon - m) / 12, a)["rate"][sc], a)
                extra["bal"][sc] = extra["bal"][sc] * (1 + r) + add
    return {"phases": phases,
            "goals": {g["key"]: {**res[i], "path": {sc: path[sc][i] for sc in SCENARIOS}, "contrib": contrib[i]}
                      for i, g in enumerate(goals)},
            "extra": {"start_month": extra["start_month"], "contrib": extra["contrib"],
                      "balance": {sc: _r(v) for sc, v in extra["bal"].items()}}}


def _project_goals(c: _Ctx) -> list[dict]:
    s2 = c.step2
    a_monthly = c.detail.get("a_monthly")
    if a_monthly is None:
        c.warnings.append("월 잉여 값이 없어 계획 월 적립을 0으로 둠")
    a1 = _n(a_monthly)
    locked = {"housing": _n(s2.get("p")), "retire": _n(s2.get("q")) + _n(s2.get("r")) + _n(s2.get("s"))}
    committed = {"housing": _n(s2.get("deltap")) / 12,
                 "retire": (_n(s2.get("deltaq")) + _n(s2.get("deltar")) + _n(s2.get("deltas"))) / 12}
    active = [g for g in c.alloc["goals"] if g["active"] and g["years"] is not None and g["need"] is not None]
    tr = _transition(c)
    c.transition = tr
    cost = child_support(c, 1) if tr is not None else None
    tc = tr["month"] if tr else None
    t3 = tc + c.a["child_transition_years"] * 12 if tr else None
    specs = []
    for g in active:
        months = max(round(g["years"] * 12), 1)
        cat = g.get("category", g["goal"])
        is_child = tr is not None and g["goal"] == tr["goal"]
        specs.append({"key": g["goal"], "name": g["name"], "need": g["need"], "kind": rate_kind(cat),
                      "start": _n(g.get("amount")) + locked.get(g["goal"], 0.0),
                      "months": min(tc, months) if is_child else months, "horizon": months, "child": is_child,
                      "g": g, "cat": cat, "target_months": months})
    housing = next((sp["key"] for sp in specs if sp["cat"] == "housing" and sp["g"].get("kind", "base") == "base"), None)
    retire = next((sp for sp in specs if sp["cat"] == "retire"), None)
    sim = rebalance_phases(specs, a1, c.a, child_cost=cost["monthly"] if cost else 0.0, tc=tc, t3=t3,
                           initial_ratios={g["goal"]: _n(g.get("ratio")) for g in active}, d=c.d,
                           overflow_key=housing, extra_horizon=retire["horizon"] if retire else None)
    c.sim = sim
    if cost is not None and a1 - cost["monthly"] < 0:
        c.warnings.append(f"양육 전환 뒤 월 잉여가 음수({fm(a1 - cost['monthly'])}) → 0으로 둠")
    phases = sim["phases"]
    for p in phases:
        p["name"] = f"{p['phase']}단계"
    out = []
    for sp in specs:
        k, g, kind, months = sp["key"], sp["g"], sp["kind"], sp["months"]
        r = sim["goals"][k]
        p1 = phases[0]["monthly"].get(k, 0.0) if phases else 0.0
        comm = committed.get(k, 0.0)
        new = _n(p1) - comm
        if new < -1e-9:
            c.warnings.append(f"{g['name']}: 계획 월 적립 {fm(p1)}이 기존 납입 {fm(comm)}보다 적음(신규 적립 {fm(new)})")
        need = g["need"]
        due = r["due_balance"] or {sc: r["path"][sc][-1] for sc in SCENARIOS}
        req = {sc: None if sp["child"] else required_monthly(sp["start"], need, months, kind, c.a, sc) for sc in SCENARIOS}
        cov = {sc: _r(due[sc] / need, 4) if need else None for sc in SCENARIOS}
        rule = term_of(g["years"])
        stated = g.get("term_stated")
        series_idx = sorted(set(list(range(0, months + 1, 12)) + [months]))
        by_phase = [{"phase": p["phase"], "start": p["start"], "ratio": p["ratios"].get(k),
                     "monthly": p["monthly"].get(k)} for p in phases if p["start_month"] < months]
        amounts = [x["monthly"] for x in by_phase if x["monthly"]]
        rec = {
            "goal": k, "name": g["name"], "category": sp["cat"], "label": g.get("label"),
            "term": stated or rule, "term_stated": stated, "term_rule": rule,
            "term_note": None if not stated or stated == rule else f"고객 표기 {stated}, 남은 기간 기준 {rule}",
            "need_source": g.get("need_source"), "reference_need": g.get("reference_need"),
            "need_diff": g.get("need_diff"), "due": g.get("due"), "raw": g.get("raw"),
            "kind": g.get("kind", "base"), "recurrence": g.get("recurrence"),
            "recurring_monthly": g.get("recurring_monthly"),
            "years": g["years"], "months": sp["horizon"], "horizon": sp["horizon"],
            "target": add_months(c.d, sp["horizon"]), "need": need, "existing_locked": _r(locked.get(k, 0.0)),
            "allocated": _r(_n(g.get("amount"))), "start": _r(sp["start"]), "ratio": g.get("ratio"),
            "planned_monthly": _r(p1), "committed_monthly": _r(comm), "new_monthly": _r(new),
            "monthly_by_phase": by_phase, "max_monthly": _r(max(amounts)) if amounts else 0.0,
            "rate_kind": kind,
            "rate_plan": rate_plan(months, kind, c.a, c.d, c.age, r["path"], horizon=sp["horizon"]),
            "fv_low": _r(due["low"]), "fv_base": _r(due["base"]), "fv_high": _r(due["high"]),
            "coverage": cov["base"], "coverage_low": cov["low"], "coverage_high": cov["high"],
            "shortfall": None if sp["child"] else _r(max(need - due["base"], 0.0)),
            "shortfall_low": None if sp["child"] else _r(max(need - due["low"], 0.0)),
            "shortfall_at_due": None if sp["child"] or r["shortfall_at_due"] is None
            else {sc: _r(v) for sc, v in r["shortfall_at_due"].items()},
            "achieved_month": None if r["achieved_month"] is None else add_months(c.d, r["achieved_month"]),
            "late_funded_month": None if r.get("late_funded_month") is None else add_months(c.d, r["late_funded_month"]),
            "funded_note": None if r["achieved_month"] is None else "이 시점 이후 적립 없이도 목표 시점에 필요자금 도달",
            "exit_reason": r["exit_reason"],
            "required_monthly": _r(req["base"]), "required_monthly_low": _r(req["low"]),
            "required_monthly_high": _r(req["high"]),
            "series": [{"year": j, "t": round(i / 12, 3), "label": c.d.year + j,
                        **{sc: _r(r["path"][sc][min(i, len(r["path"][sc]) - 1)]) for sc in SCENARIOS}}
                       for j, i in enumerate(series_idx)],
            "horizon_source": g.get("horizon_source"), "inputs": g.get("inputs"), "range": g.get("range"),
            "ends_at_transition": sp["child"], "years_late": g.get("years_late"), "late_label": g.get("late_label"),
        }
        if sp["child"]:
            reserve = {sc: _r(due[sc]) for sc in SCENARIOS}
            rec["transition"] = {**tr, "reserve": reserve, "child_cost": cost,
                                 "reserve_months": _r(reserve["base"] / cost["monthly"], 1) if cost["monthly"] else None,
                                 "child_end": add_months(c.d, t3)}
            rec["target"] = tr["date"]
            rec["months"] = months
        out.append(rec)
    # 양육 전환 뒤(KPI 보조 줄): 그 단계 시작 잔액에서 남은 목표의 필요 월 적립
    if tr is not None:
        ph = next((p for p in phases if "양육 전환" in p["events"]), None)
        if ph is not None:
            for rec in out:
                sb = ph["start_balances"].get(rec["goal"])
                if rec["goal"] in ph["ratios"] and sb:
                    rec["required_monthly_phase2"] = {
                        sc: _r(required_monthly(sb[sc], rec["need"], rec["months"] - ph["start_month"], rec["rate_kind"],
                                                c.a, sc)) for sc in SCENARIOS}
    c.phases = phases
    c.surplus_fn = (lambda m: a1 - (cost["monthly"] if cost and tc <= m < t3 else 0.0) if cost else a1)
    return out


def _options(c: _Ctx, goals: list[dict]) -> dict:
    sim = [g for g in goals if not g.get("ends_at_transition")]
    seq = sequential(sim, lambda m: max(c.surplus_fn(m), 0.0), c.a)
    rows = []
    for g, m in zip(sim, seq):
        sh = (g.get("shortfall_at_due") or {}).get("base")
        rows.append({
            "goal": g["goal"], "name": g["name"], "target": g["target"],
            "ratio_month": g["achieved_month"], "ratio_shortfall": None if g["achieved_month"] else sh,
            "ratio_on_time": g["achieved_month"] is not None and g["achieved_month"] <= g["target"],
            "sequential_month": None if m is None else add_months(c.d, m),
            "sequential_years": None if m is None else _r(m / 12),
            "sequential_on_time": None if m is None else m <= g["months"],
        })
    note = ("비율 배분은 목표 달성·목표 시점 도래·양육 전환·자녀 19세마다 남은 목표로 비율을 다시 계산한 결과. "
            "시점순은 월 잉여 전액을 시점이 가까운 목표부터 채움(기존 청약·연금 납입도 집중 대상에 포함한 단순화)")
    return {
        "monthly_total": _r(_n(c.detail.get("a_monthly"))),
        "ratio": {"label": "비율 배분(단계별 재계산)", "rule": "사건마다 남은 목표로 같은 공식의 비율을 다시 계산해 적립"},
        "sequential": {"label": "시점순 집중", "rule": "월 잉여 전액을 목표 시점이 가까운 목표부터 채우고, 달성하면 다음 목표로"},
        "rows": rows, "note": note, "principles": ["P16"],
    }


# ── 카드 ──────────────────────────────────────────────────────
def _card(id, term, title, action, numbers, basis, evidence, confirm, certainty, priority, goal=None):
    return {"id": id, "term": term, "goal": goal, "title": title, "action": action, "numbers": numbers,
            "basis": basis, "evidence": evidence, "confirm": confirm, "certainty": certainty, "priority": priority}


def _ref_ev(*keys):
    out = []
    for k in keys:
        src = STRATEGY_REFERENCE.get(k) or REFERENCE.get(k)
        out.append({"type": "reference", "ref": k, "url": (src or {}).get("url")})
    return out


def _emergency_goal(profile) -> dict | None:
    hits = [g for g in profile.get("goals", []) if EMERGENCY_RE.search(str(g.get("name") or ""))]
    return max(hits, key=lambda g: g.get("amount_won") or 0) if hits else None


def card_emergency(c: _Ctx, liquid_now: float, mapping: dict | None = None) -> dict:
    em = c.alloc["emergency"]
    g = _emergency_goal(c.profile)
    cust = _man(g.get("amount_won")) if g else None
    target = max(_n(em["amount"]), _n(cust))
    short = max(target - liquid_now, 0.0)
    confirm = []
    if em.get("capped"):
        confirm.append("비상금 필요액이 가용 자산보다 커서 분배 비상금이 줄어 있음")
    if g and cust is None:
        confirm.append(f"고객 목표 '{g.get('name')}'의 금액 미기재: 목표액 확인")
    if g and cust is not None and cust > _n(em["amount"]):
        confirm.append("고객이 적은 비상금 목표가 분배 비상금보다 큼: 차액을 분배 대상에서 뺄지 확인")
    deposit = sum(_man(x.get("balance_won")) or 0 for x in m_assets(c.profile)
                  if x.get("liquidity") == "즉시" and x.get("kind") in DEPOSIT_KINDS)
    invest = max(liquid_now - deposit, 0.0)
    # 자산 배정표 기준: 비상금은 안전 자산에서만 채우고 투자 자산은 쓰지 않는다
    parts, immediate, converted, maturing = [], 0.0, 0.0, []
    for r in (mapping or {}).get("rows", []):
        x = r["assigned"].get("emergency")
        if x:
            parts.append(f"{r['kind']} {fm(x)}")
            if r["liquidity"] == "즉시":
                immediate += x
            elif r.get("matures_on"):
                maturing.append(f"{r['kind']}(만기 {r['matures_on']})")
        converted += r["converted"].get("emergency", 0.0)
    mapped = sum(r["assigned"].get("emergency", 0.0) for r in (mapping or {}).get("rows", []))
    short_immediate = max(target - immediate, 0.0)
    action = (f"비상금 목표 {fm(target)}을 안전 자산 {' + '.join(parts)}으로 채웁니다." if parts
              else f"비상금 목표 {fm(target)}을 채울 안전 자산이 없습니다.") + " 투자 자산은 비상금에 쓰지 않습니다."
    if short_immediate > 0.005:
        action += f" 즉시 인출 가능한 안전 자산만으로는 {fm(short_immediate)}이 모자랍니다."
    if maturing:
        confirm.append(f"{', '.join(maturing)}은 만기 전 중도해지 조건 확인")
    if converted > 0.005:
        action += f" 투자 자산 {fm(converted)}을 안전 자산으로 전환해야 합니다."
        confirm.append(f"투자 자산 {fm(converted)} 안전 자산 전환 필요")
    if target - mapped - converted > 0.005:
        confirm.append(f"자산 배정 비상금 {fm(mapped)}이 비상금 목표보다 {fm(target - mapped - converted)} 적음")
    return _card("S1", "단기", "비상금 확보", action,
                 {"분배 비상금(만원)": em["amount"], "고객 목표 비상금(만원)": cust, "비상금 목표(만원)": _r(target),
                  "즉시 인출 자산(만원)": _r(liquid_now), "즉시 인출 예금성(만원)": _r(deposit),
                  "즉시 인출 투자자산·가격 변동(만원)": _r(invest), "부족액(만원)": _r(short),
                  "안전 자산 배정(만원)": _r(mapped), "즉시 인출 안전 자산 배정(만원)": _r(immediate),
                  "즉시 인출 안전 자산 부족(만원)": _r(short_immediate), "안전 전환 필요(만원)": _r(converted),
                  "월 소득 배수": None if em.get("months") else em.get("multiple"),
                  "월 지출, 소득 불규칙(만원)": em.get("monthly_spending"), "유지 개월": em.get("months")},
                 (f"소득이 불규칙한 고객(근로소득 아님): 비상금 = 월 지출 {fm(em.get('monthly_spending'))} × {em.get('months')}개월"
                  f"(생활비·보험·사업 필요경비·국민연금·건강보험 포함, 가용 자산 상한). 소득세·지방소득세 납부·적립은 뺌"
                  + (f"({', '.join(em.get('spending_excluded') or [])}만원)" if em.get('spending_excluded') else "")
                  + ". " if em.get("months") else
                  "비상금 목표 = 월 소득 × 배수와 고객 목표액 중 큰 값. ")
                 + "배정은 자산 배정표(안전 자산만, 즉시 인출 → 만기 빠른 순)",
                 _ref_ev("irregular_emergency_months" if em.get("months") else "emergency_multiple") + ([{"type": "profile", "ref": f"goals:{g.get('goal_id')}"}] if g else [])
                 + [{"type": "profile", "ref": "assets(liquidity=즉시)"}],
                 confirm, "계산값", 1, goal="emergency")


def card_debt(c: _Ctx, goals: list[dict] | None = None) -> dict | None:
    debts = c.profile.get("liabilities", [])
    dg = next((g for g in goals or [] if g.get("category") == "debt"), None)
    if not debts:
        return None
    safe = c.a["short_return"]["base"]
    rows, confirm, total, interest_total = [], [], 0.0, 0.0
    high = False
    for l in debts:
        bal, rate = _man(l.get("balance_won")), l.get("rate_pct")
        mi = None if bal is None or rate is None else _r(bal * rate / 100 / 12)
        if bal is None:
            confirm.append(f"부채 '{l.get('kind')}' 잔액 확인")
            c.ask(f"부채 잔액({l.get('kind')})", "부채 처리 검토에 필요")
        if rate is None:
            confirm.append(f"부채 '{l.get('kind')}' 금리 확인")
            c.ask(f"부채 금리({l.get('kind')})", "상환 우선순위 판단에 필요")
        if l.get("matures_on") is None:
            c.ask(f"부채 만기({l.get('kind')})", "만기 기준 기간 구분에 필요")
        if rate is not None and rate / 100 > safe:
            high = True
        total += _n(bal)
        interest_total += _n(mi)
        years = None
        if l.get("matures_on"):
            years = _r((date.fromisoformat(l["matures_on"]) - c.d).days / 365.25)
        rows.append({"kind": l.get("kind"), "balance": bal, "rate_pct": rate, "matures_on": l.get("matures_on"),
                     "years_to_maturity": years, "term": term_of(years) if years is not None else "확인 필요",
                     "repayment": l.get("repayment"), "monthly_interest": mi})
    D = c.alloc["distributable"]
    c.warnings.append(f"분배 대상(D) {fm(D)}은 부채 잔액 {fm(total)}을 빼지 않은 금액")
    goal = next((g for g in c.profile.get("goals", []) if re.search(r"대출|부채|상환", str(g.get("name") or ""))), None)
    action = (f"부채 잔액 {fm(total)} 중 금리가 안전 수익률 {safe * 100:g}%보다 높은 부채가 있어 상환 재원 우선 확보를 검토합니다."
              if high else f"부채 잔액 {fm(total)}의 금리·만기를 확인하고 상환 계획을 함께 검토합니다.")
    confirm.append("분배 대상(D)에서 부채를 뺄지(상환 우선) 결정")
    plan_numbers = {}
    if dg is not None:
        action += (f" 고객 상환 목표 {fm(dg['need'])}({dg['target']})에 계획 월 적립 {fm(dg['planned_monthly'])}을 "
                   f"위험자산 없이 모으면 충족률 {_n(dg['coverage']) * 100:.1f}%입니다.")
        plan_numbers = {"상환 목표 시점": dg["target"], "상환 재원 계획 월 적립(만원)": dg["planned_monthly"],
                        "상환 재원 필요 월 적립(만원)": dg["required_monthly"],
                        "상환 재원 충족률(%)": None if dg["coverage"] is None else _r(dg["coverage"] * 100, 1)}
    return _card("S2", "단기", "부채 점검", action,
                 {"부채 합계(만원)": _r(total), "월 이자 합계(만원)": _r(interest_total), "분배 대상 D(만원)": D,
                  "안전 수익률(%)": safe * 100,
                  "고객 상환 목표(만원)": _man(goal.get("amount_won")) if goal else None, **plan_numbers,
                  "rows": rows},
                 "월 이자 = 잔액 × 금리 ÷ 12. 금리 > 안전 수익률이면 상환 재원 우선 확보 검토",
                 _ref_ev("short_return") + [{"type": "profile", "ref": "liabilities"}],
                 confirm, "계산값" if not any(r["rate_pct"] is None for r in rows) else "확인 필요",
                 2 if high else 5, goal="debt")


def card_maturity(c: _Ctx, mapping: dict, names: dict) -> dict | None:
    limit = add_months(c.d, 36)
    rows = [r for r in mapping["rows"] if r["matures_on"] and r["matures_on"][:7] <= limit]
    if not rows:
        return None
    items = [{"asset": r["asset"], "balance": r["balance"], "matures_on": r["matures_on"],
              "assigned": {names.get(k, k): v for k, v in r["assigned"].items()}} for r in rows]
    total = sum(r["balance"] for r in rows)
    first = min(rows, key=lambda r: r["matures_on"])
    return _card("S3", "단기", "3년 안 만기 자금", f"3년 안에 만기가 오는 자산 {fm(total)}을 목적별 배정표에 따라 "
                 f"이어서 운용할 곳을 미리 정합니다. 가장 빠른 만기는 {first['matures_on']}입니다.",
                 {"만기 자산 합(만원)": _r(total), "건수": len(rows), "rows": items},
                 "기준일부터 36개월 안에 만기가 오는 금융자산. 목적은 자산 목적별 배정표를 따름",
                 [{"type": "profile", "ref": "assets(matures_on)"}], ["만기 후 재예치·이전 의사 확인"], "계산값", 6)


def card_cut(c: _Ctx) -> dict | None:
    detail = c.detail
    beh = detail.get("cut_monthly")
    pol = detail.get("policy_cut_monthly") or {}
    pol_total = _n(pol.get("variable")) + _n(pol.get("fixed"))
    refl = (c.step2.get("_meta") or {}).get("policy_reflection") or {}
    applied = refl.get("applied_policies") or []
    if not _n(beh) and not pol_total and not applied:
        return None
    ev = [{"type": "policy", "ref": p.get("policy_id"), "name": p.get("policy_name"), "url": p.get("url")} for p in applied]
    rows = [{"policy": p.get("policy_name"), "item": p.get("item"), "monthly": p.get("monthly_saving"),
             "verdict": p.get("verdict"), "url": p.get("url")} for p in applied]
    confirm = ["소비 조정 항목별 고객 동의"] + [f"정책 '{p.get('policy_name')}' 신청 조건: " + "; ".join(p["open_conditions"])
                                       for p in applied if p.get("open_conditions")]
    return _card("S4", "단기", "지출 절감 실행",
                 f"행동 절감 월 {fm(beh)}과 정책 절감 월 {fm(pol_total)}을 실행하면 월 잉여가 그만큼 늘어난 것으로 계산했습니다.",
                 {"행동 절감(만원/월)": beh, "정책 절감 변동(만원/월)": pol.get("variable"),
                  "정책 절감 고정(만원/월)": pol.get("fixed"), "정책 절감 합(만원/월)": _r(pol_total), "rows": rows},
                 "행동 절감 = 소비 습관 조정액, 정책 절감 = 지출액을 넘지 않게 상한을 둔 정책 월 절감",
                 ev + [{"type": "profile", "ref": "cashflow_items"}], confirm,
                 "확인 필요" if any(p.get("verdict") == "확인 필요" for p in applied) else "계산값", 3)


def card_apply(c: _Ctx) -> dict | None:
    sep = c.cut.get("separate") or {}
    rows = []
    for o in sep.get("one_off_income", []):
        eff = o.get("effect") or {}
        lo, hi = (eff.get("one_off_won") or [None, None])[:2]
        rows.append({"policy": o["policy_name"], "kind": "일시금", "certainty": o.get("certainty"),
                     "amount": _man(hi) if lo == hi else None,
                     "amount_text": None if lo is None else (fm(_man(hi)) if lo == hi else f"{fm(_man(lo))}~{fm(_man(hi))}"),
                     "deadline": (o.get("action") or {}).get("deadline"), "url": (o.get("action") or {}).get("url"),
                     "policy_id": o.get("policy_id")})
    for o in sep.get("one_off_on_lines", []):
        rows.append({"policy": o["policy_name"], "kind": f"일시금({o.get('item')})", "certainty": o.get("verdict"),
                     "amount": None, "amount_text": o.get("benefit_summary"),
                     "deadline": (o.get("apply") or {}).get("deadline"), "url": (o.get("apply") or {}).get("url"),
                     "policy_id": o.get("policy_id")})
    for o in sep.get("savings_programs", []):
        rows.append({"policy": o["policy_name"], "kind": o.get("type"), "certainty": o.get("certainty"),
                     "amount": None, "amount_text": o.get("benefit_summary"),
                     "deadline": (o.get("action") or {}).get("deadline"), "url": (o.get("action") or {}).get("url"),
                     "policy_id": o.get("policy_id")})
    if not rows:
        return None
    for r in rows:
        d = str(r["deadline"] or "")
        r["deadline"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if re.fullmatch(r"\d{8}", d) else (d or None)
    rows.sort(key=lambda r: (r["certainty"] not in ("시나리오 사용 가능", "입력상 충족"), r["deadline"] or "9999"))
    sure = sum(r["certainty"] in ("시나리오 사용 가능", "입력상 충족") for r in rows)
    return _card("S5", "단기", "정책 신청 검토",
                 f"일시금·저축 상품 정책 {len(rows)}건(입력상 충족 {sure}건)의 신청 여부를 검토합니다. 전망 숫자에는 넣지 않았습니다.",
                 {"건수": len(rows), "입력상 충족": sure, "rows": rows},
                 "일시금·저축 상품 정책. 선정 전이라 전망과 월 잉여에 넣지 않음",
                 [{"type": "policy", "ref": r["policy_id"], "name": r["policy"], "url": r["url"]} for r in rows],
                 ["정책별 신청 조건·마감일 공고 확인", "저축 상품은 본인 저축 재원 필요"], "확인 필요", 4)


def _goal_numbers(g: dict) -> dict:
    return {"필요자금(만원)": g["need"], "기존 보존자산(만원)": g["existing_locked"], "분배액(만원)": g["allocated"],
            "분배 비율(%)": g["ratio"], "계획 월 적립(만원)": g["planned_monthly"],
            "기존 납입(만원/월)": g["committed_monthly"], "신규 적립(만원/월)": g["new_monthly"],
            "하한 수익률 목표 시점 잔액(만원)": g["fv_low"], "기준 수익률 목표 시점 잔액(만원)": g["fv_base"],
            "상한 수익률 목표 시점 잔액(만원)": g["fv_high"],
            "충족률(%)": None if g["coverage"] is None else _r(g["coverage"] * 100, 1),
            "충족률 하한(%)": None if g["coverage_low"] is None else _r(g["coverage_low"] * 100, 1),
            "충족률 상한(%)": None if g["coverage_high"] is None else _r(g["coverage_high"] * 100, 1),
            "부족액(만원)": g["shortfall"], "필요 월 적립(만원)": g["required_monthly"],
            "필요 월 적립 하한 수익률(만원)": g["required_monthly_low"],
            "달성 예상 시점": g.get("achieved_month"),
            "목표 시점 부족액(만원)": None if g.get("achieved_month") else (g.get("shortfall_at_due") or {}).get("base"),
            "최대 월 적립(만원)": g.get("max_monthly"),
            "단계별 월 적립": phase_text(g),
            "목표 시점": g["target"], "남은 기간(년)": g["years"]}


def phase_text(g: dict) -> str:
    """'79.9(2026-11) → 57.8(2028-04) → 0(달성)'처럼 단계별 월 적립 변화."""
    parts, last = [], None
    for x in g.get("monthly_by_phase") or []:
        v = _n(x["monthly"])
        if last is None or abs(v - last) > 0.005:
            parts.append(f"{v:,.1f}({x['start']})")
            last = v
    return " → ".join(parts)


def plan_text(plan: list[dict]) -> str:
    """운용 구간을 한 줄로: '성장 운용 연 6.5%(5~8%) 2026-11~2028-11 → 단기 운용 …'."""
    parts = []
    for p in plan:
        n = p["nominal"]
        rng = f"{n['low']:g}~{n['high']:g}%" if n["low"] != n["high"] else f"{n['base']:g}%"
        risk = f" 위험:안전 {p['risk_ratio']}" if p["risk_ratio"] else ""
        parts.append(f"{p['stage']} 연 {n['base']:g}%({rng}){risk} {p['start']}~{p['end']}")
    return " → ".join(parts)


def _goal_action(g: dict) -> str:
    p1, top = _n(g["planned_monthly"]), _n(g.get("max_monthly"))
    head = f"{g['name']} 목표({g['target']})는 월 {fm(p1)} 적립으로 시작합니다. "
    if top > p1 + 0.005:
        head += f"이전 목표가 끝나면 그 몫이 넘어와 월 적립이 {fm(p1)}에서 {fm(top)}으로 바뀝니다. "
    if g.get("achieved_month"):
        tail = (f"{g['achieved_month']}부터는 적립 없이도 목표 시점에 필요자금에 닿을 것으로 보여 적립을 멈춥니다"
                f"(목표 시점 충족률 {_n(g['coverage']) * 100:.1f}%).")
    else:
        sh = (g.get("shortfall_at_due") or {}).get("base")
        tail = (f"목표 시점 충족률 {_n(g['coverage']) * 100:.1f}%로 목표 시점에 {fm(sh)}이 모자랍니다. "
                f"지금부터 일정액으로 맞추려면 월 {fm(g['required_monthly'])}이 필요합니다.")
    return head + tail


def card_goal(c: _Ctx, g: dict, priority: int) -> dict:
    confirm, ev = [], _ref_ev("short_return", "growth_return" if g.get("rate_kind") != "retire" else "retire_stages",
                              "inflation")
    hs = g.get("horizon_source") or {}
    numbers = _goal_numbers(g)
    numbers["구분"] = "기본 목표" if g.get("kind", "base") == "base" else "추가 목표"
    if g.get("recurrence") == "annual":
        numbers["매년 반복 월 적립(만원)"] = g.get("recurring_monthly")
        confirm.append("매년 반복 목표: 달성 뒤에도 같은 월 적립이 계속 필요")
    title = f"{g['name']} 목표 운용"
    basis = (f"시작 잔액 = 분배액 + 기존 보존자산. 월 복리·월말 적립, 명목 수익률에서 물가를 뺀 실질 수익률. "
             f"운용: {plan_text(g['rate_plan'])}. "
             "필요 월 적립 = (필요자금 − 적립 없이 굴린 시작 잔액) ÷ 월 1만원 적립의 목표 시점 잔액")
    if hs.get("source") == "고객 목표":
        ev.append({"type": "profile", "ref": f"goals:{hs.get('goal_id')}"})
    cat = g.get("category", g["goal"])
    customer = g.get("need_source") == "고객 금액"
    if g.get("need_source"):
        numbers["필요자금 출처"] = g["need_source"]
        numbers["통계 참고 필요자금(만원)"] = g.get("reference_need")
        numbers["고객 목표 − 통계(만원)"] = g.get("need_diff")
    if g.get("term_note"):
        basis += f" 기간: {g['term_note']}."
    if cat == "housing":
        inp = g.get("inputs") or {}
        price, loan, rate = inp.get("house_price"), inp.get("loan"), inp.get("purchase_cost_rate")
        prefix = "참고 " if customer else ""
        numbers.update({f"{prefix}매매가(만원)": price, f"{prefix}대출(만원)": loan,
                        f"{prefix}부대비용(만원)": None if price is None or rate is None else _r(price * rate),
                        "청약 잔액 p(만원)": c.step2.get("p"), "청약 연 납입 deltap(만원)": c.step2.get("deltap")})
        basis = ("필요자금 = 고객이 적은 주거 자기자금 목표(매매가·대출은 통계 기준 참고값). " if customer
                 else "필요자금 = 매매가 − 대출 + 매매가 × 부대비용률. ") + basis
        ev += _ref_ev("house_price_sido" if inp.get("sido") in (REFERENCE["house_price_sido"]["value"] or {}) and
                      REFERENCE["house_price_sido"]["value"].get(inp.get("sido")) else "house_price_national",
                      "ltv", "purchase_cost_rate", "mortgage_cap")
        confirm.append("청약 납입 유지 여부와 매수 지역·가격대 확인")
        if g.get("need_source") == "통계 대체":
            confirm.append("필요자금은 고객 희망 주택 가격·자기자금 확인 전 통계값")
            basis = "필요자금은 고객 희망 주택 가격·자기자금을 확인하기 전 통계값(시도 평균 매매가 기준). " + basis
        if g.get("years_late"):
            late_m = max(round(g["years_late"] * 12), 1)
            req_late = required_monthly(g["start"], g["need"], late_m, g["rate_kind"], c.a)
            fv_late = project(g["start"], g["planned_monthly"], late_m, g["rate_kind"], c.a)[0]
            numbers.update({f"늦은 시점({g['late_label']}) 기준 필요 월 적립(만원)": _r(req_late),
                            f"늦은 시점({g['late_label']}) 기준 충족률(%)": _r(fv_late / g["need"] * 100, 1)})
            confirm.append(f"시점 범위 '{g['due']}': 이른 해로 계산함. 늦은 해({g['late_label']})면 필요 월 적립 "
                           f"{fm(req_late)}, 1단계 월 적립 유지 시 충족률 {fv_late / g['need'] * 100:.1f}%")
            c.decisions.append({"topic": f"주거 목표 시점 범위 '{g['due']}'",
                                "detail": f"이른 해 기준 필요 월 적립 {fm(g['required_monthly'])}, 충족률 {_n(g['coverage']) * 100:.1f}%. "
                                          f"늦은 해({g['late_label']}) 기준 필요 월 적립 {fm(req_late)}, "
                                          f"충족률 {fv_late / g['need'] * 100:.1f}%(1단계 월 적립 유지)",
                                "options": ["이른 해로 계획", "늦은 해로 계획", "고객과 시점 확정"]})
        title = "주거 자금 마련"
    elif cat == "retire":
        inp = g.get("inputs") or {}
        s2 = c.step2
        annual = _n(s2.get("deltaq")) + _n(s2.get("deltar")) + _n(s2.get("deltas"))
        now = g["rate_plan"][0] if g["rate_plan"] else None
        numbers.update({"보존자산 잔액, 연금저축·IRP·변액(만원)": g["existing_locked"],
                        "보존자산 연 납입(만원)": _r(annual),
                        "현재 단계 위험:안전": now["risk_ratio"] if now else None,
                        "현재 단계 명목 수익률(%)": now["nominal"]["base"] if now else None})
        confirm.append(f"연금 계좌 안의 운용 비중을 현재 단계({now['risk_ratio'] if now else '미확인'})에 맞는지 점검")
        confirm.append("단계별 위험 비중이 고객 투자성향보다 높을 수 있음: 손실 감내 확인")
        confirm.append("연금계좌 세액공제(L1)와 함께 검토")
        numbers.update({"은퇴 연령": inp.get("retire_age"),
                        ("고객 월 생활비(만원)" if customer else "월 생활비 가정(만원)"): inp.get("living_min"),
                        "국민연금 가정(만원/월)": inp.get("pension"), "보존자산 q+r+s(만원)": g["existing_locked"]})
        if fact(c.profile, "national_pension_expected") is None:
            confirm.append("국민연금 예상 수령액 미확인: 가입내역 조회 결과로 노후 필요자금 다시 계산")
            c.ask("국민연금 예상 수령액", "노후 필요자금은 평균 수급액을 가정해 계산함")
        ev += _ref_ev("retire_min_living_single", "nps_old_age_avg_monthly", "pension_start_age")
        title = "노후 자금 준비"
    elif cat == "child":
        title = "교육 자금 준비" if g["name"] in ("교육", "교육자금") or "교육" in (g.get("label") or "") else "양육 자금 준비"
        if hs.get("source") != "고객 목표":
            confirm.append("고객이 적지 않은 목표, 포함 여부 확인")
        ev += _ref_ev("child_cost_to_college", "cpi_annual", "child_self_share")
        tr = g.get("transition")
        if tr:
            cost = tr["child_cost"]
            ph2 = next((p for p in c.phases if "양육 전환" in p["events"]), {})
            numbers = {"전환 시점": tr["date"], "전환 근거": tr["source"], "전환 전 계획 월 적립(만원)": g["planned_monthly"],
                       "분배 비율(%)": g["ratio"], "양육 준비금 하한(만원)": tr["reserve"]["low"],
                       "양육 준비금 기준(만원)": tr["reserve"]["base"], "양육 준비금 상한(만원)": tr["reserve"]["high"],
                       "준비금, 월 양육비 개월분": tr["reserve_months"], "월 양육비(만원)": cost["monthly"],
                       "양육비 소득 구간": cost["bracket"], "전환 뒤 월 잉여(만원)": ph2.get("surplus"),
                       "자녀 19세(양육비 끝)": tr["child_end"]}
            ratios = ", ".join(f"{GOAL_LABEL.get(k, k)} {v:g}%" for k, v in (ph2.get("ratios") or {}).items())
            numbers["전환 뒤 비율"] = ratios
            basis = ("양육은 전환 시점까지만 목표로 적립하고, 그 뒤 월 양육비를 변동지출로 처리한다. "
                     f"월 양육비: {cost['basis']}. 전환 뒤 남은 목표 비율 = (필요자금 − 전환 시점 예상 잔액) ÷ 남은 기간의 비중")
            confirm = [x for x in confirm if x != "고객이 적지 않은 목표, 포함 여부 확인"]
            if tr["source"] != "고객 목표":
                confirm.append("전환 시점은 통계상 출산 예상 시점: 출산 계획 확인")
            confirm += ["배우자 소득 확인 시 양육비 소득 구간 상향 가능",
                        "양육 준비금 처리: 양육비 충당 또는 다른 목표로 재배정 결정",
                        "양육비 산정기준표는 일부 값만 대조함: 법원 원문 표로 전체 대조 필요"]
            ev += _ref_ev("child_support_table", "child_support_children_factor", "child_support_self_share")
    elif cat == "wedding":
        title = "결혼 자금 준비"
        ev += _ref_ev("wedding_cost_ex_housing", "wedding_self_share")
        if hs.get("source") != "고객 목표":
            confirm.append("결혼 시점은 통계 연령으로 잡음: 고객 계획 확인")
    elif cat == "other":
        title = f"{g['name']} 준비"
        if g.get("label"):
            confirm.append(f"고객 목표 원문: {g['label']}")
    if g.get("need_source") == "통계 대체":
        confirm.append("고객 목표에 금액이 없어 통계값으로 계산: 목표 금액 확인")
    if g["new_monthly"] is not None and g["new_monthly"] < 0:
        confirm.append("계획 월 적립이 기존 납입보다 적음: 기존 납입 조정 여부 확인")
    certainty = "확인 필요" if (hs.get("source") != "고객 목표" and cat in ("child", "wedding")) or \
        g.get("need_source") == "통계 대체" else "가정 포함"
    card_id = GOAL_CARD_ID.get(g["goal"]) or f"G-{g['goal']}"
    action = _goal_action(g)
    over = next((p for p in c.phases if p.get("mode") == "housing"), None)
    if cat == "housing" and over is not None:
        action += (f" {', '.join(e for e in over['events'] if e != '주거 추가 적립(목표 시점 이후)')} 뒤 {over['start']}부터 "
                   f"월 {fm(over['monthly'].get(g['goal']))}이 주거로 넘어와 "
                   + (f"{g['late_funded_month']}에 필요자금에 닿습니다(지연 달성)." if g.get("late_funded_month")
                      else "노후 시점까지 적립해도 필요자금에 닿지 못합니다."))
        numbers["지연 달성 예상 시점"] = g.get("late_funded_month") or "노후 시점까지 미달"
    if cat == "child" and g.get("transition"):
        tr = g["transition"]
        ph2 = next((p for p in c.phases if "양육 전환" in p["events"]), {})
        action = (f"양육은 {tr['date']}에 재무목표에서 빼고 월 양육비 {fm(tr['child_cost']['monthly'])}을 변동지출로 처리합니다. "
                  f"그때까지 월 {fm(g['planned_monthly'])}씩 모아 양육 준비금 {fm(tr['reserve']['base'])}"
                  f"(월 양육비 {tr['reserve_months']:g}개월분)이 됩니다. 전환 뒤 월 잉여 {fm(ph2.get('surplus'))}을 남은 목표에 "
                  f"다시 나눕니다.")
    if cat == "retire":
        now = g["rate_plan"][0] if g["rate_plan"] else None
        annual = _n(c.step2.get("deltaq")) + _n(c.step2.get("deltar")) + _n(c.step2.get("deltas"))
        action += (f" 연금저축·IRP·변액 잔액 {fm(g['existing_locked'])}과 연 납입 {fm(annual)}을 노후 재원에 포함했습니다."
                   + (f" 현재 단계({now['risk_ratio']})에 맞춰 연금 계좌 안의 운용 비중을 점검합니다." if now else ""))
    elif cat in ("housing", "child") and g.get("rate_kind") == "growth" and g["months"] > SHORT_HORIZON_YEARS * 12:
        confirm.append("성장 운용(연 5~8%) 비중이 고객 투자성향보다 높을 수 있음: 손실 감내 확인")
    if g.get("recurrence") == "annual" and g.get("recurring_monthly") is not None:
        action += f" 매년 반복 목표라 달성 뒤에도 월 {fm(g['recurring_monthly'])}을 계속 적립해야 합니다."
    return _card(card_id, g["term"], title, action, numbers, basis,
                 ev, confirm, certainty, priority, goal=g["goal"])


def card_mortgage(c: _Ctx, g: dict) -> dict | None:
    inp = g.get("inputs") or {}
    loan = inp.get("loan")
    a = c.a
    loans = [f for f in c.policy.get("findings", [])
             if f["benefit"]["type"] == "대출" and "주거" in str(f.get("category") or "")]
    rows = [{"policy": f["policy_name"], "verdict": f["verdict"], "limit": _man(f["benefit"].get("loan_limit_won")),
             "url": f["apply"].get("url"), "policy_id": f["policy_id"]} for f in loans]
    salary = _income_annual(c.profile, "총급여")
    rent = _man(fact(c.profile, "rent_monthly"))
    pay = pay_s = dsr = dsr_s = None
    if loan:
        pay = annuity_payment(loan, a["mortgage_rate"], a["mortgage_years"])
        pay_s = annuity_payment(loan, a["mortgage_rate"] + a["dsr_stress_add"], a["mortgage_years"])
        if salary:
            dsr, dsr_s = pay * 12 / salary, pay_s * 12 / salary
    confirm = ["대출 상품별 자격·한도·금리 공고 확인", "기존 대출 원리금은 DSR에 넣지 않음"]
    if salary is None:
        confirm.append("세전 연소득(총급여) 미확인: DSR 계산 못 함")
        c.ask("세전 연소득(소득금액증명 등)", "간이 DSR 계산에 필요")
    if not loan and not rows:
        return None
    action = (f"주택 대출 {fm(loan)}을 30년 원리금균등으로 갚으면 월 {fm(pay)}이고 간이 DSR은 "
              + (f"{dsr * 100:.1f}%(스트레스 {dsr_s * 100:.1f}%)입니다. " if dsr is not None else "계산하지 못했습니다. ")
              + f"주거 대출 정책 {len(rows)}건을 함께 검토합니다.") if loan else f"주거 대출 정책 {len(rows)}건을 검토합니다."
    own = {}
    if g.get("need_source") == "고객 금액" and g.get("reference_need") is not None:
        own = {"고객 자기자금 목표(만원)": g["need"], "통계 자기자금(만원)": g["reference_need"],
               "자기자금 차이(만원)": g.get("need_diff")}
        action += (f" 대출액은 통계 매매가 기준 참고값입니다. 고객 자기자금 목표 {fm(g['need'])}은 통계 자기자금 "
                   f"{fm(g['reference_need'])}과 {fm(abs(g.get('need_diff') or 0))} 차이가 납니다.")
    return _card("M2", g["term"], "주택 대출 검토", action,
                 {**own, "대출액(만원)": loan, "금리(%)": a["mortgage_rate"] * 100, "월 원리금(만원)": _r(pay),
                  "스트레스 금리(%)": round((a["mortgage_rate"] + a["dsr_stress_add"]) * 100, 2),
                  "스트레스 월 원리금(만원)": _r(pay_s), "세전 연소득(만원)": salary,
                  "DSR(%)": None if dsr is None else _r(dsr * 100, 1),
                  "스트레스 DSR(%)": None if dsr_s is None else _r(dsr_s * 100, 1), "DSR 기준(%)": a["dsr_limit"] * 100,
                  "현재 월세(만원)": rent, "월 원리금 − 월세(만원)": None if pay is None or rent is None else _r(pay - rent),
                  "rows": rows},
                 "월 원리금 = 30년 원리금균등 상환액(월 이율 = 연 금리 ÷ 12). DSR = 연 원리금 ÷ 세전 연소득(총급여). "
                 "대출액은 분배 단계의 주거 대출(오늘 가격 기준)",
                 _ref_ev("mortgage_rate", "mortgage_years", "dsr_stress_add", "dsr_limit", "mortgage_cap")
                 + [{"type": "policy", "ref": r["policy_id"], "name": r["policy"], "url": r["url"]} for r in rows]
                 + [{"type": "profile", "ref": "income_items:총급여"}],
                 confirm, "가정 포함" if dsr is not None else "확인 필요", 2, goal="housing")


def card_isa(c: _Ctx, goals: list[dict]) -> dict | None:
    mids = [g for g in goals if g["years"] > c.a["isa_min_years"]]
    if not mids:
        return None
    a = c.a
    glides = {g["name"]: plan_text(g["rate_plan"]) for g in goals if g["term"] == "중기"}
    planned = sum(_n(g["planned_monthly"]) for g in goals if g["term"] == "중기")
    return _card("M3", "중기", "중기 자금 운용과 ISA",
                 f"3년이 넘는 목표 자금은 남은 기간에 맞춰 위험 비중을 줄여 가는 경로로 운용하고, ISA(연 한도 {fm(a['isa_annual_limit'], 0)}) "
                 "활용 가능성을 검토합니다.",
                 {"ISA 연 납입 한도(만원)": a["isa_annual_limit"], "ISA 비과세 한도(만원)": a["isa_taxfree"],
                  "초과분 분리과세(%)": round(a["isa_separate_tax"] * 100, 1), "의무 가입기간(년)": a["isa_min_years"],
                  "중기 목표 계획 월 적립 합(만원)": _r(planned), "연 환산(만원)": _r(planned * 12),
                  "rate_plans": glides},
                 "위험 비중 = 남은 기간별 기본 비중 × 투자성향 계수(상한 80%), 매년 다시 정함",
                 _ref_ev("isa_annual_limit", "isa_taxfree", "isa_separate_tax", "isa_min_years", "growth_return",
                         "short_return"),
                 ["가입 요건·한도 원문 확인", "투자성향 적합성 확인"], "가정 포함", 3)


def card_pension(c: _Ctx, retire: dict | None) -> dict:
    a = c.a
    salary = _income_annual(c.profile, "총급여")
    biz = _income_annual(c.profile, "사업소득금액")
    rate, rule = None, None
    if salary is not None:
        rate = a["pension_credit_rate_low"] if salary <= a["pension_salary_threshold"] else a["pension_credit_rate_high"]
        rule = f"총급여 {fm(salary)} {'≤' if salary <= a['pension_salary_threshold'] else '>'} {fm(a['pension_salary_threshold'], 0)}"
    elif biz is not None:
        rate = a["pension_credit_rate_low"] if biz <= a["pension_income_threshold"] else a["pension_credit_rate_high"]
        rule = (f"사업소득금액 {fm(biz)}을 종합소득금액 근사로 씀 "
                f"({'≤' if biz <= a['pension_income_threshold'] else '>'} {fm(a['pension_income_threshold'], 0)})")
    else:
        c.ask("총급여 또는 종합소득금액", "연금계좌 세액공제율 판정에 필요")
    planned = None if retire is None else _n(retire["planned_monthly"]) * 12
    tax = _decided_tax(c.profile)
    pc = pension_credit(c.step2.get("deltaq"), c.step2.get("deltar"), planned, rate, tax, a)
    confirm = ["연금계좌 세액공제 한도·공제율 원문 확인", "연금계좌는 중도 인출 시 기타소득세: 유동성 확인"]
    if tax is None:
        confirm.append("결정세액 미확인: 추가 공제가 세금보다 클 수 있음")
        c.ask("결정세액(연말정산·종합소득세)", "세액공제 상한 확인에 필요")
    if pc["capped_by_tax"]:
        confirm.append("추가 공제액이 결정세액에 막힘")
    if rate is None:
        action = "공제율을 정할 소득 자료가 없어 연금계좌 세액공제를 계산하지 못했습니다."
    else:
        action = (f"현재 연금계좌 연 납입 {fm(pc['current_annual'])}의 공제액은 {fm(pc['current_credit'])}이고 "
                  f"남은 공제 한도는 {fm(pc['remaining_limit'])}입니다.")
        if pc["additional_credit"] is not None:
            action += f" 노후 계획 적립을 연금계좌로 넣으면 공제가 연 {fm(pc['additional_credit'])} 늘어나는 것으로 계산됩니다."
    return _card("L1", "장기", "연금계좌 세액공제", action,
                 {"공제율(%)": None if rate is None else round(rate * 100, 1), "공제율 판정": rule,
                  "현재 연 납입 q+r(만원)": pc["current_annual"], "공제 대상(만원)": pc["current_eligible"],
                  "남은 한도(만원)": pc["remaining_limit"], "현재 공제액(만원)": pc["current_credit"],
                  "노후 계획 적립 연 환산(만원)": pc["planned_annual"], "계획 공제 대상(만원)": pc["planned_eligible"],
                  "계획 공제액(만원)": pc["planned_credit"], "추가 공제 상한 전(만원)": pc["additional_credit_raw"],
                  "결정세액(만원)": pc["decided_tax"], "추가 공제(만원)": pc["additional_credit"],
                  "연금저축 한도(만원)": a["pension_saving_limit"], "합산 한도(만원)": a["pension_total_limit"]},
                 "공제 대상 = 연금저축(최대 600) + IRP, 합산 최대 900. 공제액 = 대상 × 공제율. 추가 공제 = 계획 공제 − 현재 공제이며 결정세액을 넘지 않음",
                 _ref_ev("pension_saving_limit", "pension_total_limit", "pension_credit_rate_low",
                         "pension_credit_rate_high", "pension_salary_threshold")
                 + [{"type": "profile", "ref": "tax_items:결정세액"}, {"type": "profile", "ref": "income_items:총급여"}],
                 confirm, "확인 필요" if rate is None else "계산값", 1, goal="retire")


def card_retire_tax(c: _Ctx, rt: dict) -> dict:
    acc = {x["key"]: x for x in rt["accounts"]}
    q, gen = acc["qualified"], acc["general"]
    action = (f"노후 필요자금 {fm(rt['need'])} 중 적격 계좌(연금저축·IRP)는 {fm(rt['target_qualified'])}까지만 채우고 "
              f"나머지 {fm(rt['target_nonqualified'])}은 비과세 저축성 보험·ISA·일반 계좌로 나누는 안을 검토합니다. "
              f"적격 계좌 연 수령 전망 {fm(q['annual_withdrawal'])}은 저율 분리과세 한도 연 1,500만원 대비 "
              f"{q['verdict']}입니다.")
    rows = [{"name": x["name"], "balance": x["balance_at_retire"], "annual": x["annual_withdrawal"],
             "verdict": x["verdict"]} for x in rt["accounts"]]
    confirm = ["세법 원문(사적연금 분리과세 한도, 금융소득 종합과세 기준) 확인", "저축성 보험 약관(비과세 요건) 확인",
               "연금 수령 방식(기간·연 수령 한도) 확인", "국민연금 실제 예상 수령액으로 다시 계산",
               "ISA 만기 자금을 연금계좌로 옮기면 적격 한도에 영향: 확인", "검토 제안이며 세무 자문이 아님"]
    return _card("L5", "장기", "노후 수령 세금 구간 관리", action,
                 {"연 필요 인출액(만원)": rt["annual_need"], "수령 기간(년)": rt["period_years"],
                  "적격 목표 잔액 상한(만원)": rt["qualified_cap"], "적격 목표(만원)": rt["target_qualified"],
                  "비적격 목표(만원)": rt["target_nonqualified"], "비적격 연 수령(만원)": rt["nonqualified_annual"],
                  "적격 연 수령 전망(만원)": q["annual_withdrawal"], "적격 연 수령 여유(만원)": q["headroom"],
                  "일반 계좌 이자·배당 전망(만원)": gen.get("interest"), "이자·배당 여유(만원)": gen.get("headroom"),
                  "일반 계좌 잔액 상한(만원)": rt["general_balance_cap"], "rows": rows},
                 "적격 상한 = 연 1,500만원 × 수령 기간(은퇴 뒤 수익 0 가정). 적격 납입은 연 900만원까지, 상한에 닿으면 멈춤. "
                 "나머지는 비과세 보험(월 150만원) → ISA(연 2,000만원) → 일반 계좌. 일반 계좌 잔액 상한 = 2,000 ÷ 이자·배당 수익률",
                 _ref_ev("private_pension_separate_limit", "financial_income_limit", "pension_contribution_limit",
                         "savings_insurance_monthly_limit", "general_account_yield", "isa_annual_limit"),
                 confirm, "확인 필요", 5, goal="retire")


def card_insurance(c: _Ctx) -> dict | None:
    contracts = c.profile.get("insurance_contracts", [])
    if not contracts:
        return None
    income = c.detail.get("income_monthly")
    rows, protect, unknown = [], 0.0, 0
    for k in contracts:
        prem = _man(k.get("monthly_premium_won"))
        saving = "저축" in str(k.get("name") or "")
        if prem is None:
            unknown += 1
        elif not saving:
            protect += prem
        rows.append({"name": k.get("name"), "premium": prem, "coverage": k.get("coverage_summary"),
                     "pay_term": k.get("pay_term"), "coverage_end": k.get("coverage_end"),
                     "type": "저축성" if saving else "보장성"})
    ratio = None if not income else protect / income
    confirm = ["보장 내용·특약은 증권으로 확인(이 카드는 판정하지 않음)"]
    if unknown:
        confirm.append(f"보험료 미확인 계약 {unknown}건")
    single = fact(c.profile, "household_size") == 1 or fact(c.profile, "marital_status") == "미혼"
    death = [r["name"] for r in rows if re.search(r"종신|사망", f"{r['name']} {r['coverage'] or ''}")]
    if single and death:
        confirm.append("사망보장 필요성 점검")
    return _card("L4", "장기", "보험 점검",
                 f"보장성 보험료 월 {fm(protect)}은 월 소득의 {(ratio or 0) * 100:.1f}%입니다. 계약 {len(rows)}건의 보장 범위와 기간을 점검합니다."
                 if ratio is not None else f"보험 계약 {len(rows)}건의 보장 범위와 기간을 점검합니다.",
                 {"계약 수": len(rows), "보장성 보험료(만원/월)": _r(protect), "월 소득(만원)": income,
                  "보험료 ÷ 월 소득(%)": None if ratio is None else _r(ratio * 100, 1), "rows": rows},
                 "보장성 보험료 합 ÷ 세후 월 소득. 이름에 '저축'이 들어간 계약은 저축성으로 보고 뺌",
                 [{"type": "profile", "ref": "insurance_contracts"}], confirm,
                 "확인 필요" if unknown else "계산값", 4)


# ── 요약·판단 사항 ────────────────────────────────────────────
def _summary(c: _Ctx, goals: list[dict], liquid: float) -> dict:
    s2 = c.step2
    locked = sum(_n(s2.get(k)) for k in "pqrs")
    debts = c.profile.get("liabilities", [])
    debt = sum(_man(l.get("balance_won")) or 0 for l in debts)
    other = sum(_man(a.get("balance_won")) or 0 for a in c.profile.get("assets", [])
                if classify_asset(a) is None)
    for a in c.profile.get("assets", []):
        if classify_asset(a) is None and a.get("balance_won") is None:
            c.warnings.append(f"비금융 자산 '{a.get('kind')}' 금액 미확인 → 순자산에서 빠짐")
            c.ask(f"{a.get('kind')} 시세·금액", "순자산 계산에 필요")
    for l in debts:
        if l.get("balance_won") is None:
            c.warnings.append(f"부채 '{l.get('kind')}' 잔액 미확인 → 0으로 계산")
    refl = (s2.get("_meta") or {}).get("policy_reflection") or {}
    before = (refl.get("before") or {}).get("a_monthly")
    after = c.detail.get("a_monthly")
    by = {sc: sum(max(_n(g["required_monthly" if sc == "base" else f"required_monthly_{sc}"]),
                      _n(g.get("recurring_monthly"))) for g in goals if not g.get("ends_at_transition"))
          for sc in SCENARIOS}
    need = by["base"]
    ph2 = next((p for p in c.phases if "양육 전환" in p["events"]), None)
    by2 = None
    if ph2 is not None:
        by2 = {sc: sum(max(_n((g.get("required_monthly_phase2") or {}).get(sc)), _n(g.get("recurring_monthly")))
                       for g in goals if g.get("required_monthly_phase2")) for sc in SCENARIOS}
    feas = (s2.get("_allocation") or {}).get("feasibility") or {}
    return {
        "M": s2.get("M"), "locked": _r(locked), "debt": _r(debt), "other_assets": _r(other),
        "net_worth": _r(_n(s2.get("M")) + locked + other - debt),
        "net_worth_rule": "금융자산(M) + 보존자산(p+q+r+s) + 비금융 자산(임차보증금·해약환급금 등, 아는 값) − 부채",
        "liquid_now": _r(liquid),
        "surplus_before": before, "surplus_after": after,
        "monthly_need": _r(need), "monthly_shortfall": _r(max(need - _n(after), 0.0)),
        "monthly_need_by_rate": {sc: _r(v) for sc, v in by.items()},
        "phase2": None if ph2 is None else {
            "start": ph2["start"], "surplus": ph2["surplus"], "child_cost": ph2["child_cost"],
            "monthly_need_by_rate": {sc: _r(v) for sc, v in by2.items()},
            "monthly_shortfall_by_rate": {sc: _r(max(v - _n(ph2["surplus"]), 0.0)) for sc, v in by2.items()},
            "rule": "전환 시점부터 남은 목표의 필요 월 적립(전환 시점 예상 잔액에서 출발) 합과 전환 뒤 월 잉여 비교"},
        "monthly_shortfall_by_rate": {sc: _r(max(v - _n(after), 0.0)) for sc, v in by.items()},
        "monthly_need_rule": "목표별 필요 월 적립(기준 수익률) 합. 하한·상한은 낮은·높은 수익률 가정일 때. 매년 반복 목표는 1회 금액 ÷ 12 이상. 반영 후 월 잉여와 비교",
        "allocation_feasibility": feas,
        "income_monthly": c.detail.get("income_monthly"),
        "fixed_monthly": c.detail.get("fixed_after_monthly"), "variable_monthly": c.detail.get("variable_after_monthly"),
        "distributable": c.alloc["distributable"], "emergency": c.alloc["emergency"]["amount"],
    }


def _decisions(c: _Ctx, goals: list[dict], summary: dict):
    if _n(summary["monthly_shortfall"]) > 0:
        c.decisions.append({"topic": "월 부족액 해소 방식",
                            "detail": f"목표별 필요 월 적립 합 {fm(summary['monthly_need'])} 대비 반영 후 월 잉여 "
                                      f"{fm(summary['surplus_after'])}으로 월 {fm(summary['monthly_shortfall'])} 부족",
                            "options": ["목표 시점 연장", "목표액 조정", "저축 증액(지출 추가 절감)", "시점순 집중으로 순서 정하기"]})
    customer = c.alloc.get("goal_basis") == "customer"
    for g in goals:
        sh = (g.get("shortfall_at_due") or {}).get("base")
        if not g.get("ends_at_transition") and not g.get("achieved_month") and sh and sh > 0.5:
            late = ""
            if g["goal"] == "housing":
                late = (f". 남는 잉여를 주거로 돌리면 지연 달성 예상 {g['late_funded_month']}" if g.get("late_funded_month")
                        else ". 남는 잉여를 주거로 돌려도 노후 시점까지 미달")
            c.decisions.append({"topic": f"{g['name']} 목표 시점 부족액 {fm(sh)}",
                                "detail": f"단계별 재계산으로 적립해도 {g['target']}에 {fm(sh)}이 모자람"
                                          f"(충족률 {_n(g['coverage']) * 100:.1f}%){late}",
                                "options": ["시점 연장", "금액 조정", "대출·다른 재원"]})
    child = next((g for g in goals if g.get("transition")), None)
    if child is not None:
        tr = child["transition"]
        c.decisions.append({"topic": "양육 전환 시점",
                            "detail": f"{tr['date']}({tr['source']})부터 양육을 목표에서 빼고 월 양육비 "
                                      f"{fm(tr['child_cost']['monthly'])}을 변동지출로 처리, 남은 목표 비율을 다시 계산",
                            "options": ["이 시점 유지", "고객 출산 계획으로 시점 수정"]})
        c.decisions.append({"topic": "양육 준비금 처리",
                            "detail": f"전환 시점 양육 준비금 {fm(tr['reserve']['base'])}(월 양육비 {tr['reserve_months']:g}개월분)",
                            "options": ["준비금으로 양육비 충당", "다른 목표로 재배정"]})
        if tr["source"] != "고객 목표":
            c.ask("출산 계획 시점", "양육 전환 시점을 통계상 출산 예상 시점으로 잡음")
    if customer and c.alloc.get("unstated_goals"):
        un = c.alloc["unstated_goals"]
        c.decisions.append({"topic": f"기본 목표(고객 미기재: {', '.join(u['name'] for u in un)}) 금액·시점 확인",
                            "detail": "; ".join(
                                (f"{u['name']}: 통계상 출산 예상 시점({c.transition['date']})까지 준비금을 모은 뒤 양육비로 전환"
                                 if u["goal"] == "child" and c.transition else
                                 f"{u['name']} 통계값 필요자금 {fm(u['need'])}, {u['years']}년 뒤") for u in un)
                                      + ". 기본 목표로 분배에 넣음",
                            "options": ["통계값 유지", "고객과 금액·시점 확정", "제외 후 재분배"]})
    for g in goals:
        if customer and g.get("need_source") == "통계 대체":
            c.decisions.append({"topic": f"{g['name']} 목표 금액 미기재",
                                "detail": f"고객 목표에 금액이 없어 통계값 {fm(g['need'])}으로 계산함",
                                "options": ["통계값 사용", "고객과 금액 확정"]})
    for g in goals:
        hs = g.get("horizon_source") or {}
        if not customer and hs.get("source") != "고객 목표":
            c.decisions.append({"topic": f"{g['name']} 목표 포함 여부",
                                "detail": f"고객이 적지 않은 목표(통계 기본값으로 시점·금액 설정). 필요자금 {fm(g['need'])}, "
                                          f"분배 비율 {g['ratio']}%", "options": ["포함", "제외 후 재분배", "시점·금액 조정"]})
    debt = summary["debt"]
    if debt:
        c.decisions.append({"topic": "부채 처리", "detail": f"부채 {fm(debt)}이 분배 대상(D) {fm(c.alloc['distributable'])}에서 "
                                                         "빠지지 않음", "options": ["상환 우선(분배 대상에서 차감)", "만기 유지", "일부 상환"]})
    if any(_n(g["new_monthly"]) < 0 for g in goals):
        c.decisions.append({"topic": "기존 납입 조정", "detail": "계획 월 적립이 기존 청약·연금 납입보다 적은 목표가 있음",
                            "options": ["기존 납입 유지(다른 목표 축소)", "기존 납입 감액"]})
    names = {"주거", "주택", "내집", "내 집", "결혼", "웨딩", "혼인", "양육", "자녀", "육아", "출산", "은퇴", "노후"}
    for g in ([] if customer else c.profile.get("goals", [])):
        n = str(g.get("name") or "")
        if any(w in n for w in names) or EMERGENCY_RE.search(n) or re.search(r"대출|부채|상환", n):
            continue
        c.decisions.append({"topic": f"분배에 없는 고객 목표: {n}",
                            "detail": f"금액 {fm(_man(g.get('amount_won')))}, 기한 {g.get('due') or '미확인'}. 4대 목표 분배에 들어가지 않음",
                            "options": ["별도 적립", "기존 목표 금액에서 충당", "보류"]})


def _assumptions(a: dict) -> list[dict]:
    out = []
    for k, e in STRATEGY_REFERENCE.items():
        out.append({"key": k, "value": a.get(k, e["value"]), "unit": e["unit"], "kind": e["kind"], "check": e["check"],
                    "source": e["source"], "note": e["note"]})
    return out


def build_strategy(reflected_step2: dict, profile: dict, policy_result: dict | None, cut: dict | None, *,
                   as_of: str | None = None, assumptions: dict | None = None, allocation: dict | None = None,
                   goal_basis: str = "statistical") -> dict:
    """반영본 step2 + 프로필 + 정책 결과 + spending_cut 결과 → 전략 dict. 입력은 바꾸지 않는다."""
    as_of = as_of or profile.get("as_of_date") or date.today().isoformat()
    a = assumptions_of(assumptions)
    alloc = allocation if allocation is not None else allocate(reflected_step2, profile, as_of=as_of,
                                                                 goal_basis=goal_basis)
    c = _Ctx(reflected_step2, profile, policy_result, cut, as_of, a, alloc)
    for r in alloc.get("data_requests") or []:
        c.ask(r["item"], r["reason"])
    summ = reflected_step2.get("_allocation")
    if summ:
        for g in summ.get("goals", []):
            mine = next((x for x in alloc["goals"] if x["goal"] == g["goal"]), None)
            if mine and (mine.get("amount") != g.get("amount") or mine.get("ratio") != g.get("ratio")):
                c.warnings.append(f"{g['name']}: 반영본의 분배 요약과 다시 계산한 분배 값이 다름 → 다시 계산한 값 사용")
    if c.detail.get("status") == "INCOMPLETE" or (reflected_step2.get("_meta") or {}).get("status") == "INCOMPLETE":
        c.warnings.append("자산·현금흐름 입력에 미확인 값이 있어 0으로 계산한 부분이 있음")
        for u in (reflected_step2.get("_meta") or {}).get("unknown", []):
            c.ask(u, "자산·현금흐름 계산에서 0으로 둠")
    birth = fact(profile, "birth_date")
    c.age = age_on(birth, as_of) if birth else None
    prof_label = _risk_profile(c)
    goals = _project_goals(c)
    options = _options(c, goals)
    for g in goals:
        g.pop("schedule", None)

    massets = m_assets(profile)
    liquid = sum(_man(x.get("balance_won")) or 0 for x in massets if x.get("liquidity") == "즉시")
    names = {"emergency": "비상금", **{g["goal"]: g["name"] for g in goals}, "unassigned": "미배정"}
    targets = [{"key": g["goal"], "name": g["name"], "amount": g["allocated"], "months": g.get("horizon", g["months"]),
                "target": g["target"]} for g in goals]
    s2 = reflected_step2
    locked = [{"label": "주택청약", "balance": _n(s2.get("p")), "annual": s2.get("deltap"), "goal": "housing"},
              {"label": "IRP", "balance": _n(s2.get("q")), "annual": s2.get("deltaq"), "goal": "retire"},
              {"label": "연금저축", "balance": _n(s2.get("r")), "annual": s2.get("deltar"), "goal": "retire"},
              {"label": "변액보험", "balance": _n(s2.get("s")), "annual": s2.get("deltas"), "goal": "retire"}]
    excluded = {x["asset_id"] for x in alloc["m_avail"].get("earmarked_items") or []}
    mapping = asset_mapping(massets, alloc["emergency"]["amount"], targets, locked, excluded)
    if abs(mapping["total"] - _n(reflected_step2.get("M"))) > 0.05:
        c.warnings.append(f"고객 자료의 금융자산 합 {fm(mapping['total'])}과 입력 금융자산(M) {fm(reflected_step2.get('M'))}이 다름: 배정표는 고객 자료 기준")

    cards = [card_emergency(c, liquid, mapping), card_debt(c, goals), card_maturity(c, mapping, names), card_cut(c),
             card_apply(c)]
    for g in sorted(goals, key=lambda g: g["months"]):
        if g.get("category") == "debt":
            continue  # 부채 상환 목표는 S2 카드에 연결
        cards.append(card_goal(c, g, {"housing": 1, "retire": 2, "child": 3, "wedding": 5}.get(g["goal"], 6)))
        if g["goal"] == "housing":
            cards.append(card_mortgage(c, g))
    cards.append(card_isa(c, goals))
    retire_goal_rec = next((g for g in goals if g["goal"] == "retire"), None)
    rt = None
    if retire_goal_rec is not None and c.sim is not None:
        rt = retire_tax_plan(retire_goal_rec, reflected_step2, c.sim["goals"]["retire"]["contrib"],
                             c.sim["extra"]["contrib"], c.phases, c.a, stage_of, _monthly_real)
        if rt:
            c.warnings += rt["warnings"]
            cards.append(card_retire_tax(c, rt))
    cards.append(card_pension(c, retire_goal_rec))
    if rt:  # 연금계좌 세액공제(L1)·노후 목표(L2)를 수령 세금 구간(L5)과 잇기
        q = next(x for x in rt["accounts"] if x["key"] == "qualified")
        link = {"L1": f"적격 납입은 수령 세금 구간(L5) 적격 목표 잔액 상한 {fm(rt['qualified_cap'])}에 닿으면 멈춤",
                "L2": f"노후 월 적립의 계좌 배분(적격·저축성 보험·ISA·일반)과 은퇴 뒤 적격 연 수령 전망 "
                      f"{fm(q['annual_withdrawal'])}은 수령 세금 구간(L5)과 함께 검토"}
        for cd in cards:
            if cd and cd["id"] in link:
                cd["confirm"].append(link[cd["id"]])
    cards.append(card_insurance(c))
    cards = [x for x in cards if x is not None]
    guide = load_guide()
    # 목표 카드 번호의 머리글자(S·M·L)가 실제 기간과 다르면(예: 고객이 중기로 적은 결혼) 그 기간의 빈 번호를 준다
    letter = {"단기": "S", "중기": "M", "장기": "L"}
    used = {x["id"] for x in cards}
    for x in cards:
        if x["id"][0] != letter[x["term"]] or x["id"].startswith("G-"):
            n = 6
            while f"{letter[x['term']]}{n}" in used:
                n += 1
            x["id"] = f"{letter[x['term']]}{n}"
            used.add(x["id"])
    cards.sort(key=lambda x: (TERMS.index(x["term"]), x["priority"], x["id"]))
    for x in cards:  # 카드 번호가 정해진 뒤에 원칙을 붙인다(결정적)
        x["principles"] = principles_for(card_tokens(x), guide)

    summary = _summary(c, goals, liquid)
    _decisions(c, goals, summary)
    return {
        "module": "strategy", "version": "v1", "unit": "만원", "as_of": as_of,
        "age": age_on(birth, as_of) if birth else None,
        "risk_profile": prof_label,
        "rate_note": "투자성향은 계산에 쓰지 않음. 단계별 위험 비중이 성향보다 높을 수 있어 확인 사항으로 둠",
        "terms_rule": "남은 기간 ≤ 3년 단기, 3년 초과 ≤ 10년 중기, 10년 초과 장기",
        "goal_basis": alloc.get("goal_basis", "statistical"),
        "goals_input": alloc.get("goals_input"),
        "unstated_goals": alloc.get("unstated_goals", []),
        "goals": goals,
        "phases": c.phases,
        "retire_tax": rt,
        "extra_retire": None if c.sim is None or c.sim["extra"]["start_month"] is None else {
            "start": add_months(c.d, c.sim["extra"]["start_month"]), "balance": c.sim["extra"]["balance"],
            "note": "노후 적립 완료·주거 지연 달성 뒤 남는 잉여를 추가 노후 재원으로 쌓은 금액(은퇴 시점)"},
        "child_transition": None if c.transition is None else next(
            (g["transition"] for g in goals if g.get("transition")), None),
        "child_note": c.alloc.get("child_note"),
        "options": options,
        "asset_mapping": mapping,
        "cards": cards,
        "summary": summary,
        "decisions": c.decisions,
        "principles": {"asset_mapping": principles_for({"asset_mapping", "allocation"}, guide),
                       "rate_plan": principles_for({"glide", "retire_glide", "projection", "assumptions"}, guide),
                       "decisions": principles_for({"decisions"}, guide),
                       "options": principles_for({"options"}, guide)},
        "guide": {"source": guide.get("source"), "version": guide.get("version"),
                  "principles": [{k: p.get(k) for k in ("id", "term", "title", "text")} for p in guide.get("principles", [])],
                  "report_flow": guide.get("report_flow", [])},
        "warnings": list(dict.fromkeys(c.warnings)),
        "data_requests": c.data_requests,
        "assumptions": _assumptions(a),
        "allocation": {"distributable": alloc["distributable"], "emergency": alloc["emergency"],
                       "earmarked": alloc["m_avail"].get("earmarked"),
                       "earmarked_items": alloc["m_avail"].get("earmarked_items"),
                       "m_avail": alloc["m_avail"]["value"], "no_ratio_reason": alloc["no_ratio_reason"],
                       "goals": [{k: g.get(k) for k in ("goal", "name", "need", "existing", "gap", "years", "ratio",
                                                          "amount", "coverage", "monthly_needed", "active", "note",
                                                          "horizon_source", "range", "category", "label",
                                                          "term_stated", "need_source", "reference_need", "need_diff",
                                                          "due", "raw", "kind", "recurrence",
                                                          "recurring_monthly")} for g in alloc["goals"]],
                       "warnings": alloc["warnings"]},
    }
