import json
from pathlib import Path

import pytest

from customer.profile import connect, load_case, store
from spending_report.inputs import load_input

CASES = Path(__file__).resolve().parents[1] / "customer" / "cases"


def offset(name, monthly=None, certainty="시나리오 사용 가능", kind="비율", **effect):
    eff = {"kind": kind, "basis": "테스트 근거", "assumptions": []}
    if monthly is not None:
        eff["monthly_saving_won"] = [monthly, monthly]
    eff.update(effect)
    return {"policy_id": f"id-{name}", "policy_name": name, "certainty": certainty, "open_conditions": [],
            "effect": eff, "action": {"deadline": None, "url": "https://example.test/apply"}}


def handoff_for(case_id, version, lines=(), one_off=()):
    return {
        "module": "policy_to_spending", "case_id": case_id, "profile_version": version, "generated_at": "x",
        "source": {"module": "policy", "generated_at": "x", "policy_db_as_of": "20261104"},
        "totals": {}, "expense_lines": [{"section": s, "item": i, "policy_offsets": o} for s, i, o in lines],
        "savings_programs": [], "one_off_income": list(one_off), "questions": ["정책 질문"],
        "excluded": {"debt_instruments": 0, "unmapped_financial": [], "targeted_group_unverified": [], "other_services": 0},
    }


@pytest.fixture
def make_input(tmp_path):
    def _make(case_id, lines=(), one_off=(), mutate=None):
        profile = load_case(CASES / f"{case_id}.json")
        if mutate:
            mutate(profile)
        conn = connect(tmp_path / f"{case_id}.db")
        store(conn, profile)
        conn.execute("INSERT INTO module_results VALUES (?,?,?,?,?,?)",
                     (case_id, profile["profile_version"], "policy_to_spending", "completed",
                      json.dumps(handoff_for(case_id, profile["profile_version"], lines, one_off), ensure_ascii=False), "t"))
        conn.commit()
        return load_input(conn, case_id, profile["profile_version"])
    return _make
