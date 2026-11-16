"""입력 → 계산 → 문장 → module_result JSON 조립. 형식은 조립 설명서 9장과 .scratch/spending-report/spec.md를 따른다."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from customer.profile import age_on

from . import engine, guidance, intake
from .inputs import ReportInput
from .narrative import won_text, write_narrative
from .rules import RULES_VERSION

ROOT = Path(__file__).resolve().parents[1]


def check_documents(inp: ReportInput, root: Path = ROOT) -> tuple[dict, tuple[str, dict] | None]:
    """원본 문서와 저장값을 대조한다. 읽지 못해도 보고서는 '미검증'으로 계속 만든다."""
    checks, receipt = {}, None
    path = inp.fact("doc_fact_finding")
    if path:
        try:
            doc = intake.read_cashflow_doc(root / path)
            checks["fact_finding"] = {"file": path, **intake.reconcile_cashflow(doc, inp.cashflow, inp.income_cashflow)}
        except intake.DocumentUnavailable as exc:
            checks["fact_finding"] = {"file": path, "status": "미검증", "reason": str(exc)}
    path = inp.fact("doc_withholding")
    if path:
        try:
            kind, fields = intake.read_withholding(root / path)
            receipt = kind, fields
            result = (intake.reconcile_withholding(fields, inp.tax_items, inp.income_items) if kind == "근로"
                      else intake.reconcile_business(fields))
            checks["withholding"] = {"file": path, "kind": kind, **result}
        except intake.DocumentUnavailable as exc:
            checks["withholding"] = {"file": path, "status": "미검증", "reason": str(exc)}
    return checks, receipt


def _effect_text(effect: dict) -> str:
    kind = effect["kind"]
    if kind in ("비율", "월정액", "정액할인"):
        low, high = effect["monthly_saving_won"]
        return f"월 {won_text(high)}" if low == high else f"월 {won_text(low)}~{won_text(high)}"
    if kind == "지출미확인":
        return "지출 금액을 알려주면 계산"
    if kind == "일시금":
        low, high = effect["one_off_won"]
        return f"일시금 {won_text(high)}" if low == high else f"일시금 {won_text(low)}~{won_text(high)}"
    return "금액은 공고 확인"


def _policy_entry(item: dict, row_label: str | None = None) -> dict:
    return {"policy_id": item["policy_id"], "name": item["policy_name"], "row": row_label, "certainty": item["certainty"],
            "effect": _effect_text(item["effect"]), "open_conditions": item.get("open_conditions", []),
            "deadline": item["action"].get("deadline"), "url": item["action"].get("url")}


def _policy_benefits(handoff: dict, analysis: engine.Analysis) -> dict:
    applied_ids = {r.policy_offset["policy_id"]: r for r in analysis.rows if r.policy_offset}
    out = {"applied": [], "other_scenario": [], "needs_check": []}
    housing_move = 0
    for line in handoff["expense_lines"]:
        for offset in line["policy_offsets"]:
            if offset["effect"]["kind"] == "주거이전":  # 이사가 전제라 소비 계획의 대상이 아니다
                housing_move += 1
                continue
            entry = _policy_entry(offset, line["item"])
            row = applied_ids.get(offset["policy_id"])
            if row is not None:
                out["applied"].append({**entry, "effect": f"월 {won_text(row.policy)}", "basis": offset["effect"]["basis"],
                                       "note": row.policy_valid_note})
            elif offset["certainty"] == "시나리오 사용 가능":
                out["other_scenario"].append(entry)
            else:
                out["needs_check"].append(entry)
    for item in handoff["one_off_income"]:
        out["other_scenario" if item["certainty"] == "시나리오 사용 가능" else "needs_check"].append(_policy_entry(item))
    out["savings_programs"] = [{"name": s["policy_name"], "certainty": s["certainty"], "note": s["note"],
                                "open_conditions": s["open_conditions"], "deadline": s["action"]["deadline"],
                                "url": s["action"]["url"]} for s in handoff["savings_programs"]]
    excluded = handoff["excluded"]
    out["excluded_counts"] = {"debt_instruments": excluded["debt_instruments"], "other_services": excluded["other_services"],
                              "housing_move": housing_move,
                              "targeted_group_unverified": len(excluded["targeted_group_unverified"])}
    return out


def _tax_reference(inp: ReportInput, receipt: tuple[str, dict] | None) -> dict:
    """보고서 7번. 세금 변화는 규칙 엔진이 생기기 전까지 계산하지 않는다."""
    note = "소비 조정과 정책 반영에 따른 세금 변화는 아직 계산하지 않았습니다."
    kind, fields = receipt or (None, None)
    if kind == "사업":
        v = lambda key: fields[key]["value"]
        rows = [("지급총액", v("지급총액"), "won"), ("원천징수 소득세", v("소득세"), "won"), ("원천징수 지방소득세", v("지방소득세"), "won"),
                ("월 지급액 최소", v("월최소"), "won"), ("월 지급액 최대", v("월최대"), "won")]
        return {"source": "사업소득 원천징수영수증 2025년 귀속", "rows": [{"label": a, "value": b, "unit": c} for a, b, c in rows],
                "after_effect": "미계산", "note": f"원천징수는 최종 세금이 아닙니다. 필요경비와 다른 지급처를 확인해야 합니다. {note}"}
    if fields:
        v = lambda key: fields[key]["value"]
        refund = -(v("차감징수_소득세") + v("차감징수_지방소득세"))
        rows = [("총급여", v("총급여"), "won"), ("결정세액(소득세 + 지방소득세)", v("결정세액_소득세") + v("결정세액_지방소득세"), "won"),
                ("연말정산 환급액", refund, "won"), ("실효세율", v("실효세율"), "pct"),
                ("신용카드 등 소득공제", v("신용카드공제"), "won"), ("주택청약 소득공제", v("주택청약공제"), "won"),
                ("월세액 세액공제", v("월세공제"), "won"), ("연금저축 세액공제", v("연금저축공제"), "won"),
                ("보장성보험 세액공제", v("보장성공제"), "won")]
        return {"source": "근로소득 원천징수영수증 2025년 귀속", "rows": [{"label": a, "value": b, "unit": c} for a, b, c in rows],
                "after_effect": "미계산", "note": note}
    rows = [{"label": f"{t['item']} ({t['tax_year']}년{', 월' if t['period'] == '월' else ''})", "value": t["amount_won"], "unit": "won"}
            for t in inp.tax_items]
    return {"source": "종합소득세·사회보험 입력값", "rows": rows, "after_effect": "미계산", "note": note}


def _kept_groups(kept: list[tuple[str, str, int]]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for reason, label, won in kept:
        groups.setdefault(reason, []).append({"label": label, "monthly_won": won})
    return [{"reason": reason, "items": items} for reason, items in groups.items()]


def _bonus_question(inp: ReportInput, receipt: tuple[str, dict] | None) -> list[str]:
    """영수증에 상여가 있는데 FF 성과금 칸이 비어 있으면 수입이 빠졌을 수 있다."""
    row = next((r for r in inp.income_cashflow if r["basis"] == "성과금"), None)
    if not receipt or receipt[0] != "근로" or row is None or row["amount_won"] is not None:
        return []
    bonus = receipt[1]["상여"]["value"]
    return [f"2025년 영수증에 상여 {won_text(bonus)}이 있습니다. 올해 성과금 예상액을 알려주세요"] if bonus else []


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _limitations(inp: ReportInput, complete: bool) -> list[str]:
    notes = ["소비 조정 절감률은 임시 가정이며 재무팀 검토와 고객 합의 전입니다.",
             "거래내역 없이 Fact Finding의 월 배분표만으로 계산했습니다.",
             "정책 혜택은 입력값 기준 판정이며 기관의 최종 선정과 다릅니다.",
             "세금 변화와 목표별 자금 배분은 아직 반영하지 않았습니다.",
             f"정책 정보 기준일은 {inp.handoff['source']['policy_db_as_of']}입니다. 신청 전 공고를 다시 확인해야 합니다."]
    if not complete:
        notes.insert(0, "지출 금액 일부가 비어 있어 적용 후 효과와 비율 지표를 확정하지 않았습니다. 빈칸은 0원으로 계산하지 않았습니다.")
    if inp.customer_type in ("사업·프리랜서", "겸업"):
        notes.append("수입은 연간 매출의 월평균입니다. 실제 입금월이 달라 월별로는 자금이 부족할 수 있습니다.")
    return notes


def build_report(inp: ReportInput, client, log_file: str | None = None, root: Path = ROOT) -> tuple[dict, list[dict]]:
    documents, receipt = check_documents(inp, root)
    analysis = engine.analyze_rows(inp)
    calc = engine.calculate(inp, analysis)
    narrative, ai_log = write_narrative(calc, analysis.recommendations, client)

    sources = [narrative["summary"]["source"], *(c["source"] for c in narrative["coaching"])]
    mismatch = [d for d in documents.values() if d["status"] == "불일치"]
    questions = analysis.questions + _bonus_question(inp, receipt) + inp.handoff.get("questions", [])
    questions += [f"원본 문서와 입력값이 다릅니다. '{d['file']}'를 확인해 주세요" for d in mismatch]
    benefits = _policy_benefits(inp.handoff, analysis)
    requests = guidance.data_requests(inp, calc, analysis)
    covered = ("금액이 비어", "수입 칸이", "카드 명세서") if requests else ()
    birth = inp.fact("birth_date")
    report = {
        "module": "spending",
        "case_id": inp.case_id,
        "profile_version": inp.profile_version,
        "as_of_date": inp.as_of_date,
        "is_synthetic": inp.is_synthetic,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "needs_review" if mismatch else "partial",
        "customer": {"name": inp.fact("name"), "age": age_on(birth, inp.as_of_date) if birth else None,
                     "occupation": inp.fact("occupation"), "customer_type": inp.customer_type,
                     "household_size": inp.fact("household_size"), "region": inp.fact("region_sigungu")},
        "findings": {"classification": analysis.classification, "kept": _kept_groups(analysis.kept),
                     "policy_benefits": benefits, "deduction_flags": guidance.deduction_flags(inp, receipt),
                     "income_tax_reference": _tax_reference(inp, receipt)},
        "calculations": calc,
        "recommendations": analysis.recommendations,
        "conditional_options": analysis.conditional,
        "data_requests": requests,
        "action_plan": guidance.action_plan(inp, calc, analysis, benefits, requests),
        "narrative": narrative,
        "evidence": {"documents": documents, "policy_source": inp.handoff["source"], "rules_version": RULES_VERSION},
        "missing_fields": sorted(k for k, f in inp.facts.items() if f["status"] == "미확인"),
        "questions": _unique(questions),
        "questions_other": [q for q in _unique(questions) if not q.startswith(covered or ("\0",))],
        "limitations": _limitations(inp, calc["cashflow"]["totals"]["complete"]),
        "review": {"state": "담당자 검토 전", "reviewer": None, "reviewed_at": None, "customer_choice": "미완료"},
        "ai_usage": {"provider": client.provider, "model": client.model,
                     "sentences_llm": sources.count("llm"), "sentences_template": sources.count("template"),
                     "log_file": log_file},
    }
    return report, ai_log
