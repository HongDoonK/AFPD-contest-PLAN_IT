"""주 모델 → 보조 모델 → 템플릿 대체(FallbackLLM) 시험. 가짜 클라이언트와 가짜 urlopen만 쓴다."""
import io
import json
import re
import urllib.error
import urllib.request

import pytest

from planning.advisor import advise
from spending_report.llm import (EXHAUSTION_STATUS, EXHAUSTION_WORDS, FallbackLLM, LLMUnavailable, NoLLM, ServerLLM,
                                 get_client, is_exhaustion)

KEY = "sk-primary-SECRET"
FB_KEY = "sk-fallback-SECRET"
MSGS = [{"role": "system", "content": "지시"}, {"role": "user", "content": "[초안]\n좋습니다.\n\n[요청]\n다듬기"}]


class Scripted:
    """응답 목록을 차례로 쓴다. 항목이 예외면 던진다."""

    def __init__(self, provider, model, script):
        self.provider, self.model = provider, model
        self.script = list(script)
        self.calls = 0
        self.last_usage = None

    def chat(self, messages, max_tokens=200):
        self.calls += 1
        item = self.script.pop(0) if self.script else "기본 답입니다."
        if isinstance(item, Exception):
            raise item
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 2, "who": self.model}
        return item


def err(status=None, body=""):
    e = LLMUnavailable(f"LLM 서버 호출 실패: HTTP {status} {body}" if status else "LLM 서버 호출 실패: timed out",
                       status=status)
    e.body = body
    return e


def test_exhaustion_rule_constants():
    assert EXHAUSTION_STATUS == {401, 402, 403, 429}
    assert "quota" in EXHAUSTION_WORDS and "잔액" in EXHAUSTION_WORDS
    assert is_exhaustion(err(402)) and is_exhaustion(err(429))
    assert is_exhaustion(err(400, '{"error":{"code":"insufficient_quota"}}'))
    assert is_exhaustion(err(500, "토큰 한도 소진"))
    assert not is_exhaustion(err(503, "busy")) and not is_exhaustion(err(None)) and not is_exhaustion(LLMUnavailable("빈 응답"))


def test_normal_uses_primary_only():
    p, s = Scripted("server", "gpt-6-luna", ["가."] * 3), Scripted("server", "qwen", [])
    fb = FallbackLLM(p, s)
    for _ in range(3):
        assert fb.chat(MSGS) == "가."
    assert p.calls == 3 and s.calls == 0 and not fb.switched
    assert fb.last_role == "primary" and fb.last_model == "gpt-6-luna" and fb.last_usage["who"] == "gpt-6-luna"


@pytest.mark.parametrize("exc", [err(402, "payment required"), err(429, '{"error":"quota exceeded"}'),
                                 err(400, "monthly credit balance is too low")])
def test_exhaustion_switches_permanently(exc):
    p = Scripted("server", "gpt-6-luna", [exc, "주 모델 답"])
    s = Scripted("server", "qwen", ["보조 1.", "보조 2.", "보조 3."])
    fb = FallbackLLM(p, s)
    assert fb.chat(MSGS) == "보조 1."
    assert fb.switched and fb.switched_at_call == 1 and "소진형" in fb.switch_reason
    assert fb.chat(MSGS) == "보조 2." and fb.chat(MSGS) == "보조 3."
    assert p.calls == 1  # 전환 뒤로 주 모델을 다시 부르지 않음
    assert fb.provider == "server" and fb.model == "qwen" and fb.last_role == "secondary"
    assert fb.last_usage["who"] == "qwen"
    assert fb.summary()["secondary_calls"] == 3


def test_transient_error_falls_back_for_that_call_only():
    p = Scripted("server", "gpt-6-luna", [err(503, "busy"), "주 2.", LLMUnavailable("빈 응답"), "주 4."])
    s = Scripted("server", "qwen", ["보조 1.", "보조 3."])
    fb = FallbackLLM(p, s)
    assert [fb.chat(MSGS) for _ in range(4)] == ["보조 1.", "주 2.", "보조 3.", "주 4."]
    assert not fb.switched and p.calls == 4 and s.calls == 2


def test_three_consecutive_failures_switch():
    p = Scripted("server", "gpt-6-luna", [err(None), err(500, "x"), LLMUnavailable("빈 응답"), "주 모델"])
    s = Scripted("server", "qwen", ["a.", "b.", "c.", "d."])
    fb = FallbackLLM(p, s)
    assert [fb.chat(MSGS) for _ in range(4)] == ["a.", "b.", "c.", "d."]
    assert fb.switched and fb.switched_at_call == 3 and "연속 3회" in fb.switch_reason
    assert p.calls == 3


def test_both_fail_raises_unavailable_without_keys():
    p = Scripted("server", "gpt-6-luna", [err(402, "quota")])
    s = Scripted("server", "qwen", [err(None)])
    fb = FallbackLLM(p, s)
    with pytest.raises(LLMUnavailable) as caught:
        fb.chat(MSGS)
    msg = str(caught.value)
    assert "주 모델" in msg and "보조 모델" in msg and KEY not in msg
    assert fb.events[-1]["event"] == "fail"


def test_both_fail_makes_advisor_use_templates():
    strategy = {"cards": [{"id": "S1", "term": "단기", "goal": None, "title": "S1 검토", "action": "비상금은 600만원입니다.",
                           "numbers": {"목표(만원)": 600.0}, "basis": "", "evidence": [], "confirm": [],
                           "certainty": "계산값", "priority": 1}],
                "summary": {"M": 1.0, "locked": 0.0, "debt": 0.0, "surplus_after": 1.0, "monthly_need": 1.0,
                            "monthly_shortfall": 0.0}}
    fb = FallbackLLM(Scripted("server", "gpt-6-luna", [err(402)] * 9), Scripted("server", "qwen", [err(None)] * 9))
    out = advise(strategy, fb)
    assert out["calls"] == 1  # 보조까지 실패한 뒤로는 부르지 않는다(남은 문장은 템플릿)
    assert all(t["source"] == "template" for t in out["terms"].values()) and out["summary"]["source"] == "template"


def test_advisor_log_records_actual_model():
    strategy = {"cards": [{"id": "S1", "term": "단기", "goal": None, "title": "S1 검토", "action": "비상금은 600만원입니다.",
                           "numbers": {"목표(만원)": 600.0}, "basis": "", "evidence": [], "confirm": [],
                           "certainty": "계산값", "priority": 1}],
                "summary": {"M": 1.0, "locked": 0.0, "debt": 0.0, "surplus_after": 1.0, "monthly_need": 1.0,
                            "monthly_shortfall": 0.0}}

    class Echo(Scripted):
        def chat(self, messages, max_tokens=200):
            self.calls += 1
            text = messages[1]["content"]
            if self.model == "gpt-6-luna" and self.calls == 1:
                raise err(429, "rate limit exceeded")
            if "[카드]" in text:
                return json.dumps({"order": ["S1"], "focus": "유동성부터 봅니다."}, ensure_ascii=False)
            return re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", text, re.S).group(1)
    log = []
    out = advise(strategy, FallbackLLM(Echo("server", "gpt-6-luna", []), Echo("server", "qwen", [])), log=log)
    assert {e["model"] for e in log} == {"qwen"} and {e["role"] for e in log} == {"secondary"}
    assert all(s["model"] == "qwen" for s in out["call_stats"])


# ── get_client와 실제 요청 본문 ─────────────────────────────────
class FakeUrlopen:
    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        req = {"url": request.full_url, "headers": {k.lower(): v for k, v in request.header_items()}, "body": body}
        self.requests.append(req)
        status, payload = self.handler(req)
        raw = json.dumps(payload).encode("utf-8")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(raw))
        return io.BytesIO(raw)


def _gateway_then_qwen(req):
    if "gateway" in req["url"]:
        return 402, {"error": {"message": "insufficient_quota", "key": KEY}}
    return 200, {"choices": [{"message": {"content": "큐웬 답입니다."}}]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    monkeypatch.setenv("LLM_REASONING_HEADROOM", "300")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", FB_KEY)
    for k in ("LLM_FALLBACK_PROVIDER", "LLM_FALLBACK_MODEL", "LLM_FALLBACK_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_get_client_without_fallback_is_unchanged(env):
    c = get_client("server", "gpt-6-luna", "https://gw.example.test/gateway/v1")
    assert isinstance(c, ServerLLM)
    assert isinstance(get_client("none"), NoLLM)
    env.setenv("LLM_FALLBACK_PROVIDER", "server")
    assert isinstance(get_client("none"), NoLLM)  # 주 공급자 none이면 감싸지 않음


def test_get_client_fallback_request_bodies(env):
    env.setenv("LLM_FALLBACK_PROVIDER", "server")
    fake = FakeUrlopen(_gateway_then_qwen)
    env.setattr(urllib.request, "urlopen", fake)
    c = get_client("server", "gpt-6-luna", "https://gw.example.test/gateway/v1")
    assert isinstance(c, FallbackLLM) and c.secondary.model == "Qwen/Qwen3-4B-Instruct-2507"
    assert c.chat(MSGS, max_tokens=120) == "큐웬 답입니다."
    assert c.chat(MSGS, max_tokens=120) == "큐웬 답입니다."
    gw = [r for r in fake.requests if "gateway" in r["url"]]
    qwen = [r for r in fake.requests if "gateway" not in r["url"]]
    assert len(gw) == 1 and len(qwen) == 2  # 402 소진 → 영구 전환
    assert gw[0]["body"]["reasoning_effort"] == "none" and gw[0]["body"]["max_completion_tokens"] == 420
    # 보조(Qwen) 요청 본문은 예전 형식 그대로: 추론 설정 없음
    assert qwen[0]["url"] == "http://localhost:8080/v1/chat/completions"
    assert qwen[0]["body"] == {"model": "Qwen/Qwen3-4B-Instruct-2507", "messages": MSGS, "max_tokens": 120,
                               "temperature": 0, "seed": 42}
    assert qwen[0]["headers"]["authorization"] == f"Bearer {FB_KEY}"
    assert KEY not in json.dumps(c.summary(), ensure_ascii=False)


def test_fallback_settings_from_args(env):
    c = get_client("server", "gpt-6-luna", "https://gw.example.test/gateway/v1", fallback_provider="server",
                   fallback_model="Qwen/Qwen3-1.7B", fallback_base_url="http://127.0.0.1:9931/v1")
    assert isinstance(c, FallbackLLM)
    assert c.secondary.model == "Qwen/Qwen3-1.7B" and c.secondary.base_url == "http://127.0.0.1:9931/v1"
    assert c.secondary.reasoning_effort == "" and c.secondary.reasoning_headroom == 0


def test_pipeline_records_fallback(env, tmp_path):
    from planning.pipeline import POLICY_DB, ROOT, main
    if not POLICY_DB.exists():
        pytest.skip("정책 DB 없음")

    def handler(req):
        if "gateway" in req["url"]:
            return 402, {"error": {"message": "insufficient_quota"}}
        text = req["body"]["messages"][-1]["content"]
        if "[카드]" in text:
            ids = re.findall(r'"id": "([^"]+)"', text)
            answer = json.dumps({"order": ids, "focus": "유동성부터 봅니다."}, ensure_ascii=False)
        else:
            m = re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", text, re.S)
            answer = m.group(1) if m else "네."
        return 200, {"choices": [{"message": {"content": answer}}], "usage": {"prompt_tokens": 40, "completion_tokens": 9}}
    env.setattr(urllib.request, "urlopen", FakeUrlopen(handler))
    args = ["--case", "kim_doyun", "--step2", str(ROOT / "planning" / "cases" / "kim_doyun_step2.json"),
            "--out-dir", str(tmp_path), "--format", "html", "--llm-provider", "server", "--llm-model", "gpt-6-luna",
            "--llm-base-url", "https://gw.example.test/gateway/v1", "--llm-fallback-provider", "server", "--suffix", "_fb"]
    assert main(args) == 0
    plan = json.loads((tmp_path / "plan_kim_doyun_certain_v1_fb.json").read_text(encoding="utf-8"))
    log = json.loads((tmp_path / "ai_log_plan_kim_doyun_certain_v1_fb.json").read_text(encoding="utf-8"))
    fb = plan["advice"]["fallback"]
    assert fb["enabled"] and fb["switched"] and fb["switched_at_call"] == 1 and "소진형" in fb["switch_reason"]
    src = plan["advice"]["sentence_sources"]
    assert src["primary"] == 0 and src["secondary"] > 0 and sum(src.values()) > 0
    assert all(c["model"] == "Qwen/Qwen3-4B-Instruct-2507" for c in log["calls"] if c["ok"])
    assert log["fallback"]["switched"] is True
    page = (tmp_path / "dashboard_kim_doyun_certain_v1_fb.html").read_text(encoding="utf-8")
    assert f"대체 모델 {src['secondary']}문장" in page and "번째 호출부터 대체 모델만 사용" in page
    for p in tmp_path.iterdir():
        text = p.read_text(encoding="utf-8")
        assert KEY not in text and FB_KEY not in text


def test_llm_eval_counts_fallback_calls():
    from spending_report.llm_eval import RecordingClient, aggregate, case_stats, summary_markdown
    fb = FallbackLLM(Scripted("server", "gpt-6-luna", [err(402), "x"]), Scripted("server", "qwen", ["a.", "b.", "c."]))
    rec = RecordingClient(fb)
    for _ in range(3):
        rec.chat(MSGS)
    assert rec.roles == ["secondary"] * 3
    rows = [{"source": "llm", "attempts": [{"problems": [], "raw": "a."}]}]
    stats = case_stats(rows, [{"problems": []}] * 3, rec.usage, rec.roles)
    m = aggregate("gpt-6-luna", {"kim_doyun_v2": stats}, None, None)
    assert m["fallback_calls"] == 3 and m["calls"] == 3
    md = summary_markdown({"label": "t", "generated_at": "x", "cases": ["kim_doyun_v2"], "models": [m]})
    assert "## 대체 모델 호출" in md and "| 3 | 3 |" in md
