"""planning.advisor 시험. 가짜 클라이언트로 채택·거부·대체를 본다. 네트워크·모델은 쓰지 않는다."""
import copy
import json
import re
from pathlib import Path

import pytest

from planning.advisor import advise, template_order, term_draft
from spending_report.llm import LLMUnavailable, NoLLM

ROOT = Path(__file__).resolve().parents[1]


PRINCIPLES = {"S1": ["P02", "P03"], "S2": ["P05"], "M1": ["P08", "P10"], "L2": ["P12", "P13"], "L1": ["P12"]}


def card(id, term, priority, action, numbers):
    return {"id": id, "term": term, "goal": None, "title": f"{id} 검토", "action": action, "numbers": numbers,
            "basis": "", "evidence": [], "confirm": [], "certainty": "계산값", "priority": priority,
            "principles": PRINCIPLES.get(id, [])}


STRATEGY = {
    "cards": [
        card("S1", "단기", 1, "비상금 목표 600만원 대비 즉시 인출 자산이 400만원으로 200만원 부족합니다.",
             {"비상금 목표(만원)": 600.0, "부족액(만원)": 200.0}),
        card("S2", "단기", 2, "부채 잔액 1,000만원의 금리는 5.4%입니다.", {"부채 합계(만원)": 1000.0, "금리(%)": 5.4}),
        card("M1", "중기", 1, "주거 목표는 계획 월 적립 50만원으로 충족률 40%입니다.", {"계획 월 적립(만원)": 50.0}),
        card("L2", "장기", 2, "노후 목표는 계획 월 적립 10만원입니다.", {"계획 월 적립(만원)": 10.0}),
        card("L1", "장기", 1, "연금계좌 공제액은 15.8만원입니다.", {"현재 공제액(만원)": 15.8}),
    ],
    "guide": {"principles": [{"id": "P02", "title": "보호자금 우선", "text": "비상자금을 먼저 채운다."},
                             {"id": "P03", "title": "곧 쓸 돈은 원금보전", "text": "가까운 자금은 원금보전 자산에 둔다."},
                             {"id": "P05", "title": "부채는 이자와 만기부터", "text": "금리와 만기를 먼저 본다."},
                             {"id": "P08", "title": "주택은 자기자본과 대출을 함께 검증", "text": "주택 자기자본을 계산한다."},
                             {"id": "P10", "title": "사용 시점이 다가오면 위험 축소", "text": "위험 비중을 낮춘다."},
                             {"id": "P12", "title": "노후는 계좌를 나눠 준비", "text": "연금 계좌를 함께 본다."},
                             {"id": "P13", "title": "은퇴까지 남은 기간에 따라 비중 조정", "text": "단계별 운용."}]},
    "summary": {"M": 4700.0, "locked": 800.0, "debt": 1000.0, "surplus_after": 151.0, "monthly_need": 434.9,
                "monthly_shortfall": 283.9},
}


def _draft_of(messages):
    m = re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", messages[1]["content"], re.S)
    return m.group(1) if m else None


def _cards_of(messages):
    m = re.search(r"\[카드\]\n(.*?)\n\n\[", messages[1]["content"], re.S)
    return json.loads(m.group(1)) if m else None


class Fake:
    provider, model = "fake", "fake-1"

    def __init__(self, order=lambda ids: ids[::-1], text=lambda draft: draft, focus="먼저 유동성을 확인합니다."):
        self.order, self.text, self.focus = order, text, focus
        self.prompts = []

    def chat(self, messages, max_tokens=200):
        self.prompts.append(copy.deepcopy(messages))
        cards = _cards_of(messages)
        if cards is not None:
            return json.dumps({"order": self.order([c["id"] for c in cards]), "focus": self.focus}, ensure_ascii=False)
        return self.text(_draft_of(messages))


class Down:
    provider, model = "server", "x"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, max_tokens=200):
        self.calls += 1
        raise LLMUnavailable("연결 실패")


def test_none_provider_uses_templates_without_calls():
    out = advise(STRATEGY, NoLLM())
    assert out["calls"] == 0 and out["provider"] == "none"
    assert out["terms"]["장기"]["order"] == ["L1", "L2"]  # priority 순
    for t in out["terms"].values():
        assert t["source"] == "template" and t["order_source"] == "template"
    assert out["summary"]["source"] == "template" and "283.9만원" in out["summary"]["text"]


def test_valid_llm_answers_are_accepted():
    log = []
    fake = Fake()
    out = advise(STRATEGY, fake, log=log)
    assert out["terms"]["단기"]["order"] == ["S2", "S1"] and out["terms"]["단기"]["order_source"] == "llm"
    assert out["terms"]["단기"]["focus"] == "먼저 유동성을 확인합니다."
    assert all(t["source"] == "llm" for t in out["terms"].values())
    assert out["summary"]["source"] == "llm"
    assert out["rejected"] == 0 and out["accepted"] == out["calls"] == len(log)
    for entry in log:
        assert {"stage", "term", "prompt_hash", "response", "accepted", "reason"} <= set(entry)


def test_bad_order_is_rejected():
    log = []
    out = advise(STRATEGY, Fake(order=lambda ids: ids[:1] + ["X9"]), log=log)
    for term in ("단기", "장기"):
        t = out["terms"][term]
        assert t["order_source"] == "template"
        assert t["order"] == template_order([c for c in STRATEGY["cards"] if c["term"] == term])
    rejected = [e for e in log if e["stage"] == "order" and not e["accepted"]]
    assert rejected and "순열" in rejected[0]["reason"]


def test_unparsable_order_is_rejected():
    class NotJson(Fake):
        def chat(self, messages, max_tokens=200):
            return "순서는 S1, S2입니다." if _cards_of(messages) is not None else _draft_of(messages)
    out = advise(STRATEGY, NotJson())
    assert out["terms"]["단기"]["order_source"] == "template" and out["rejected"] >= 1


def test_sentence_with_new_number_is_rejected_after_retry():
    log = []
    out = advise(STRATEGY, Fake(text=lambda d: d + " 추가로 월 999만원이 필요합니다."), log=log)
    for t in out["terms"].values():
        assert t["source"] == "template"
    assert out["summary"]["source"] == "template"
    narr = [e for e in log if e["stage"] == "narrative" and e["term"] == "단기"]
    assert len(narr) == 2 and not any(e["accepted"] for e in narr)  # 한 번 재시도 후 템플릿
    assert "사실에 없는 금액" in narr[0]["reason"]
    assert out["terms"]["단기"]["narrative"] == term_draft(
        "단기", [c for c in STRATEGY["cards"] if c["id"] in ("S2", "S1")][::-1],
        ["보호자금 우선", "곧 쓸 돈은 원금보전", "부채는 이자와 만기부터"])


def test_llm_unavailable_falls_back_to_template():
    down = Down()
    log = []
    out = advise(STRATEGY, down, log=log)
    assert down.calls == 1  # 첫 실패 뒤로는 부르지 않는다
    assert out["calls"] == 1 and out["accepted"] == 0
    assert all(t["source"] == "template" for t in out["terms"].values())
    assert log[0]["accepted"] is False and "LLM 사용 불가" in log[0]["reason"]


KIM_POLICY = ROOT / "output" / "policy_kim_doyun_v2.json"


@pytest.mark.skipif(not KIM_POLICY.exists(), reason="output/policy_kim_doyun_v2.json 없음")
def test_prompts_have_no_personal_or_institution_data():
    from customer.profile import load_case
    from planning.reflect import build
    from planning.spending_cut import spending_cut
    from planning.strategy import build_strategy

    step2 = json.loads((ROOT / "planning" / "cases" / "kim_doyun_step2.json").read_text(encoding="utf-8"))
    profile = load_case(ROOT / "customer" / "cases" / "kim_doyun.json")
    policy = json.loads(KIM_POLICY.read_text(encoding="utf-8"))
    st = build_strategy(build(copy.deepcopy(step2), profile, policy, "certain"), profile, policy,
                        spending_cut(profile, policy, copy.deepcopy(step2)))
    fake = Fake()
    advise(st, fake)
    text = json.dumps(fake.prompts, ensure_ascii=False)
    forbidden = ["김도윤", "가상로", "성남시", "중원구", "가상 A은행", "가상생활유통", "가상고객데이터", ".hwp", ".docx",
                 "http", "금융위원회", "경기도", "K-패스"]
    forbidden += [f["policy_name"] for f in policy["findings"]]
    forbidden += [f.get("institution") for f in policy["findings"] if f.get("institution")]
    for word in forbidden:
        assert word not in text, word


def _with_principles(chooser):
    fake = Fake()
    base = fake.chat

    def chat(messages, max_tokens=200):
        raw = base(messages, max_tokens)
        if _cards_of(messages) is not None:
            data = json.loads(raw)
            term = messages[1]["content"].split("\n")[1]
            data["principles"] = chooser(term)
            return json.dumps(data, ensure_ascii=False)
        return raw
    fake.chat = chat
    return fake


def test_principles_template_and_llm_choice():
    out = advise(STRATEGY, NoLLM())
    t = out["terms"]["단기"]
    assert t["principles"] == ["P02", "P03", "P05"] and t["principles_source"] == "template"
    assert "적용 원칙은 '보호자금 우선'" in t["narrative"]
    good = _with_principles(lambda term: {"단기": ["P05", "P02"], "중기": ["P10"], "장기": ["P13"]}[term])
    out = advise(STRATEGY, good)
    assert out["terms"]["단기"]["principles"] == ["P05", "P02"] and out["terms"]["단기"]["principles_source"] == "llm"
    assert out["terms"]["장기"]["principles"] == ["P13"]
    system = good.prompts[0][0]["content"]
    assert "[전략 원칙]" in system and "P13 은퇴까지 남은 기간에 따라 비중 조정" in system
    assert "[원칙 후보]\nP02, P03, P05" in good.prompts[0][1]["content"]


def test_principles_outside_candidates_rejected():
    bad = _with_principles(lambda term: ["P99", "P15"])
    log = []
    out = advise(STRATEGY, bad, log=log)
    t = out["terms"]["단기"]
    assert t["principles_source"] == "template" and t["principles"] == ["P02", "P03", "P05"]
    assert t["order_source"] == "llm"  # 순서는 그대로 채택
    assert any("principles 거부" in (e.get("reason") or "") for e in log)
