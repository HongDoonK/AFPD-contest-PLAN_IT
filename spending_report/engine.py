"""FF 7번 현금흐름표의 적용 전·후 계산. 보고서의 모든 금액과 비율은 여기서 정해진다.

적용 후 = 적용 전 − 정책 혜택 − 소비 조정. 줄어든 돈은 '절감 재원' 행으로 옮겨 표의 합계를 유지한다.
정책은 '입력상 충족'인 것만, 조정은 rules.py의 후보만 반영하며 둘 다 확정이 아닌 시뮬레이션이다.
금액이 비어 있는 행은 0원으로 치지 않고 미확인으로 남기며, 그때는 비율 지표를 계산하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .inputs import ReportInput
from .rules import (BLANK_INCOME_QUESTION, BLANK_KEEP, BLANK_QUESTION, CARD_QUESTION, ESTIMATED_KEEP, FF_BLOCK, FF_BLOCKS, INCOME_LABELS, LIQUID_KINDS,
                    LUMP_REVIEW_PCT, MIXED_KEEP, MIXED_QUESTION, RESERVE_LABEL, SECTION_KEEP, PolicyNote, SpendRule, match_rule,
                    policy_note)

CHOICE_PENDING = "미선택"


def _floor_thousand(won: float) -> int:
    return int(won) // 1000 * 1000


def split_parts(item: str, monthly_won: int, detail: str | None) -> list[tuple[str, int]]:
    """'대중교통 8 + 택시 2(만원)' 형식의 세부 내역을 나눈다. 합계가 안 맞으면 행 전체를 한 덩어리로 본다."""
    found = re.match(r"^(.*?)\(만원\)", detail or "")
    if found and "+" in found.group(1):
        parts = []
        for chunk in found.group(1).split("+"):
            piece = re.match(r"\s*(.+?)\s+([\d.]+)\s*$", chunk)
            if piece is None:
                return [(item, monthly_won)]
            parts.append((piece.group(1), round(float(piece.group(2)) * 10_000)))
        if sum(won for _, won in parts) == monthly_won:
            return parts
    return [(item, monthly_won)]


@dataclass
class Row:
    source: dict
    block: str
    before: int | None
    policy: int = 0
    adjustment: int = 0
    essential: int = 0        # 소득이 끊겨도 계속 나가는 금액(R02 분모)
    essential_cut: int = 0    # 그중 적용 후 줄어드는 금액
    policy_offset: dict | None = None
    policy_annual: int = 0
    policy_valid_note: str = ""

    @property
    def rid(self) -> str:
        return f"{self.source['section']}:{self.source['item']}"

    @property
    def known(self) -> bool:
        return self.before is not None

    @property
    def after(self) -> int | None:
        return None if self.before is None else self.before - self.policy - self.adjustment


@dataclass
class Analysis:
    rows: list[Row] = field(default_factory=list)
    classification: list[dict] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    conditional: list[dict] = field(default_factory=list)
    kept: list[tuple[str, str, int | None]] = field(default_factory=list)  # (사유, 항목, 원)
    questions: list[str] = field(default_factory=list)


def _full_months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)


def _apply_policy_timing(row: Row, note: PolicyNote | None, inp: ReportInput) -> None:
    """청년 구간 혜택은 상한 나이를 넘으면 낮아진다. 12개월 효과와 안내 문구에 반영한다."""
    row.policy_annual = row.policy * 12
    birth = inp.fact("birth_date")
    if note is None or note.youth_age_max is None or not birth:
        return
    born, as_of = date.fromisoformat(birth), date.fromisoformat(inp.as_of_date)
    ends = date(born.year + note.youth_age_max + 1, born.month, min(born.day, 28))
    general = round(row.policy * note.general_pct / note.youth_pct)
    if ends <= as_of:
        row.policy, row.policy_annual = general, general * 12
        return
    youth_months = min(12, _full_months(as_of, ends))
    if youth_months < 12:
        row.policy_annual = row.policy * youth_months + general * (12 - youth_months)
        row.policy_valid_note = (f"만 {note.youth_age_max + 1}세가 되는 {ends.isoformat()}부터 일반 구간 "
                                 f"{note.general_pct:g}%가 적용되어 월 {general:,}원으로 낮아짐")


def _pick_policy(line: dict | None) -> dict | None:
    """같은 행의 정책 중 시나리오에 쓸 수 있고 월 절감액이 가장 큰 것 하나."""
    best, best_saving = None, 0
    for offset in (line or {}).get("policy_offsets", []):
        saving = (offset["effect"].get("monthly_saving_won") or [0])[0]
        if offset["certainty"] == "시나리오 사용 가능" and saving > best_saving:
            best, best_saving = offset, saving
    return best


def _recommendation(rec_id, kind, row, target, action, before, after, certainty, assumptions, when, tradeoff="") -> dict:
    change = before - after
    return {
        "id": rec_id, "kind": kind, "row_id": row.rid, "row_label": row.source["item"], "target": target,
        "action": action, "amount": {"before_won": before, "after_won": after, "monthly_change_won": -change},
        "period": "월", "when": when, "tradeoff": tradeoff, "assumptions": list(assumptions), "dependencies": [],
        "alternatives": [{"label": "유지", "monthly_change_won": 0},
                         {"label": "절반만 적용", "monthly_change_won": -_floor_thousand(change / 2)}],
        "customer_choice": CHOICE_PENDING, "certainty": certainty,
    }


def _classify(result: Analysis, row: Row, label: str, won: int | None, necessity: str, basis: str, cut: int = 0) -> dict:
    src = row.source
    entry = {"row_id": row.rid, "label": label, "monthly_won": won, "purpose": src.get("purpose") or "미확인",
             "frequency": src.get("frequency") or "미확인", "necessity": necessity, "basis": basis, "cut_won": cut, "policy_won": 0}
    result.classification.append(entry)
    return entry


def _customer_wants_kept(rule: SpendRule, src: dict) -> bool:
    """고객이 유지를 원한다고 적힌 지출인가. 같은 행의 다른 세부 항목에 번지지 않도록 '유지 희망' 문구만 본다."""
    stated = " ".join(filter(None, [src.get("detail"), src.get("assumptions")]))
    return any("유지 희망" in phrase and re.search(rule.pattern, phrase) for phrase in re.split(r"\.\s+", stated))


def _is_estimate(src: dict) -> bool:
    return src["status"] == "추정" or src.get("low_won") != src.get("high_won")


def _analyze_consumption_row(result: Analysis, row: Row, inp: ReportInput, line: dict | None) -> None:
    src = row.source
    offset = _pick_policy(line)
    if offset:
        row.policy_offset = offset
        row.policy = offset["effect"]["monthly_saving_won"][0]
        _apply_policy_timing(row, policy_note(offset["policy_name"]), inp)

    entries: list[dict] = []
    for label, won in split_parts(src["item"], row.before, src.get("detail")):
        rule = match_rule(label)
        necessity, reason = rule.necessity, rule.keep_reason
        keep_requested = _customer_wants_kept(rule, src)
        if keep_requested:
            necessity, reason = "필수", f"{rule.keep_reason or '고객이 유지를 원하는 지출'} (고객 유지 희망)"
        cut = 0
        if rule.reduce_pct and won > rule.floor_won and not keep_requested:
            cut = min(_floor_thousand(won * rule.reduce_pct), row.before - row.policy - row.adjustment)
        if cut > 0:
            row.adjustment += cut
            target = label if label != src["item"] else src["item"]
            result.recommendations.append(_recommendation(
                f"adjust:{row.rid}:{label}", "소비 조정", row, target, rule.action, won, won - cut, "고객 선택 전",
                [f"절감률 {rule.reduce_pct * 100:g}%는 임시 가정. 거래내역과 고객 합의로 확정"], rule.when, rule.tradeoff))
        elif reason:
            result.kept.append((reason, label, won))
        if rule.question and (cut > 0 or not rule.reduce_pct):
            result.questions.append(rule.question)
        entries.append(_classify(result, row, label, won, necessity, "분류 규칙", cut))
        if necessity != "선택":
            row.essential += won
            row.essential_cut += cut
    if row.policy:
        next((e for e in entries if e["necessity"] != "선택"), entries[0])["policy_won"] = row.policy
        if row.essential:
            row.essential_cut = min(row.essential, row.essential_cut + row.policy)


def _note_card_lump(result: Analysis, row: Row) -> None:
    """카드값은 여러 지출의 합이라 표를 바꾸지 않고, 명세서 확인 뒤의 조건부 대안만 남긴다."""
    src = row.source
    if "카드값" not in (src.get("detail") or ""):
        return
    result.questions.append(CARD_QUESTION)
    low, high = src.get("low_won") or row.before, src.get("high_won") or row.before
    mixed = src.get("purpose") in ("업무", "혼합")
    result.conditional.append({
        "target": f"{src['item']}(카드값)", "condition": "카드 명세서를 항목별로 나눈 뒤 줄일 수 있는 지출을 고객과 정한 경우",
        "monthly_range_won": [_floor_thousand(low * LUMP_REVIEW_PCT), _floor_thousand(high * LUMP_REVIEW_PCT)],
        "assumptions": [f"절감률 {LUMP_REVIEW_PCT * 100:g}%는 임시 가정"],
        "dependencies": ["세금 재계산(업무비 포함)"] if mixed else [],
    })


def analyze_rows(inp: ReportInput) -> Analysis:
    lines = {(l["section"], l["item"]): l for l in inp.handoff.get("expense_lines", [])}
    result = Analysis()
    blanks = []
    for src in inp.cashflow:
        row = Row(source=src, block=FF_BLOCK[src["section"]], before=src["monthly_won"])
        result.rows.append(row)
        if row.before is None:
            blanks.append(src["item"])
            result.kept.append((BLANK_KEEP, src["item"], None))
            continue
        if row.before == 0:
            continue
        section_reason = SECTION_KEEP.get(src["section"])
        if section_reason:
            result.kept.append((section_reason, src["item"], row.before))
            necessity = src.get("necessity") or ("필수" if src["section"] == "보험" else "해당없음")
            _classify(result, row, src["item"], row.before, necessity, "구역 규칙")
            # 보험과 사회보험은 소득이 끊겨도 나간다. 세금 적립과 저축은 그렇지 않다
            if src["section"] == "보험" or (src["section"] == "세금·사회보험" and "적립" not in src["item"]):
                row.essential = row.before
        elif src.get("purpose") in ("업무", "혼합"):
            result.kept.append((MIXED_KEEP, src["item"], row.before))
            result.questions.append(MIXED_QUESTION.format(item=src["item"]))
            _classify(result, row, src["item"], row.before, "미확인", "업무비 혼합")
            row.essential = row.before
            _note_card_lump(result, row)
        elif _is_estimate(src) and match_rule(src["item"]).necessity != "필수":
            # 어림값에 절감률을 곱하면 없는 정밀도가 생긴다. 표는 그대로 두고 확인을 요청한다
            result.kept.append((ESTIMATED_KEEP, src["item"], row.before))
            _classify(result, row, src["item"], row.before, "미확인", "추정 금액")
            row.essential = row.before
            _note_card_lump(result, row)
        else:
            _analyze_consumption_row(result, row, inp, lines.get((src["section"], src["item"])))
        if row.policy_offset:
            offset = row.policy_offset
            note = policy_note(offset["policy_name"])
            assumptions = list(offset["effect"].get("assumptions", []))
            if note:
                assumptions.append(note.conditions)
            if row.policy_valid_note:
                assumptions.append(row.policy_valid_note)
            result.recommendations.append(_recommendation(
                f"policy:{offset['policy_id']}", "정책 혜택", row, offset["policy_name"],
                f"{offset['policy_name']} 신청", row.before, row.before - row.policy, "기관 확인 전", assumptions, "이번 달 안에"))
    if blanks:
        result.questions.insert(0, BLANK_QUESTION.format(items=", ".join(blanks)))
    result.recommendations.sort(key=lambda rec: rec["amount"]["monthly_change_won"])  # 효과가 큰 순서
    blank_income = [INCOME_LABELS.get(r["basis"], r["basis"]) for r in inp.income_cashflow if r["amount_won"] is None]
    if blank_income:
        result.questions.insert(0, BLANK_INCOME_QUESTION.format(items=", ".join(blank_income)))
    return result


# ── 표와 지표 ────────────────────────────────────────────────
def _pct(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator > 0 else None


def _metric(code, name, unit, before, after, formula, note=""):
    return {"code": code, "name": name, "unit": unit, "before": before, "after": after, "formula": formula, "note": note}


def _span(item: dict, key: str = "monthly_won") -> tuple[int | None, int | None]:
    low, high = item.get("low_won"), item.get("high_won")
    return (low, high) if low != high else (None, None)


def _cashflow_table(inp: ReportInput, rows: list[Row], freed: int) -> dict:
    blocks = []
    for name in FF_BLOCKS:
        out = []
        for r in rows:
            if r.block != name:
                continue
            low, high = _span(r.source)
            out.append({"row_id": r.rid, "label": r.source["item"], "before_won": r.before, "low_won": low, "high_won": high,
                        "estimated": r.known and _is_estimate(r.source), "not_applicable": r.source["status"] == "해당없음", "policy_won": r.policy, "adjustment_won": r.adjustment,
                        "after_won": r.after, "delta_won": 0 if not r.known else r.after - r.before,
                        "changed": r.known and r.after != r.before, "is_reserve": False})
        if name == "저축" and freed:
            out.append({"row_id": "reserve", "label": RESERVE_LABEL, "before_won": 0, "low_won": None, "high_won": None, "estimated": False,
                        "policy_won": 0, "adjustment_won": 0, "after_won": freed, "delta_won": freed, "changed": True, "is_reserve": True})
        before, after = sum(x["before_won"] or 0 for x in out), sum(x["after_won"] or 0 for x in out)
        blocks.append({"block": name, "rows": out,
                       "subtotal": {"before_won": before, "after_won": after, "delta_won": after - before}})
    income = []
    for r in inp.income_cashflow:
        low, high = _span(r)
        income.append({"label": INCOME_LABELS.get(r["basis"], r["basis"]), "before_won": r["amount_won"], "after_won": r["amount_won"],
                       "low_won": low, "high_won": high, "estimated": r["amount_won"] is not None and _is_estimate(r)})
    inflow = sum(i["before_won"] or 0 for i in income)
    outflow = sum(b["subtotal"]["before_won"] for b in blocks)
    unknown = sum(1 for r in rows if not r.known)
    low_in = sum(i["low_won"] if i["low_won"] is not None else (i["before_won"] or 0) for i in income)
    high_in = sum(i["high_won"] if i["high_won"] is not None else (i["before_won"] or 0) for i in income)
    rows_low = sum(r.source.get("low_won") if r.source.get("low_won") is not None else (r.before or 0) for r in rows)
    rows_high = sum(r.source.get("high_won") if r.source.get("high_won") is not None else (r.before or 0) for r in rows)
    complete = unknown == 0 and all(i["before_won"] is not None for i in income)
    return {
        "unit": "KRW", "income": income, "blocks": blocks,
        "totals": {"income_before_won": inflow, "income_after_won": inflow, "outflow_before_won": outflow,
                   "outflow_after_won": sum(b["subtotal"]["after_won"] for b in blocks),
                   "unverified_income_won": inp.fact("unverified_income"), "unverified_outflow_won": inp.fact("unverified_outflow"),
                   "complete": complete, "known_rows": len(rows) - unknown, "unknown_rows": unknown,
                   "unexplained_won": inflow - outflow if not complete else None,
                   "unexplained_range_won": [low_in - rows_high, high_in - rows_low] if not complete else None},
    }


def _diagnosis(analysis: Analysis, complete: bool) -> dict:
    """소비지출(저축·보험·세금 제외)의 필수성 구성. 빈칸이 있으면 비중이 왜곡되므로 만들지 않는다."""
    if not complete:
        return {"available": False}
    entries = [c for c in analysis.classification if c["basis"] in ("분류 규칙", "업무비 혼합", "추정 금액")]
    total = sum(c["monthly_won"] for c in entries)
    groups = []
    for necessity in ("필수", "일부조정", "선택", "미확인"):
        members = [c for c in entries if c["necessity"] == necessity]
        before = sum(c["monthly_won"] for c in members)
        reduced = sum(c["cut_won"] + c["policy_won"] for c in members)
        groups.append({"necessity": necessity, "before_won": before, "share_pct": _pct(before, total), "after_won": before - reduced,
                       "change_won": -reduced})
    flexible = sorted((c for c in entries if c["necessity"] in ("일부조정", "선택")), key=lambda c: -c["monthly_won"])
    return {"available": True, "consumption_won": total, "by_necessity": groups,
            "flexible_share_pct": _pct(sum(c["monthly_won"] for c in flexible), total),
            "top_flexible": [{"label": c["label"], "monthly_won": c["monthly_won"], "necessity": c["necessity"],
                              "share_pct": _pct(c["monthly_won"], total)} for c in flexible[:5]]}


def calculate(inp: ReportInput, analysis: Analysis) -> dict:
    rows = analysis.rows
    if any(r.known and r.after < 0 for r in rows):
        raise ValueError("적용 후 금액이 0원보다 작은 행이 있습니다")
    policy_total, adjust_total = sum(r.policy for r in rows), sum(r.adjustment for r in rows)
    freed = policy_total + adjust_total
    table = _cashflow_table(inp, rows, freed)
    totals = table["totals"]
    if totals["outflow_after_won"] != totals["outflow_before_won"]:
        raise ValueError("적용 전후 지출 합계가 다릅니다")

    complete = totals["complete"]
    by_section = lambda *names: sum(r.before or 0 for r in rows if r.source["section"] in names)
    inflow = totals["income_before_won"]
    taxes = by_section("세금·사회보험")
    disposable = inflow - taxes
    saving, insurance = by_section("저축"), by_section("보험")
    consumption = by_section("고정지출", "변동지출")
    debt_rows = [r for r in rows if "대출" in r.source["item"]]
    debt = sum(r.before or 0 for r in debt_rows)
    debt_known = bool(debt_rows) and all(r.known for r in debt_rows) and inflow > 0
    residual = disposable - consumption - insurance - saving

    liquid = sum(a["balance_won"] or 0 for a in inp.assets if a["kind"] in LIQUID_KINDS and not a["earmark"])
    essential = sum(r.essential for r in rows)
    essential_after = essential - sum(r.essential_cut for r in rows)
    months = lambda spend: round(liquid / spend, 1) if spend > 0 else None
    only_if_complete = lambda value: value if complete else None
    blank_note = "지출 금액 일부가 비어 있어 계산하지 않음"

    def scenario(name, freed_won):
        return {"name": name, "monthly_freed_won": freed_won, "residual_won": only_if_complete(residual + freed_won),
                "saving_rate_pct": only_if_complete(_pct(saving + freed_won, disposable)),
                "consumption_ratio_pct": only_if_complete(_pct(consumption - freed_won, disposable))}

    annual = sum(r.policy_annual for r in rows) + adjust_total * 12
    notes = [r.policy_valid_note for r in rows if r.policy_valid_note]
    note = lambda text: text if complete else blank_note
    return {
        "cashflow": table,
        "diagnosis": _diagnosis(analysis, complete),
        "effect": {"monthly_freed_won": freed, "policy_monthly_won": policy_total, "adjustment_monthly_won": adjust_total,
                   "annual_freed_won": annual, "annual_note": " ".join(notes)},
        "scenarios": [scenario("현재", 0), scenario("정책 혜택만", policy_total),
                      scenario("소비 조정만", adjust_total), scenario("정책 혜택 + 소비 조정", freed)],
        "metrics": [
            _metric("R01", "월 잔여현금흐름", "won", only_if_complete(residual), only_if_complete(residual + freed),
                    "세후 가용소득 − (소비지출 + 보험 + 계획저축)", note("적용 후 금액은 배분을 기다리는 절감 재원")),
            _metric("저축률", "저축률", "pct", only_if_complete(_pct(saving, disposable)), only_if_complete(_pct(saving + freed, disposable)),
                    "계획저축 ÷ 세후 가용소득", note("적용 후는 절감 재원을 전부 저축한다고 볼 때")),
            _metric("소비", "소비지출 비중", "pct", only_if_complete(_pct(consumption, disposable)),
                    only_if_complete(_pct(consumption - freed, disposable)), "(고정지출 + 변동지출) ÷ 세후 가용소득", note("")),
            _metric("R02", "소득 중단 시 유지 개월", "months", only_if_complete(months(essential)), only_if_complete(months(essential_after)),
                    "가용 유동자산 ÷ 위기 시 필수지출", note(f"가용 유동자산 {liquid:,}원. 유지소득 0원 가정. 적정 개월 수는 고객별로 판단")),
            _metric("R04", "상환부담률", "pct", _pct(debt, disposable) if debt_known else None, _pct(debt, disposable) if debt_known else None,
                    "월 원리금상환액 ÷ 세후 가용소득",
                    "규제 DSR과 다른 내부 지표" if complete or not debt_known else "규제 DSR과 다른 내부 지표. 수입과 상환액 모두 고객 어림값"),
        ],
        "basis": {"inflow_won": inflow, "taxes_and_social_won": taxes, "disposable_won": disposable},
    }
