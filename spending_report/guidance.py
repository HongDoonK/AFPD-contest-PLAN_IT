"""보고서의 행동 지침: 자료 요청 목록, 실행·점검 계획, 공제 확인 항목.

금액을 새로 만들지 않고 계산 결과와 입력값을 사람이 움직일 수 있는 목록으로 정리한다.
공제 항목은 세액을 계산하지 않고 FF와 영수증이 어긋나는 곳만 짚는다.
"""
from __future__ import annotations

from datetime import date

from .engine import Analysis
from .inputs import ReportInput
from .narrative import won_text


def add_months(iso: str, months: int) -> str:
    d = date.fromisoformat(iso)
    index = d.month - 1 + months
    return date(d.year + index // 12, index % 12 + 1, min(d.day, 28)).isoformat()


def data_requests(inp: ReportInput, calc: dict, analysis: Analysis) -> list[dict]:
    """빈칸 고객에게 받을 자료. 계산에 미치는 영향이 큰 순서다."""
    if calc["cashflow"]["totals"]["complete"]:
        return []
    out: list[dict] = []

    def add(item: str, why: str, unlocks: str) -> None:
        out.append({"priority": len(out) + 1, "item": item, "why": why, "unlocks": unlocks})

    self_employed = inp.customer_type in ("사업·프리랜서", "겸업")
    if any(r["amount_won"] is None or r["low_won"] != r["high_won"] for r in inp.income_cashflow):
        add("최근 12개월 입금 내역" if self_employed else "최근 3개월 급여 명세", "수입이 어림값이거나 일부 칸이 비어 있음", "세후 가용소득과 저축률")
    if inp.fact("marital_status") == "기혼":
        add("배우자 수입과 함께 쓰는 생활비 범위", "가구 수입 일부만 적혀 있어 지출이 수입보다 많게 보일 수 있음", "가구 기준 현금흐름")
    if self_employed:
        add("국민연금·건강보험 고지서와 최근 종합소득세 신고 자료", "세금과 사회보험을 뺀 사용 가능 금액을 알 수 없음", "실제 저축 여력")
    if analysis.conditional:
        low, high = analysis.conditional[0]["monthly_range_won"]
        add("카드 명세서 최근 3개월", "카드값에 여러 지출이 섞여 있음", f"항목별 절감 후보. 조건부 효과 월 {won_text(low)}~{won_text(high)} 확인")

    lines = {(l["section"], l["item"]): l for l in inp.handoff.get("expense_lines", [])}
    plain = []
    for row in analysis.rows:
        if row.known:
            continue
        section, item = row.source["section"], row.source["item"]
        if section == "보험" or "대출" in item:
            continue
        offsets = lines.get((section, item), {}).get("policy_offsets", [])
        names = [o["policy_name"] for o in offsets if o["effect"]["kind"] == "지출미확인" and o["certainty"] == "시나리오 사용 가능"]
        if names:
            add(f"{item} 월 금액", "금액이 비어 있어 정책 효과를 계산하지 못함", f"{', '.join(names[:2])} 효과")
        else:
            plain.append(item)
    if plain:
        add(f"{', '.join(plain)} 월 금액", "표의 남은 빈칸", "적용 후 현금흐름과 저축률")

    loan_row_blank = any(not r.known and "대출" in r.source["item"] for r in analysis.rows)
    if inp.liabilities and (loan_row_blank or any(l["rate_pct"] is None for l in inp.liabilities)):
        add("대출 상환표(원금, 이자, 금리)", "상환액의 구성이나 금리를 알 수 없음", "상환부담률과 대출 조정 검토")
    insurance_blank = any(not r.known and r.source["section"] == "보험" for r in analysis.rows)
    if insurance_blank:
        add("보험증권과 보험료 납입 내역", "보험료가 비어 있거나 다른 지출과 겹칠 수 있음", "보험료 중복 제거와 보험 분석")
    return out


def action_plan(inp: ReportInput, calc: dict, analysis: Analysis, benefits: dict, requests: list[dict]) -> dict:
    """실행 항목과 점검 일정. 재무설계 6단계의 실행·모니터링에 해당한다."""
    items = []
    for rec in analysis.recommendations:
        is_policy = rec["kind"] == "정책 혜택"
        items.append({"when": rec["when"], "owner": "고객", "action": rec["action"], "target": rec["target"],
                      "check": "환급 또는 지원 내역 확인" if is_policy else f"월 {won_text(rec['amount']['after_won'])} 이하"})
    for program in benefits["savings_programs"]:
        deadline = program["deadline"]
        if program["certainty"] == "시나리오 사용 가능" and deadline:
            items.append({"when": f"{deadline[:4]}-{deadline[4:6]}-{deadline[6:8]} 전", "owner": "고객·담당자",
                          "action": f"{program['name']} 신청 여부 검토", "target": program["name"], "check": "본인 저축 재원 확인"})
    for request in requests:
        items.append({"when": "다음 상담 전", "owner": "고객", "action": f"{request['item']} 준비", "target": request["item"], "check": request["unlocks"]})
    items.append({"when": "다음 상담", "owner": "담당자", "action": "고객 선택을 기록하고 같은 가정으로 다시 계산", "target": "보고서", "check": "동의·수정·보류 기록"})

    effect = calc["effect"]
    metrics = [{"name": rec["target"], "target_won": rec["amount"]["after_won"]} for rec in analysis.recommendations
               if rec["kind"] == "소비 조정"][:5]
    if effect["monthly_freed_won"]:
        metrics.insert(0, {"name": "절감 재원 별도 이체액", "target_won": effect["monthly_freed_won"]})
    triggers = ["수입, 주거, 가족 구성이 바뀔 때", "정책 모집 조건이 바뀌거나 선정 결과가 나올 때"]
    triggers += [r.policy_valid_note for r in analysis.rows if r.policy_valid_note]
    complete = calc["cashflow"]["totals"]["complete"]
    return {"items": items, "first_check": add_months(inp.as_of_date, 1) if complete else "자료를 받은 뒤 다시 계산",
            "review": add_months(inp.as_of_date, 3), "metrics": metrics, "review_triggers": triggers}


def deduction_flags(inp: ReportInput, receipt: tuple[str, dict] | None) -> list[dict]:
    """FF의 납입 내역과 2025년 영수증의 공제를 대조한다. 공제 가능 여부를 단정하지 않는다."""
    if not receipt or receipt[0] != "근로":
        return []
    fields = receipt[1]
    value = lambda key: (fields.get(key) or {}).get("value") or 0
    monthly = lambda prefix: next((r["monthly_won"] or 0 for r in inp.cashflow if r["item"].startswith(prefix)), 0)
    flags = []

    def flag(item: str, paid: int, check: str) -> None:
        flags.append({"item": item, "finding": f"FF에 월 {won_text(paid)} 지출이 있지만 2025년 영수증의 공제액은 0원입니다.", "check": check})

    if monthly("주택청약") and not value("주택청약공제"):
        flag("주택청약 소득공제", monthly("주택청약"), "작년 납입 여부와 무주택 세대주 요건 확인")
    if monthly("개인연금") and not value("연금저축공제") and not value("퇴직연금공제"):
        flag("연금계좌 세액공제", monthly("개인연금"), "작년 납입 여부와 계좌 종류 확인")
    if monthly("월 임대료") and not value("월세공제"):
        flag("월세액 세액공제", monthly("월 임대료"), "작년 임대차 계약과 전입 요건 확인")
    refund = -(value("차감징수_소득세") + value("차감징수_지방소득세"))
    if refund > 0:
        flags.append({"item": "연말정산 환급", "finding": f"2025년 귀속분 {won_text(refund)}을 환급받았고 월 수입에는 들어 있지 않습니다.", "check": "올해도 비슷한지 확인하고 쓰임을 정하기"})
    return flags
