"""plan JSON → 재무설계사용 대시보드 HTML 한 파일. 외부 CDN·폰트·스크립트 없음.

막대는 HTML(좁은 화면에서도 글자 크기 유지), 선 그래프는 서버에서 좌표를 계산한 인라인 SVG.
화면에는 코드 식별자·영문 키를 보이지 않게 여기서 한글 표시 이름으로 바꾼다(plan JSON 키는 그대로).
형태는 templates/dashboard.html.j2 한 파일에서 고친다. 템플릿 값은 autoescape.
"""
from __future__ import annotations

import math
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .reference import REFERENCE, STRATEGY_REFERENCE

TERMS = ("단기", "중기", "장기")
TERM_COLORS = {"단기": "#a4462b", "중기": "#2d5b94", "장기": "#3a7350"}
NEUTRAL = ["#55606e", "#7b8794", "#a3adb8", "#c7ced6", "#e1e5ea"]
SAFE_COLOR = "#8a939e"
SCENARIO_LABELS = {"certain": "입력상 충족 정책만", "potential": "확인 필요 정책 포함"}
STEP_LABELS = {"step2": "자산·현금흐름 입력", "반영본 step2": "정책 절감 반영"}
CERT_CLASS = {"계산값": "c-calc", "확인 필요": "c-check"}


# ── 표기 ──────────────────────────────────────────────────────
def man(x, nd: int = 1) -> str:
    """만원 숫자(단위 없이). None은 '미확인'."""
    if x is None:
        return "미확인"
    if isinstance(x, str):
        return x
    text = f"{x:,.{nd}f}"
    if nd:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def manu(x, nd: int = 1) -> str:
    return "미확인" if x is None else f"{man(x, nd)}만원"


def pct(x, nd: int = 1) -> str:
    return "계산 불가" if x is None else f"{x:,.{nd}f}%"


def ratio_pct(x, nd: int = 1) -> str:
    return "계산 불가" if x is None else f"{x * 100:,.{nd}f}%"


def num(x) -> str:
    if x is None:
        return "미확인"
    if isinstance(x, bool):
        return "예" if x else "아니오"
    if isinstance(x, float):
        return man(x, 2)
    if isinstance(x, int):
        return f"{x:,}"
    if isinstance(x, (list, tuple)):
        return ", ".join(num(v) for v in x)
    if isinstance(x, dict):
        return ", ".join(f"{k} {num(v)}" for k, v in x.items())
    return str(x)


def ymd(value) -> str:
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def won_range(pair) -> str:
    vals = sorted({x for x in (pair or []) if x is not None})
    return "~".join(manu(x / 10_000, 2) for x in vals)


def _p(x) -> str:
    return f"{x * 100:g}%"


# ── 표시 이름 ─────────────────────────────────────────────────
LABEL_FIX = {
    "현재 연 납입 q+r(만원)": "현재 연 납입, IRP+연금저축(만원)",
    "청약 잔액 p(만원)": "청약 잔액(만원)",
    "청약 연 납입 deltap(만원)": "청약 연 납입(만원)",
    "보존자산 q+r+s(만원)": "보존자산, IRP·연금저축·변액(만원)",
    "분배 대상 D(만원)": "분배 대상(만원)",
}


def display_label(label: str) -> str:
    return LABEL_FIX.get(label, label)


def strip_unit(label: str) -> str:
    label = display_label(label)
    return label.rsplit("(", 1)[0].strip() if label.endswith(")") and "(" in label else label


def label_value(label: str, v) -> str:
    """카드 숫자 표시: 라벨의 단위를 읽어 값에 붙인다."""
    if v is None:
        return "미확인"
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return num(v)
    if label.endswith("만원)") or "(만원" in label:
        unit = "만원/월" if "/월" in label else "만원"
        return f"{man(v, 2)}{unit}"
    if label.endswith("(%)"):
        return f"{v:,.1f}%"
    if label.endswith("연도"):
        return str(v)
    if label.endswith("(년)"):
        return f"{v:g}년"
    if label.endswith("배수"):
        return f"{v:g}배"
    return num(v)


def _range(v) -> str:
    lo, b, hi = v["low"], v["base"], v["high"]
    return f"연 {b * 100:g}%" if lo == hi else f"연 {lo * 100:g}~{hi * 100:g}%, 기준 {b * 100:g}%"


def _stages_text(v) -> str:
    out = []
    for i, st in enumerate(v):
        risk = f"{round(st['risk'] * 100)}:{round((1 - st['risk']) * 100)}"
        if i == 0:
            when = f"은퇴까지 {st['above_years']}년 초과"
        elif st["above_years"]:
            when = f"{v[i - 1]['above_years']}년 이하 {st['above_years']}년 초과"
        else:
            when = f"{v[i - 1]['above_years']}년 이하"
        out.append(f"{when} {risk}·{st['rate_text']}")
    return ", ".join(out)


ASSUMPTION_NAMES = {
    "short_return": ("단기 자금 수익률(3년 이하 목표·비상금·부채 상환)", _range),
    "growth_return": ("주거·양육 등 3년 초과 목표 수익률(노후 제외)", _range),
    "retire_stages": ("노후 단계별 위험:안전 비중과 수익률", _stages_text),
    "inflation": ("물가상승률", _p), "mortgage_rate": ("주택담보대출 금리", _p),
    "mortgage_years": ("주택담보대출 만기", lambda v: f"{v}년"),
    "dsr_stress_add": ("스트레스 가산 금리", lambda v: f"{v * 100:g}%p"), "dsr_limit": ("DSR 기준", _p),
    "isa_annual_limit": ("ISA 연 납입 한도", lambda v: f"{v:,}만원"),
    "isa_taxfree": ("ISA 일반형 비과세 한도", lambda v: f"{v:,}만원"),
    "isa_separate_tax": ("ISA 초과분 분리과세율", lambda v: f"{v * 100:g}%"),
    "isa_min_years": ("ISA 의무 가입기간", lambda v: f"{v}년"),
    "pension_saving_limit": ("연금저축 세액공제 한도", lambda v: f"연 {v:,}만원"),
    "pension_total_limit": ("연금저축·IRP 합산 세액공제 한도", lambda v: f"연 {v:,}만원"),
    "pension_credit_rate_low": ("연금계좌 공제율(기준 이하)", lambda v: f"{v * 100:g}%"),
    "pension_credit_rate_high": ("연금계좌 공제율(기준 초과)", lambda v: f"{v * 100:g}%"),
    "pension_salary_threshold": ("공제율 기준(총급여)", lambda v: f"{v:,}만원"),
    "pension_income_threshold": ("공제율 기준(종합소득금액)", lambda v: f"{v:,}만원"),
}
REF_NAMES = {
    "emergency_multiple": "가정: 비상금 = 월 소득 × 1.5", "house_price_sido": "통계: 시도 평균 주택가격",
    "house_price_national": "통계: 전국 평균 주택가격", "ltv": "가정: LTV 70%", "purchase_cost_rate": "가정: 부대비용 3%",
    "mortgage_cap": "제도: 수도권 주택담보대출 한도 6억원", "retire_min_living_single": "통계: 개인 최소 노후생활비",
    "nps_old_age_avg_monthly": "통계: 국민연금 노령연금 평균액", "pension_start_age": "가정: 연금 개시 65세",
    "child_cost_to_college": "통계: 자녀 양육비(출생~대학)", "cpi_annual": "통계: 소비자물가지수",
    "child_self_share": "가정: 양육비 본인 부담 50%", "wedding_cost_ex_housing": "통계: 결혼 비용(주거비 제외)",
    "wedding_self_share": "가정: 결혼 비용 본인 부담 50%",
}
PROFILE_NAMES = {
    "assets(liquidity=즉시)": "즉시 인출 자산", "assets(matures_on)": "자산 만기", "liabilities": "부채 내역",
    "cashflow_items": "현금흐름표", "income_items:총급여": "총급여(원천징수영수증)", "tax_items:결정세액": "결정세액",
    "insurance_contracts": "보험 계약",
}


def assumption_rows(assumptions: list[dict]) -> list[dict]:
    out = []
    for a in assumptions:
        name, f = ASSUMPTION_NAMES.get(a["key"], (a["source"], num))
        try:
            value = f(a["value"])
        except (TypeError, ValueError):
            value = num(a["value"])
        out.append({"name": name, "value": value, "kind": a["kind"], "check": a["check"], "source": a["source"],
                    "note": a["note"]})
    return out


def evidence_label(e: dict) -> str:
    t, ref = e.get("type"), str(e.get("ref") or "")
    if t == "policy":
        return f"정책: {e.get('name') or '공고'}"
    if t == "profile":
        if ref.startswith("goals:"):
            return "고객 자료: 재무목표"
        return f"고객 자료: {PROFILE_NAMES.get(ref, '입력 자료')}"
    if ref in STRATEGY_REFERENCE:
        name, f = ASSUMPTION_NAMES.get(ref, (STRATEGY_REFERENCE[ref]["source"], num))
        kind = "제도" if STRATEGY_REFERENCE[ref]["kind"] == "제도" else "가정"
        return f"{kind}: {name} {f(STRATEGY_REFERENCE[ref]['value'])}"
    if ref in REF_NAMES:
        return REF_NAMES[ref]
    src = (REFERENCE.get(ref) or {}).get("source") or "기준값"
    return f"기준값: {src[:40]}"


def href(url: str | None) -> str | None:
    if not url:
        return None
    return url if "://" in url else "https://" + url


COL_LABELS = {"kind": "종류", "balance": "잔액", "rate_pct": "금리(%)", "matures_on": "만기", "years_to_maturity": "만기까지(년)",
              "term": "기간", "repayment": "상환", "monthly_interest": "월 이자", "asset": "자산", "assigned": "배정",
              "policy": "정책", "item": "항목", "monthly": "월 절감", "verdict": "판정", "url": "링크",
              "certainty": "판정", "amount": "금액", "amount_text": "내용", "deadline": "마감", "limit": "한도(만원)",
              "name": "계약", "premium": "월 보험료", "coverage": "보장", "pay_term": "납입", "coverage_end": "보장 종료",
              "type": "유형"}
HIDDEN_COLS = ("policy_id", "amount")
KEY_NUMBERS = {
    "S1": ["비상금 목표(만원)", "안전 자산 배정(만원)", "즉시 인출 안전 자산 부족(만원)"],
    "S2": ["부채 합계(만원)", "월 이자 합계(만원)"],
    "S3": ["만기 자산 합(만원)", "건수"],
    "S4": ["행동 절감(만원/월)", "정책 절감 합(만원/월)"],
    "S5": ["건수", "입력상 충족"],
    "M2": ["월 원리금(만원)", "DSR(%)", "스트레스 DSR(%)"],
    "M3": ["ISA 연 납입 한도(만원)", "중기 목표 계획 월 적립 합(만원)"],
    "L1": ["현재 공제액(만원)", "남은 한도(만원)", "추가 공제(만원)"],
    "L4": ["보장성 보험료(만원/월)", "보험료 ÷ 월 소득(%)"],
}
GOAL_KEYS = ["계획 월 적립(만원)", "필요 월 적립(만원)", "충족률(%)"]


def key_numbers(card: dict) -> list[dict]:
    if "전환 시점" in card["numbers"]:
        keys = ["전환 시점", "양육 준비금 기준(만원)", "준비금, 월 양육비 개월분"]
        return [{"label": strip_unit(k), "value": label_value(k, card["numbers"].get(k))}
                for k in keys if card["numbers"].get(k) is not None]
    keys = KEY_NUMBERS.get(card["id"]) or (GOAL_KEYS if card.get("goal") not in (None, "emergency", "debt")
                                           and "충족률(%)" in card["numbers"] else [])
    if not keys:
        keys = [k for k, v in card["numbers"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)][:2]
    return [{"label": strip_unit(k), "value": label_value(k, card["numbers"].get(k))}
            for k in keys if card["numbers"].get(k) is not None]


def table_rows(rows: list[dict], goal_names: dict) -> dict:
    cols = [k for k in rows[0].keys() if k not in HIDDEN_COLS]
    body = []
    for r in rows:
        cells = []
        for k in cols:
            v = r.get(k)
            if k == "url":
                cells.append({"link": href(v)})
            elif isinstance(v, dict):
                cells.append({"text": ", ".join(f"{goal_names.get(kk, kk)} {man(vv)}" for kk, vv in v.items())})
            elif v is None:
                cells.append({"text": ""})
            else:
                cells.append({"text": num(v)})
        body.append(cells)
    return {"head": [COL_LABELS.get(k, k) for k in cols], "body": body}


def goals_table(plan: dict) -> list[dict]:
    """Fact Finding 2번 재무목표(고객 표기) → 화면 표. 고객 목표 기준이 아니면 빈 목록."""
    items = plan["strategy"].get("goals_input") or []
    rows = []
    for it in items:
        amount = it.get("amount")
        basis = it.get("amount_basis") or ""
        if amount is None:
            amount_text = "미확인"
        else:
            amount_text = f"월 {man(amount)}만원" if "월" in basis else f"{man(amount)}만원"
        use = it.get("plan_use") or ""
        if it.get("category") == "housing" and "통계 대체" in use:
            use += "(고객 희망 주택 가격·자기자금 확인 전 통계값)"
        rows.append({"term": it.get("term_stated") or "", "text": it.get("raw") or it.get("name") or "",
                     "due": it.get("due") or ("" if it.get("kind") == "excluded" else "미확인"), "amount": amount_text
                     if it.get("kind") != "excluded" else "", "use": use, "reference": it.get("reference_need")})
    if items:
        for u in plan["strategy"].get("unstated_goals") or []:
            tr = plan["strategy"].get("child_transition") if u["goal"] == "child" else None
            label = f"고객 유보 '{u['hedge']}': 확인" if u.get("hedge") else "고객 미기재, 통계값"
            rows.append({"term": "", "text": f"기본 목표({label}): {u['name']}",
                         "due": f"전환 {tr['date']}(통계상 출산 예상)" if tr else f"{u['years']}년 뒤(통계 연령)",
                         "amount": "미기재" if tr else f"{man(u['need'])}만원",
                         "use": "기본 목표로 분배 후 전환 시점부터 양육비로 처리" if tr else "기본 목표로 분배(통계 기본값)",
                         "reference": u["need"]})
    return rows


def shade(hex_color: str, k: float) -> str:
    """k(0~1)만큼 흰색 쪽으로."""
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(c + (255 - c) * k):02x}" for c in rgb)


# ── 차트 ──────────────────────────────────────────────────────
def stacked_bar(items: list[dict]) -> list[dict]:
    """items [{'label','value','color'}] → 비율(%)이 붙은 목록(0 이하·None 제외)."""
    vals = [max(i["value"] or 0, 0) for i in items]
    total = sum(vals)
    return [{**i, "share": v / total, "pct": round(v / total * 100, 3)} for i, v in zip(items, vals) if v > 0 and total > 0]


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for step in (1, 2, 2.5, 5, 10):
        if v <= step * mag:
            return step * mag
    return 10 * mag


def line_chart(goal: dict, w: int = 400, h: int = 230) -> dict:
    """목표 전망: 기준 수익률 선 + 하한~상한 띠(면) + 필요자금 선(값 라벨)."""
    pad_l, pad_r, pad_t, pad_b = 52, 14, 22, 30
    series = goal.get("series") or []
    need = 0 if goal.get("ends_at_transition") else (goal.get("need") or 0)  # 양육 준비금 그래프에는 필요자금 선 없음
    top = nice_max(max([need] + [p["high"] or 0 for p in series] + [p["base"] or 0 for p in series]) * 1.08)
    ts = [p.get("t", i) for i, p in enumerate(series)]  # 마지막 점은 실제 경과 연수(1년이 안 되는 끝 구간)
    n = max(len(series) - 1, 1)
    span = max(ts[-1] if ts else 1, 1e-9)
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(i):
        return round(pad_l + (ts[i] if i < len(ts) else i) / span * iw, 1)

    def Y(v):
        return round(pad_t + ih - (v or 0) / top * ih, 1)
    base = " ".join(f"{X(i)},{Y(p['base'])}" for i, p in enumerate(series))
    band = " ".join([f"{X(i)},{Y(p['high'])}" for i, p in enumerate(series)]
                    + [f"{X(i)},{Y(p['low'])}" for i, p in reversed(list(enumerate(series)))])
    yticks = [{"y": Y(top * k / 4), "label": man(top * k / 4, 0)} for k in range(5)]
    step = max(1, math.ceil(n / 5))
    idx = [i for i in range(0, n + 1, step)]
    if idx[-1] != n:
        if n - idx[-1] < step * 0.6:  # 마지막 눈금과 끝 연도가 가까우면 끝 연도만
            idx[-1] = n
        else:
            idx.append(n)
    xticks = [{"x": X(i), "label": str(series[i]["label"])} for i in idx if i < len(series)]
    need_y = Y(need) if need else None
    return {"w": w, "h": h, "pad_l": pad_l, "x_end": w - pad_r, "y_base": pad_t + ih, "base": base, "band": band,
            "need_y": need_y, "need_label": f"필요자금 {man(need)}", "need_label_y": None if need_y is None else round(need_y - 5, 1),
            "yticks": yticks, "xticks": xticks}


def rate_text(plan: list[dict]) -> str:
    """목표 운용 구간을 짧게: '연 6.5%(5~8%) → 2028-11부터 연 2.5%(2~3%)'."""
    out = []
    for i, p in enumerate(plan):
        n = p["nominal"]
        rng = f"({n['low']:g}~{n['high']:g}%)" if n["low"] != n["high"] else ""
        head = "" if i == 0 else f"{p['start']}부터 "
        out.append(f"{head}연 {n['base']:g}%{rng}")
    return " → ".join(out)


def retire_band(goal: dict, w: int = 1000) -> list[dict]:
    """노후 단계 띠: 구간마다 폭(%)·연도·나이·비중·수익률·구간 말 잔액."""
    total = sum(p["months"] for p in goal["rate_plan"]) or 1
    colors = ["#2f6b45", "#5d9473", "#a9c9b4"]
    out = []
    for i, p in enumerate(goal["rate_plan"]):
        out.append({**p, "pct": round(p["months"] / total * 100, 3), "color": colors[min(i, 2)],
                    "years": round(p["months"] / 12, 1)})
    return out


def rate_notes(st: dict, inflation_pct: float) -> list[str]:
    """'명목 X%, 물가 2% 차감 후 실질 Y%'. 단계별로."""
    infl = inflation_pct / 100
    rows = []
    a = {x["key"]: x["value"] for x in st["assumptions"]}
    for label, v in (("단기 자금", a.get("short_return")), ("3년 초과 목표(노후 제외)", a.get("growth_return"))):
        if v:
            rows.append(f"{label}: 명목 {v['base'] * 100:g}%, 물가 {inflation_pct:g}% 차감 후 실질 "
                        f"{((1 + v['base']) / (1 + infl) - 1) * 100:.2f}%")
    for stg in a.get("retire_stages") or []:
        r = stg["rate"]["base"]
        rows.append(f"노후 {stg['name']}: 명목 {r * 100:g}%, 물가 {inflation_pct:g}% 차감 후 실질 {((1 + r) / (1 + infl) - 1) * 100:.2f}%")
    return rows


# ── 화면 데이터 ───────────────────────────────────────────────
def build_view(plan: dict) -> dict:
    st = plan["strategy"]
    s2 = plan["step2"]
    detail = s2["_meta"]["deltaM_detail"]
    refl = s2["_meta"].get("policy_reflection") or {}
    summ = st["summary"]
    goals = st["goals"]
    goal_color = {}
    for term in TERMS:
        for i, g in enumerate([g for g in goals if g["term"] == term]):
            goal_color[g["goal"]] = shade(TERM_COLORS[term], 0.3 * i)
    goal_names = {"emergency": "비상금", "unassigned": "미배정", "housing": "주거", "wedding": "결혼", "child": "양육",
                  "retire": "노후", "debt": "부채 상환", **{g["goal"]: g["name"] for g in goals}}

    kinds: dict[str, float] = {}
    for r in st["asset_mapping"]["rows"]:
        kinds[r["kind"] or "기타"] = kinds.get(r["kind"] or "기타", 0) + (r["balance"] or 0)
    asset_items = [{"label": k, "value": v, "color": NEUTRAL[i % 3]}
                   for i, (k, v) in enumerate(sorted(kinds.items(), key=lambda kv: -kv[1]))]
    for key, name in (("p", "주택청약"), ("q", "IRP"), ("r", "연금저축"), ("s", "변액보험")):
        if s2.get(key):
            asset_items.append({"label": name, "value": s2[key],
                                "color": shade(TERM_COLORS["중기" if key == "p" else "장기"], 0.35)})
    if summ.get("other_assets"):
        asset_items.append({"label": "비금융(보증금 등)", "value": summ["other_assets"], "color": NEUTRAL[4]})

    before = refl.get("before") or {}
    income = detail.get("income_monthly")
    rows = [("월 소득(세후)", income, income),
            ("고정지출(보험 포함)", before.get("fixed_after_monthly", detail.get("fixed_after_monthly")),
             detail.get("fixed_after_monthly")),
            ("변동지출(행동 절감 후)", before.get("variable_after_monthly", detail.get("variable_after_monthly")),
             detail.get("variable_after_monthly")),
            ("월 잉여", before.get("a_monthly", detail.get("a_monthly")), detail.get("a_monthly"))]
    ph2 = next((x for x in st.get("phases") or [] if x["phase"] == 2), None)
    cost = (ph2 or {}).get("child_cost") or 0
    p2 = {"월 소득(세후)": income, "고정지출(보험 포함)": detail.get("fixed_after_monthly"),
          "변동지출(행동 절감 후)": None if detail.get("variable_after_monthly") is None
          else round(detail["variable_after_monthly"] + cost, 2),
          "월 잉여": None if ph2 is None else ph2["surplus"]}
    top = max([abs(x or 0) for _, b, a in rows for x in (b, a)] + [abs(x or 0) for x in p2.values()] + [1])
    cashflow = [{"label": l, "before": b, "after": a, "wb": round(max(b or 0, 0) / top * 100, 2),
                 "wa": round(max(a or 0, 0) / top * 100, 2),
                 "p2": p2[l] if ph2 else None, "wp2": round(max(p2[l] or 0, 0) / top * 100, 2) if ph2 else 0,
                 "delta": None if a is None or b is None or abs(a - b) < 0.005 else round(a - b, 2)} for l, b, a in rows]

    alloc_goals = [g for g in st["allocation"]["goals"] if g["active"] and g.get("ratio")]
    ratio_bar = stacked_bar([{"label": g["name"], "value": g["ratio"], "color": goal_color.get(g["goal"], NEUTRAL[1])}
                             for g in alloc_goals])

    guide = {p["id"]: p for p in (st.get("guide") or {}).get("principles", [])}
    cards_by_id = {c["id"]: c for c in st["cards"]}
    terms = []
    for term in TERMS:
        adv = plan["advice"]["terms"].get(term) or {}
        order = adv.get("order") or [c["id"] for c in st["cards"] if c["term"] == term]
        cards = []
        for i in order:
            c = cards_by_id.get(i)
            if not c:
                continue
            nums = [{"label": strip_unit(k), "value": label_value(k, v)} for k, v in c["numbers"].items()
                    if k not in ("rows", "glide", "rate_plans") and v is not None]
            cards.append({**c, "key_numbers": key_numbers(c), "all_numbers": nums,
                          "table": table_rows(c["numbers"]["rows"], goal_names) if c["numbers"].get("rows") else None,
                          "glide_text": "; ".join(f"{name}: {txt}" for name, txt in
                                                  (c["numbers"].get("rate_plans") or {}).items()),
                          "evidence_view": [{"label": evidence_label(e), "href": href(e.get("url"))} for e in c["evidence"]],
                          "cert_class": CERT_CLASS.get(c["certainty"], "")})
        terms.append({"term": term, "color": TERM_COLORS[term], "advice": adv, "cards": cards,
                      "cls": {"단기": "short", "중기": "mid", "장기": "long"}[term],
                      "principles": [guide[pid] for pid in (adv.get("principles") or []) if pid in guide]})

    proj = {g["goal"]: g for g in goals}
    alloc_by = {g["goal"]: g for g in st["allocation"]["goals"]}
    alloc_rows = []
    for g in goals:
        now = g["rate_plan"][0] if g.get("rate_plan") else None
        alloc_rows.append({**g, "color": goal_color.get(g["goal"], NEUTRAL[1]), "rate_text": rate_text(g["rate_plan"]),
                           "risk_now": now["risk_ratio"] if now and g.get("rate_kind") == "retire" else None,
                           "bar": round(min(g["coverage"] or 0, 1) * 100, 1),
                           "bar_low": round(min(g["coverage_low"] or 0, 1) * 100, 1),
                           "bar_high": round(min(g["coverage_high"] or 0, 1) * 100, 1),
                           "need_source": g.get("need_source") or "통계값",
                           "gap": (alloc_by.get(g["goal"]) or {}).get("gap")})
    for g in st["allocation"]["goals"]:
        if not g["active"]:
            alloc_rows.append({"name": g["name"], "kind": g.get("kind", "base"), "inactive": True, "note": g.get("note"),
                               "need": g.get("need"), "need_source": g.get("need_source")})
    retire = next((g for g in goals if g.get("rate_kind") == "retire"), None)
    phases = st.get("phases") or []
    transition = None
    if phases:
        horizon_end = max([g["months"] for g in goals] + [phases[-1]["start_month"] + 12])
        bands = []
        for ph in phases:
            end = ph["end_month"] if ph["end_month"] is not None else horizon_end
            bands.append({**ph, "width": max(end - ph["start_month"], 1),
                          "label": ", ".join(ph["events"]) if ph["events"] != ["시작"] else "시작"})
        tot = sum(b["width"] for b in bands)
        floor = min(16.0, 100 / len(bands))
        raw = [max(b["width"] / tot * 100, floor) for b in bands]  # 짧은 단계도 글자가 보이게 최소 폭
        for b, w in zip(bands, raw):
            b["pct"] = round(w / sum(raw) * 100, 3)
        rows_tr = []
        for g in goals:
            cells, shown = [], False
            for ph in phases:
                k = g["goal"]
                if k in ph["ratios"]:
                    sub = f"{man(ph['monthly'][k])}" + (" 목표 시점 이후" if ph.get("mode") == "housing" else "")
                    cells.append({"text": f"{ph['ratios'][k]:g}%", "sub": sub})
                elif g.get("late_funded_month") and ph["start"] == g["late_funded_month"]:
                    cells.append({"text": f"지연 달성 {g['late_funded_month']}", "sub": "필요자금 도달"})
                elif not shown and ph["start_month"] > 0 and (ph["start_month"] >= g["months"] or g.get("achieved_month")
                                                               and ph["start"] >= g["achieved_month"]):
                    shown = True
                    if g.get("ends_at_transition"):
                        cells.append({"text": "양육 전환", "sub": f"준비금 {man(g['transition']['reserve']['base'])}"})
                    elif g.get("achieved_month"):
                        cells.append({"text": f"적립 완료 {g['achieved_month']}", "sub": "목표 시점에 필요자금 도달 예상"})
                    else:
                        cells.append({"text": "시점 도래",
                                      "sub": f"부족 {man((g.get('shortfall_at_due') or {}).get('base'))}"})
                else:
                    cells.append({"text": "", "sub": ""})
            rows_tr.append({"name": g["name"], "cells": cells})
        if any("retire_extra" in ph["ratios"] for ph in phases):
            rows_tr.append({"name": "추가 노후 재원", "cells": [
                {"text": f"{ph['ratios']['retire_extra']:g}%", "sub": man(ph["monthly"]["retire_extra"])}
                if "retire_extra" in ph["ratios"] else {"text": "", "sub": ""} for ph in phases]})
        transition = {"bands": bands, "rows": rows_tr, "info": st.get("child_transition"),
                      "many": len(phases) > 6}
    m = st["asset_mapping"]
    asset_cols = m["columns"]
    asset_rows = [{**r, "cells": [{"v": r["assigned"].get(c["key"]), "conv": r["converted"].get(c["key"])}
                                  for c in asset_cols]} for r in sorted(m["rows"], key=lambda r: r["class"] != "안전")]
    locked_rows = [{**r, "cells": [{"v": None, "conv": None} for _ in asset_cols],
                    "goal_name": goal_names.get(r["locked_goal"], r["locked_goal"])} for r in m.get("locked_rows", [])]
    extras = [g for g in goals if g.get("kind") == "extra"]
    bases = [g for g in goals if g.get("kind", "base") == "base"]
    charts = [{**g, "chart": line_chart(g), "color": TERM_COLORS[g["term"]]}
              for g in sorted(goals, key=lambda g: g["months"])]
    glance = [{"name": g["name"], "term": g["term"], "color": TERM_COLORS[g["term"]], "target": g["target"],
               "coverage": g["coverage"], "bar": round(min(g["coverage"] or 0, 1) * 100, 1),
               "planned": g["planned_monthly"], "required": g["required_monthly"],
               "kind": g.get("kind", "base"), "label": g.get("label") if g.get("kind") == "extra" else None,
               "recurring": g.get("recurring_monthly") if g.get("recurrence") == "annual" else None}
              for g in sorted(goals, key=lambda g: g["months"])]
    questions = list(dict.fromkeys((plan["policy"].get("questions") or []) + (plan["spending"].get("questions") or [])))
    meta = s2["_meta"]
    if plan.get("step2_source", "").startswith("팀원"):
        source = "팀원 산출본"
    else:
        source = "프로필에서 유도" + ("(미확인 값 있음)" if meta.get("status") == "INCOMPLETE" else "")
    steps = [{**s, "label": STEP_LABELS.get(s["step"], s["step"]),
              "memo": "건너뜀" if s["status"] == "skipped" else (f"경고 {len(s.get('warnings') or [])}건" if s.get("warnings") else ""),
              "status_label": {"ok": "완료", "skipped": "건너뜀", "failed": "실패"}.get(s["status"], s["status"])}
             for s in plan["pipeline"]["steps"]]
    other = plan["policy"].get("other_scenario") or {}
    return {
        "terms": terms, "term_colors": TERM_COLORS, "safe_color": SAFE_COLOR, "goal_color": goal_color,
        "goal_names": goal_names, "assets": stacked_bar(asset_items), "cashflow": cashflow, "ratio_bar": ratio_bar,
        "proj": proj, "charts": charts, "glance": glance, "questions": questions,
        "behavior_cut": detail.get("cut_monthly"), "policy_cut": detail.get("policy_cut_monthly") or {},
        "scenario_label": SCENARIO_LABELS.get(plan["scenario"], plan["scenario"]),
        "other_scenario_label": SCENARIO_LABELS.get(other.get("scenario"), ""),
        "input_source": source, "steps": steps, "assumptions": assumption_rows(st["assumptions"]),
        "shortfall_simple": (summ.get("allocation_feasibility") or {}).get("shortfall_monthly"),
        "provider_label": {"none": "사용 안 함(템플릿 문장)", "anthropic": "Anthropic 형식 게이트웨이",
                           "server": "OpenAI 호환 서버", "local": "로컬 모델"}.get(plan["advice"]["provider"],
                                                                                    plan["advice"]["provider"]),
        "llm_totals": plan["advice"].get("totals") or {},
        "goals_table": goals_table(plan),
        "goal_basis_label": "고객이 적은 재무목표 기준" if st.get("goal_basis") == "customer" else "4대 생애목표 통계 기준",
        "unstated": st.get("unstated_goals") or [],
        "glance_base": [g for g in glance if g["kind"] == "base"],
        "glance_extra": [g for g in glance if g["kind"] == "extra"],
        "sentence_sources": plan["advice"].get("sentence_sources"),
        "alloc_rows": alloc_rows, "retire": retire, "transition": transition,
        "phase2": summ.get("phase2"), "child_note": st.get("child_note"), "retire_band": retire_band(retire) if retire else [],
        "asset_cols": asset_cols, "asset_rows": asset_rows, "locked_rows": locked_rows,
        "extras": extras, "bases": bases, "guide": guide,
        "rate_notes": rate_notes(st, plan.get("inflation_pct", 2.0)), "inflation_pct": plan.get("inflation_pct", 2.0),
        "short_by": summ.get("monthly_shortfall_by_rate") or {}, "need_by": summ.get("monthly_need_by_rate") or {},
        "retire_tax": st.get("retire_tax"), "extra_retire": st.get("extra_retire"),
        "stage_note": next((x["note"] for x in st["assumptions"] if x["key"] == "retire_stages"), None),
        "fallback": plan["advice"].get("fallback") or {},
    }


_ENV = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")),
                   autoescape=select_autoescape(["j2", "html"]), trim_blocks=True, lstrip_blocks=True)
_ENV.filters.update(ymd=ymd, won_range=won_range, man=man, manu=manu, pct=pct, ratio_pct=ratio_pct, num=num, strip_unit=strip_unit, href=href)
_ENV.globals.update(label_value=label_value, shade=shade)
_ENV.filters.update(won_range=won_range)


def render(plan: dict) -> str:
    return _ENV.get_template("dashboard.html.j2").render(p=plan, v=build_view(plan), s=plan["strategy"])
