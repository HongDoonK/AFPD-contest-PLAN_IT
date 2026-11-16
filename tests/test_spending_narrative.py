import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from conftest import offset

from spending_report.engine import analyze_rows, calculate
from spending_report.llm import LLMUnavailable, LocalLLM, NoLLM, ServerLLM, get_client
from spending_report.narrative import POLICY_CONDITION, anchor_map, check_text, tidy, won_text, write_narrative

SOURCE = "- 저축률 30.7%\n- 월 여유 자금 17.6만원\n- 정책 혜택 18,600원\n- 기준일 2027-04-18"


def test_won_text():
    assert won_text(176_000) == "17.6만원"
    assert won_text(4_280_000) == "428만원"
    assert won_text(18_600) == "18,600원"
    assert won_text(0) == "0원"
    assert won_text(None) == "미확인"


@pytest.mark.parametrize("text", [
    "저축률은 30.7%이고 월 17.6만원의 여유 자금이 생깁니다.",
    "월 176,000원을 먼저 따로 모아 두세요.",  # 같은 금액의 다른 표기
    "정책 혜택 18,600원을 주 2회 대중교통 이용으로 챙겨 보세요.",
])
def test_grounded_text_passes(text):
    assert check_text(text, SOURCE, 200) == []


def test_zero_amount_is_money_not_a_crash():
    # 정책 혜택이 0원인 고객에서 LLM을 켜면 검증기가 멈추던 문제
    assert check_text("정책 혜택은 아직 0원입니다.", "- 정책 혜택: 월 0원", 200) == []
    assert any("금액" in p for p in check_text("정책 혜택은 아직 0원입니다.", "- 저축률 30%", 200))


@pytest.mark.parametrize("text,reason", [
    ("월 20만원이 남습니다.", "금액"),
    ("저축률이 40%로 오릅니다.", "비율"),
    ("저축률이 40퍼센트로 오릅니다.", "비율"),
    ("연 2500 정도 모을 수 있습니다.", "수"),
    ("월 십오만 원을 모으세요.", "한글"),
    ("储蓄를 늘리세요.", "한자"),
    ("이 혜택은 무조건 받을 수 있습니다.", "확정"),
    ("ETF를 매수해 보세요.", "확정"),
    ("", "빈 응답"),
    ("가" * 300, "길이"),
])
def test_bad_text_is_rejected(text, reason):
    problems = check_text(text, SOURCE, 200)
    assert any(reason in p for p in problems), problems


class FakeClient:
    provider, model = "server", "fake-qwen"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, messages, max_tokens=200):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _calc(make_input):
    inp = make_input("kim_doyun", [("고정지출", "교통비", [offset("K-패스(K패스)", 24_000)])])
    analysis = analyze_rows(inp)
    return calculate(inp, analysis), analysis.recommendations


def test_no_llm_uses_template_and_logs_nothing(make_input):
    calc, recs = _calc(make_input)
    narrative, log = write_narrative(calc, recs, NoLLM())
    assert narrative["summary"]["source"] == "template"
    assert "17.6만원" in narrative["summary"]["text"]
    assert len(narrative["coaching"]) == len(recs)
    assert log == []


def _draft_of(messages):
    return messages[1]["content"].split("[초안]\n", 1)[1].split("\n\n[요청]", 1)[0]


class ScriptedClient(FakeClient):
    """요약은 초안 그대로, 습관 문장은 replies를 차례로 돌려주고 다 쓰면 초안을 그대로 돌려준다."""

    def __init__(self, *replies):
        super().__init__(None)
        self.replies = list(replies)

    def chat(self, messages, max_tokens=200):
        self.calls.append(messages)
        if "2~3문장" in messages[1]["content"] or not self.replies:
            return _draft_of(messages)
        return self.replies.pop(0)


GOOD_COACHING = "외식과 배달 횟수를 미리 정해 두면 월 20만원인 지출을 15만원으로 줄일 수 있습니다."


def test_good_llm_text_is_used_and_logged(make_input):
    calc, recs = _calc(make_input)
    client = ScriptedClient()
    narrative, log = write_narrative(calc, recs, client)
    assert narrative["summary"]["source"] == "llm"
    assert all(c["source"] == "llm" for c in narrative["coaching"])
    assert log[0]["accepted"] and log[0]["raw_output"] and log[0]["model"] == "fake-qwen"
    assert log[0]["messages"][0]["role"] == "system"


def test_retry_tells_the_model_why_it_was_rejected(make_input):
    calc, recs = _calc(make_input)
    client = ScriptedClient("하루 커피값 4,500원을 아껴 보세요.", GOOD_COACHING)
    narrative, log = write_narrative(calc, recs, client)
    first = narrative["coaching"][0]
    assert first["source"] == "llm" and first["text"] == GOOD_COACHING
    retry = [e for e in log if e["section"] == f"coaching:{first['rec_id']}"][1]["messages"]
    # 거절된 답과 사유를 붙여 다시 묻는다. 같은 질문을 되풀이하지 않는다
    assert retry[-2] == {"role": "assistant", "content": "하루 커피값 4,500원을 아껴 보세요."}
    assert "사실에 없는 금액: 4,500원" in retry[-1]["content"]


def test_summary_must_keep_the_draft_numbers(make_input):
    calc, recs = _calc(make_input)
    client = FakeClient("여유 자금이 생겨 저축을 늘릴 수 있습니다.")
    narrative, log = write_narrative(calc, recs, client)
    assert narrative["summary"]["source"] == "template"
    problems = [p for e in log if e["section"] == "summary" for p in e["problems"]]
    assert "초안의 금액 누락: 17.6만원" in problems and any(p.startswith("초안의 비율 누락") for p in problems)


COACH_DRAFT = "쓰지 않는 구독을 해지해 보세요. 월 5만원에서 4만원으로 줄이는 안입니다."
COACH_FACTS = "- 항목: 구독\n- 현재: 월 5만원\n- 적용 후: 월 4만원\n- 월 변화: 1만원 감소\n" + COACH_DRAFT


@pytest.mark.parametrize("text,reason", [
    ("쓰지 않는 구독을 줄여 보세요. 월 5회에서 4회로 줄이는 것을 추천합니다.", "초안의 금액 누락: 5만원"),  # 단위 바꿔치기
    ("쓰지 않는 구독을 해지해 월 4만원에서 5만원으로 줄여 보세요.", "금액 순서"),
    ("쓰지 않는 구독을 해지해 월 5만원에서 4만원으로 줄여 경제적 여유를 확보하겠습니다.", "다짐"),
    ("쓰지 않는 구독을 해지해 보세요. 현재 월 5만원에서 4만원으로 조정되었습니다.", "이미 한 일"),  # Qwen2.5-7B 실제 사례
])
def test_coaching_must_keep_amounts_in_order_and_voice(text, reason):
    problems = check_text(text, COACH_FACTS, 160, required=COACH_DRAFT, advice=True, ordered=True)
    assert any(reason in p for p in problems), problems


def test_faithful_coaching_passes():
    text = "쓰지 않는 구독을 해지하시면 월 5만원인 구독료를 4만원으로 줄일 수 있어 좋겠습니다."
    assert check_text(text, COACH_FACTS, 160, required=COACH_DRAFT, advice=True, ordered=True) == []


def test_policy_coaching_keeps_the_eligibility_condition(make_input):
    calc, recs = _calc(make_input)
    # Qwen2.5-3B 실제 사례: '조건을 채우면'을 빼서 신청만 하면 받는 것처럼 읽힌다
    client = ScriptedClient("고객님은 외식 횟수를 미리 정해 두시면 월 20만원에서 15만원으로 줄일 수 있습니다.",
                            "쇼핑 전에 며칠 기다리시면 월 25만원에서 20만원으로 줄일 수 있습니다.",
                            "K-패스(K패스) 신청을 하시면 월 2.4만원의 부담이 줄어들어 좋겠습니다.")
    narrative, log = write_narrative(calc, recs, client)
    policy = next(e for e in log if e["section"].startswith("coaching:policy"))
    assert "초안의 단서 누락: 채우면" in policy["problems"]


def test_policy_condition_must_be_met_not_just_checked():
    draft = "K-패스(K패스) 신청 조건을 확인해 보세요. 조건을 채우면 월 2.4만원의 부담이 줄어드는 것으로 계산됩니다."
    rules = dict(required=draft, advice=True, ordered=True, keep=(POLICY_CONDITION,))
    # Qwen3-4B(bf16, Q4_K_M) 실제 사례: '조건'은 남았지만 확인만 하면 부담이 줄어드는 것처럼 읽힌다
    checked = "K-패스(K패스) 신청 조건을 확인해 보시면 월 2.4만원의 부담이 줄어드는 것으로 계산됩니다."
    assert "초안의 단서 누락: 채우면" in check_text(checked, draft, 160, **rules)
    met = "K-패스(K패스) 신청 조건을 확인해 보시고, 조건을 충족하시면 월 2.4만원의 부담이 줄어드는 것으로 계산됩니다."
    assert check_text(met, draft, 160, **rules) == []


def test_customer_in_a_normal_phrase_is_not_meta():
    assert check_text("이 값은 기관 선정과 고객의 선택 전 예상치입니다.", SOURCE, 200) == []
    assert any("머리말" in p for p in check_text("좋습니다. 고객이 이번 달부터 외식을 줄여 보세요.", SOURCE, 200))


SUMMARY_FACTS = ["세후 가용소득: 428만원", "현재 저축률: 30.7%", "합계 월 여유 자금: 17.6만원", "적용 후 저축률: 34.8%"]
SUMMARY_DRAFT = ("현재 세후 가용소득은 428만원이고 저축률은 30.7%입니다. 월 17.6만원의 여유 자금을 모두 저축하면 저축률은 "
                 "34.8%가 됩니다. 이 값은 기관 선정과 고객 선택 전의 시뮬레이션입니다.")


def _summary_problems(text):
    source = "\n".join(SUMMARY_FACTS) + "\n" + SUMMARY_DRAFT
    return check_text(text, source, 420, required=SUMMARY_DRAFT, anchors=anchor_map(SUMMARY_FACTS),
                      keep=(("시뮬레이션", "예상", "선정"),))


def test_numbers_must_stay_with_their_items():
    # Qwen2.5-3B가 빠진 비율을 엉뚱한 문장에 끼워 넣은 실제 사례
    text = ("세후 가용소득은 428만원입니다. 월 17.6만원의 여유 자금을 모두 저축하면 저축률이 34.8%가 됩니다. "
            "이는 세후 가용소득의 30.7%입니다. 이는 시뮬레이션 결과입니다.")
    assert "숫자와 항목이 맞지 않음: 30.7%" in _summary_problems(text)
    assert _summary_problems(SUMMARY_DRAFT) == []


def test_summary_keeps_the_simulation_caveat():
    text = "세후 가용소득은 428만원이고 저축률은 30.7%입니다. 월 17.6만원의 여유 자금을 모두 저축하면 저축률은 34.8%가 됩니다."
    assert "초안의 단서 누락: 시뮬레이션" in _summary_problems(text)
    assert _summary_problems(text + " 이는 기관 선정 전의 예상치입니다.") == []


def test_meta_answers_are_cleaned_or_rejected():
    assert tidy('물론입니다. 다음은 수정된 문장입니다: "외식은 주 1회로 줄여 보세요."') == "외식은 주 1회로 줄여 보세요."
    assert any("머리말" in p for p in check_text("고객이 이번 달부터 실천할 수 있는 습관은 다음과 같습니다.", SOURCE, 200))


def test_tidy_removes_wrapping_only():
    assert tidy('"문장: 외식은 주 1회로 줄여 보세요."') == "외식은 주 1회로 줄여 보세요."
    assert tidy("**외식**은 주 1회로\n줄여 보세요.") == "외식은 주 1회로 줄여 보세요."
    assert tidy("- 외식 줄이기\n- 구독 정리") == "- 외식 줄이기\n- 구독 정리"  # 목록은 고치지 않고 거절한다


@pytest.mark.parametrize("text,reason", [
    ("- 외식 줄이기\n- 구독 정리", "형식"),
    ("다듬은 문장입니다. 외식을 줄여 보세요.", "머리말"),
    ("이번 달부터 외식을 줄인다.", "존댓말"),
    ("외식을 줄여 보세요. 구독도 정리", "존댓말"),
])
def test_format_and_tone_are_checked(text, reason):
    assert any(reason in p for p in check_text(text, SOURCE, 200)), check_text(text, SOURCE, 200)


def test_invented_numbers_fall_back_to_template(make_input):
    calc, recs = _calc(make_input)
    client = FakeClient("월 99만원을 절약할 수 있습니다.")
    narrative, log = write_narrative(calc, recs, client)
    assert narrative["summary"]["source"] == "template"
    assert "99만원" not in json.dumps(narrative, ensure_ascii=False)
    summary_attempts = [e for e in log if e["section"] == "summary"]
    assert len(summary_attempts) == 2 and not any(e["accepted"] for e in summary_attempts)


def test_llm_outage_stops_further_calls(make_input):
    calc, recs = _calc(make_input)
    client = FakeClient(LLMUnavailable("서버 없음"))
    narrative, log = write_narrative(calc, recs, client)
    assert len(client.calls) == 1
    assert all(c["source"] == "template" for c in narrative["coaching"])
    assert log[0]["problems"] == ["서버 없음"]


def test_personal_details_are_not_sent(make_input):
    calc, recs = _calc(make_input)
    client = FakeClient("좋습니다.")
    write_narrative(calc, recs, client)
    sent = json.dumps(client.calls, ensure_ascii=False)
    assert "김도윤" not in sent and "성남" not in sent


def test_get_client_selection(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert get_client().provider == "none"
    assert get_client("local").model == "Qwen/Qwen3-4B-Instruct-2507"
    assert get_client("local", "Qwen/Qwen2.5-7B-Instruct").model == "Qwen/Qwen2.5-7B-Instruct"
    monkeypatch.setenv("LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct")
    assert get_client("local").model == "Qwen/Qwen2.5-14B-Instruct"
    monkeypatch.setenv("LLM_PROVIDER", "server")
    assert get_client().provider == "server"
    with pytest.raises(ValueError):
        get_client("unknown")


def test_server_client_round_trip():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["auth"] = self.headers.get("Authorization")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"choices": [{"message": {"content": "<think>생각</think>다듬은 문장입니다."}}],
                               "usage": {"completion_tokens": 7, "generation_ms": 90},
                               "timings": {"prompt_n": 30, "prompt_ms": 120.5, "predicted_n": 7}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = ServerLLM("Qwen/Qwen2.5-3B-Instruct", f"http://127.0.0.1:{server.server_port}/v1", api_key="test-key")
        assert client.chat([{"role": "user", "content": "안녕"}], max_tokens=20) == "다듬은 문장입니다."
        assert client.last_usage == {"completion_tokens": 7, "generation_ms": 90}
        assert client.last_timings == {"prompt_n": 30, "prompt_ms": 120.5, "predicted_n": 7}  # llama.cpp 서버가 잰 시간
    finally:
        server.shutdown()
    assert received["auth"] == "Bearer test-key"
    assert received["path"] == "/v1/chat/completions"
    assert received["body"]["model"] == "Qwen/Qwen2.5-3B-Instruct"
    assert received["body"]["temperature"] == 0 and received["body"]["max_tokens"] == 20


def test_server_failure_is_unavailable():
    client = ServerLLM("m", "http://127.0.0.1:9/v1", timeout=1, retry_wait=0)
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "x"}])


@pytest.mark.parametrize("statuses,expected", [([502, 200], "ok"), ([401, 200], 401), ([524, 524, 200], 524)])
def test_server_retries_only_transient_errors(statuses, expected):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status = statuses[len(seen)]
            seen.append(status)
            body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode() if status == 200 else b"{}"
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = ServerLLM("m", f"http://127.0.0.1:{server.server_port}/v1", retry_wait=0)
        if expected == "ok":
            assert client.chat([{"role": "user", "content": "x"}]) == "ok"
        else:
            with pytest.raises(LLMUnavailable) as caught:
                client.chat([{"role": "user", "content": "x"}])
            assert caught.value.status == expected
    finally:
        server.shutdown()
    # 401은 다시 부르지 않고, 일시 오류는 한 번만 다시 부른다
    assert len(seen) == {"ok": 2, 401: 1, 524: 2}[expected]


class _Encoded(dict):
    def to(self, device):
        return self


def _fake_local_stack(monkeypatch, loaded):
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt, enable_thinking):
            assert tokenize is False and add_generation_prompt is True and enable_thinking is False
            return "프롬프트"

        def __call__(self, texts, return_tensors):
            return _Encoded(input_ids=SimpleNamespace(shape=(1, 2)))

        def decode(self, tokens, skip_special_tokens):
            assert tokens == [3, 4] and skip_special_tokens is True
            return "<think>x</think>결과 문장"

    class Model:
        device = "cpu"

        def to(self, device):
            return self

        def eval(self):
            return self

        def generate(self, **kwargs):
            assert kwargs["do_sample"] is False and kwargs["max_new_tokens"] == 30
            return [[1, 2, 3, 4]]

    def tokenizer_loader(model, local_files_only):
        loaded["tokenizer_local_only"] = local_files_only
        return Tokenizer()

    def model_loader(model, dtype, local_files_only):
        loaded["model_local_only"] = local_files_only
        return Model()

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_loader),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_loader)))


def test_local_client_generates_without_downloading_by_default(monkeypatch):
    loaded = {}
    _fake_local_stack(monkeypatch, loaded)
    monkeypatch.delenv("LLM_ALLOW_DOWNLOAD", raising=False)
    assert LocalLLM("Qwen/Qwen2.5-3B-Instruct").chat([{"role": "user", "content": "x"}], max_tokens=30) == "결과 문장"
    assert loaded == {"tokenizer_local_only": True, "model_local_only": True}


def test_local_client_missing_packages(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(LLMUnavailable):
        LocalLLM("m").chat([{"role": "user", "content": "x"}])
