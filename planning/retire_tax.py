"""노후 재원의 계좌 종류별 배분과 은퇴 뒤 수령 세금 구간 점검(결정적, 오늘 가치 기준, 만원).

목적: 은퇴 뒤 수령액이 세금이 커지는 구간을 넘지 않도록 노후 재원을 계좌 종류별로 나눈다.
- 세제 적격(연금저축·IRP): 연 수령 1,500 이하면 저율 분리과세. 적격 목표 잔액 상한 = 1,500 × 수령 기간
  (균등 수령, 은퇴 뒤 운용 수익 0으로 보는 보수적 계산). 적격 납입은 세액공제 한도(연 900)까지, 상한에 닿으면 멈춘다.
- 그 밖(비적격): (i) 비과세 요건 저축성 보험(월 150 한도) (ii) ISA(연 2,000 한도) (iii) 일반 계좌.
  일반 계좌는 이자·배당이 연 2,000을 넘지 않게 잔액 상한 = 2,000 ÷ 이자·배당 수익률 가정.
- 한도는 명목 고정이라 물가가 오르면 실제로는 더 빨리 닿을 수 있다(오늘 가치 계산의 한계).
판정은 검토 제안이며 세무 자문이 아니다. 제도값은 원문 확인이 필요하다.
"""
from __future__ import annotations

ACCOUNTS = (("qualified", "세제 적격(연금저축·IRP)"), ("insurance", "비과세 저축성 보험"), ("isa", "ISA"),
            ("general", "일반 계좌"))


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def retire_tax_plan(goal: dict, step2: dict, contrib: list[float], extra: list[tuple[int, float]], phases: list[dict],
                    a: dict, stage_of, monthly_real) -> dict | None:
    """goal: 전략 엔진의 노후 목표(need, horizon, inputs, allocated). contrib: 노후 목표 달별 적립(기존 연금 납입 포함).
    extra: [(달, 금액)] 추가 노후 재원. stage_of·monthly_real은 전략 엔진의 수익률 함수."""
    inp = goal.get("inputs") or {}
    living, pension = inp.get("living_min"), inp.get("pension")
    if living is None or pension is None:
        return None
    period = (inp.get("years_before_pension") or 0) + (inp.get("years_with_pension") or 0)
    if period <= 0:
        return None
    lim_q = a["private_pension_separate_limit"]
    lim_fin = a["financial_income_limit"]
    annual_need = max(living - pension, 0) * 12
    need = goal["need"]
    cap_q = lim_q * period
    target_q = min(need, cap_q)
    target_nq = max(need - target_q, 0.0)
    H = goal["horizon"]
    # 달 m부터 은퇴까지 적립 없이 굴릴 때의 성장 배수(기준 경로)
    grow = [1.0] * (H + 1)
    for k in range(H - 1, -1, -1):
        grow[k] = grow[k + 1] * (1 + monthly_real(stage_of("retire", (H - k) / 12, a)["rate"]["base"], a))
    q_month = a["pension_total_limit"] / 12
    ins_month = a["savings_insurance_monthly_limit"]
    isa_month = a["isa_annual_limit"] / 12
    bal = {"qualified": float(step2.get("q") or 0) + float(step2.get("r") or 0),
           "insurance": float(step2.get("s") or 0),
           "isa": 0.0,
           "general": float(goal.get("allocated") or 0)}
    start = dict(bal)
    extra_by_month = {}
    for m, v in extra or []:
        extra_by_month[m] = extra_by_month.get(m, 0.0) + v
    flows = []  # 달마다 계좌별 납입
    q_stopped = None
    for m in range(H):
        total = (contrib[m] if m < len(contrib) else 0.0) + extra_by_month.get(m, 0.0)
        # 이번 달 적격 납입은 은퇴 시점 적격 잔액 전망이 목표를 넘지 않는 만큼만(넣은 돈은 grow[m+1]배로 자람)
        room = max((target_q - bal["qualified"] * grow[m]) / grow[m + 1], 0.0)
        if q_stopped is None and room <= 1e-9:
            q_stopped = m
        q = min(total, q_month, room)
        rest = total - q
        ins = min(rest, ins_month)
        rest -= ins
        isa = min(rest, isa_month)
        rest -= isa
        f = {"qualified": q, "insurance": ins, "isa": isa, "general": rest}
        flows.append(f)
        r = monthly_real(stage_of("retire", (H - m) / 12, a)["rate"]["base"], a)
        for k in bal:
            bal[k] = bal[k] * (1 + r) + f[k]
    by_phase = []
    for p in phases:
        m0 = p["start_month"]
        if m0 >= H:
            break
        f = flows[m0] if m0 < len(flows) else {k: 0.0 for k in bal}
        by_phase.append({"phase": p["phase"], "start": p["start"], **{k: _r(v) for k, v in f.items()},
                         "total": _r(sum(f.values()))})
    general_cap = lim_fin / a["general_account_yield"]
    accounts = []
    for key, name in ACCOUNTS:
        annual = bal[key] / period
        row = {"key": key, "name": name, "start_balance": _r(start[key]), "balance_at_retire": _r(bal[key]),
               "annual_withdrawal": _r(annual), "monthly_by_phase": [{"phase": x["phase"], "start": x["start"],
                                                                     "monthly": x[key]} for x in by_phase]}
        if key == "qualified":
            row.update(limit=f"연 수령 {lim_q:,}만원(저율 분리과세)", headroom=_r(lim_q - annual),
                       verdict="한도 이내" if annual <= lim_q + 1e-6 else f"초과 예상 {annual - lim_q:,.1f}만원",
                       target_cap=_r(cap_q), stopped_month=q_stopped)
        elif key == "general":
            interest = bal[key] * a["general_account_yield"]
            row.update(limit=f"이자·배당 연 {lim_fin:,}만원(분리과세)", interest=_r(interest),
                       headroom=_r(lim_fin - interest), balance_cap=_r(general_cap),
                       verdict="한도 이내" if interest <= lim_fin + 1e-6 else f"초과 예상 {interest - lim_fin:,.1f}만원")
        elif key == "insurance":
            row.update(limit=f"월 {ins_month:,}만원 이하·5년 납입·10년 유지 시 보험차익 비과세", verdict="요건 확인")
        else:
            row.update(limit=f"연 {a['isa_annual_limit']:,}만원 납입", verdict="요건 확인")
        accounts.append(row)
    warnings = []
    gen = next(x for x in accounts if x["key"] == "general")
    if gen["verdict"] != "한도 이내":
        warnings.append(f"일반 계좌 이자·배당이 연 {lim_fin:,}만원을 넘을 것으로 보임: 비과세·분리과세 상품으로 분산 검토")
    qa = next(x for x in accounts if x["key"] == "qualified")
    if qa["verdict"] != "한도 이내":
        warnings.append(f"적격 계좌 연 수령이 {lim_q:,}만원을 넘을 것으로 보임: 수령 기간 연장 또는 비적격 계좌로 분산 검토")
    return {
        "annual_need": _r(annual_need), "period_years": _r(period, 2), "need": _r(need),
        "qualified_cap": _r(cap_q), "target_qualified": _r(target_q), "target_nonqualified": _r(target_nq),
        "nonqualified_annual": _r(max(annual_need - lim_q, 0)),
        "accounts": accounts, "by_phase": by_phase, "general_balance_cap": _r(general_cap),
        "qualified_annual_vs_limit": {"value": qa["annual_withdrawal"], "limit": lim_q},
        "general_interest_vs_limit": {"value": gen.get("interest"), "limit": lim_fin},
        "warnings": warnings,
        "assumptions": [
            "은퇴 뒤 연 필요 인출액 = (월 생활비 − 공적연금 가정) × 12, 수령 기간 = 은퇴~기대여명(노후 필요자금 산식과 같음)",
            "적격 목표 잔액 상한 = 연 1,500만원 × 수령 기간(균등 수령, 은퇴 뒤 운용 수익 0으로 보는 보수적 계산)",
            "적격 납입은 세액공제 한도(연 900만원)까지, 은퇴 시점 적격 잔액 전망이 상한에 닿으면 멈춤",
            "나머지는 비과세 저축성 보험(월 150만원) → ISA(연 2,000만원) → 일반 계좌 순",
            f"일반 계좌 잔액 상한 = 금융소득 {lim_fin:,}만원 ÷ 이자·배당 수익률 {a['general_account_yield'] * 100:g}%",
            "한도는 명목 고정이라 물가가 오르면 실제로는 더 빨리 닿을 수 있음(오늘 가치 계산)",
            "검토 제안이며 세무 자문이 아님. 세법 원문·상품 약관·수령 방식 확인 필요",
        ],
    }
