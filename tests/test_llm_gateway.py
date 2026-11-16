"""AnthropicLLM·ServerLLM 호환 처리 시험. urllib.request.urlopen을 가짜로 바꾼다(네트워크 호출 없음)."""
import io
import json
import urllib.error
import urllib.request

import pytest

from planning.advisor import advise

from spending_report.llm import AnthropicLLM, LLMUnavailable, ServerLLM, get_client

KEY = "sk-test-SECRET-123"
MSGS = [{"role": "system", "content": "시스템 지시"}, {"role": "user", "content": "안녕"}]


class FakeUrlopen:
    """응답 목록을 차례로 돌려준다. 항목: (status, dict|str). status != 200이면 HTTPError."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append({"url": request.full_url, "headers": {k.lower(): v for k, v in request.header_items()},
                              "body": body, "timeout": timeout})
        status, payload = self.responses.pop(0)
        raw = (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(raw))
        return io.BytesIO(raw)


def ok(text_blocks, usage=None, thinking=True):
    content = ([{"type": "thinking", "thinking": "생각 중"}] if thinking else []) + \
              [{"type": "text", "text": t} for t in text_blocks]
    return 200, {"content": content, "usage": usage or {"input_tokens": 120, "output_tokens": 30}}


@pytest.fixture
def fake(monkeypatch):
    def install(responses):
        f = FakeUrlopen(responses)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return install


def client(**kw):
    return AnthropicLLM("claude-sonnet-5-5", "https://gw.example.test/v1/gateway/claude/", api_key=KEY,
                        retry_wait=0, **kw)


def test_request_format_and_text_blocks(fake):
    f = fake([ok(["다듬은 ", "문장입니다."])])
    c = client()
    assert c.chat(MSGS, max_tokens=150) == "다듬은 문장입니다."  # thinking 블록은 빼고 text만 이어 붙임
    req = f.requests[0]
    assert req["url"] == "https://gw.example.test/v1/gateway/claude/v1/messages"
    h = req["headers"]
    assert h["x-api-key"] == KEY and h["authorization"] == f"Bearer {KEY}"
    assert h["anthropic-version"] == "2023-06-01" and h["content-type"] == "application/json"
    b = req["body"]
    assert b["model"] == "claude-sonnet-5-5" and b["max_tokens"] == 150 and b["temperature"] == 0
    assert b["system"] == "시스템 지시"
    assert b["messages"] == [{"role": "user", "content": "안녕"}]  # system 역할은 messages에서 빠짐
    assert b["thinking"] == {"type": "disabled"}
    assert c.last_usage["input_tokens"] == 120 and c.last_usage["output_tokens"] == 30


def test_messages_path_from_env(fake, monkeypatch):
    f = fake([ok(["네."])])
    monkeypatch.setenv("LLM_MESSAGES_PATH", "/messages")
    monkeypatch.setenv("LLM_BASE_URL", "https://gw.example.test/v1/gateway/claude")
    monkeypatch.setenv("LLM_API_KEY", KEY)
    c = get_client("anthropic", "claude-sonnet-5-5")
    assert isinstance(c, AnthropicLLM) and c.provider == "anthropic"
    c.chat(MSGS)
    assert f.requests[0]["url"] == "https://gw.example.test/v1/gateway/claude/messages"


def test_anthropic_requires_base_url(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        get_client("anthropic", "claude-sonnet-5-5")


def test_thinking_rejected_then_retry_with_headroom(fake, monkeypatch):
    monkeypatch.setenv("LLM_THINKING_HEADROOM", "1500")
    f = fake([(400, {"error": {"message": "thinking: Extra inputs are not permitted"}}), ok(["좋습니다."])])
    assert client().chat(MSGS, max_tokens=100) == "좋습니다."
    first, second = f.requests[0]["body"], f.requests[1]["body"]
    assert "thinking" in first and "thinking" not in second
    assert second["max_tokens"] == 1600 and second["temperature"] == 0


def test_temperature_rejected_is_dropped(fake):
    f = fake([(400, {"error": {"message": "temperature is not supported with thinking"}}), ok(["네."])])
    assert client(thinking_headroom=2000).chat(MSGS, max_tokens=100) == "네."
    second = f.requests[1]["body"]
    assert "temperature" not in second and "thinking" not in second and second["max_tokens"] == 2100


def test_temperature_rejected_after_thinking_removed(fake):
    f = fake([(400, "bad thinking"), (400, '{"error":"temperature must be 1"}'), ok(["네."])])
    assert client(thinking_headroom=10).chat(MSGS, max_tokens=100) == "네."
    assert "temperature" not in f.requests[2]["body"] and len(f.requests) == 3


def test_only_thinking_blocks_is_empty_response(fake):
    fake([ok([], thinking=True)])
    with pytest.raises(LLMUnavailable, match="빈 응답"):
        client().chat(MSGS)


def test_error_message_has_no_key_and_short_body(fake):
    long_body = json.dumps({"error": f"invalid key {KEY} " + "x" * 500})
    f = fake([(401, long_body)])
    with pytest.raises(LLMUnavailable) as caught:
        client().chat(MSGS)
    msg = str(caught.value)
    assert KEY not in msg and caught.value.status == 401 and "***" in msg
    assert len(msg) < 260  # 본문은 앞 200자만
    assert len(f.requests) == 1  # 401은 다시 부르지 않음


def test_transient_error_retried_once(fake):
    f = fake([(503, "busy"), ok(["네."])])
    assert client().chat(MSGS) == "네." and len(f.requests) == 2


def test_server_max_completion_tokens_retry(fake):
    f = fake([(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}}),
              (200, {"choices": [{"message": {"content": "네."}}]})])
    c = ServerLLM("gpt-x", "https://srv.example.test/v1", api_key=KEY, retry_wait=0)
    assert c.chat(MSGS, max_tokens=80) == "네."
    second = f.requests[1]["body"]
    assert "max_tokens" not in second and second["max_completion_tokens"] == 80
    assert f.requests[0]["url"] == "https://srv.example.test/v1/chat/completions"


def test_server_temperature_retry_and_no_retry_for_other_400(fake):
    f = fake([(400, "temperature does not support 0"), (200, {"choices": [{"message": {"content": "네."}}]})])
    c = ServerLLM("m", "https://srv.example.test/v1", retry_wait=0)
    assert c.chat(MSGS) == "네."
    assert "temperature" not in f.requests[1]["body"] and "seed" not in f.requests[1]["body"]
    fake([(400, "bad request")])
    with pytest.raises(LLMUnavailable):
        c.chat(MSGS)


def test_advisor_records_tokens_with_anthropic(fake):
    strategy = {"cards": [{"id": "S1", "term": "단기", "goal": None, "title": "S1 검토", "action": "비상금은 600만원입니다.",
                           "numbers": {"목표(만원)": 600.0}, "basis": "", "evidence": [], "confirm": [],
                           "certainty": "계산값", "priority": 1}],
                "summary": {"M": 1.0, "locked": 0.0, "debt": 0.0, "surplus_after": 1.0, "monthly_need": 1.0,
                            "monthly_shortfall": 0.0}}
    responses = [ok(['{"order": ["S1"], "focus": "유동성부터 봅니다."}'])] + [ok(["엉뚱한 999만원입니다."])] * 6
    fake(responses)
    log = []
    out = advise(strategy, client(), log=log)
    assert out["provider"] == "anthropic" and out["calls"] >= 2
    assert out["totals"]["input_tokens"] == 120 * out["calls"]
    assert all(s["output_tokens"] == 30 and s["latency_ms"] is not None for s in out["call_stats"])


class EchoGateway:
    """Messages 형식 가짜 게이트웨이: [초안]을 그대로 돌려준다(첫 블록은 thinking)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, request, timeout=None):
        import re
        self.calls += 1
        body = json.loads(request.data.decode("utf-8"))
        text = body["messages"][0]["content"]
        m = re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", text, re.S)
        if "[카드]" in text:
            ids = re.findall(r'"id": "([^"]+)"', text)
            answer = json.dumps({"order": ids, "focus": "유동성부터 봅니다."}, ensure_ascii=False)
        else:
            answer = m.group(1) if m else "네."
        return io.BytesIO(json.dumps({"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": answer}],
                                      "usage": {"input_tokens": 50, "output_tokens": 10}}).encode("utf-8"))


def test_llm_eval_runs_one_case_with_anthropic(monkeypatch, tmp_path):
    from pathlib import Path
    import sys
    from spending_report import llm_eval
    db = Path(__file__).resolve().parents[1] / "data" / "customer_profiles.db"
    if not db.exists():
        pytest.skip("고객 프로필 DB 없음")
    gw = EchoGateway()
    monkeypatch.setattr(urllib.request, "urlopen", gw)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setattr(sys, "argv", ["llm_eval", "--provider", "anthropic", "--models", "claude-sonnet-5-5",
                                      "--cases", "kim_doyun", "--label", "fake", "--base-url", "https://gw.example.test/x",
                                      "--db", str(db), "--out", str(tmp_path), "--warmup-timeout", "5"])
    llm_eval.main()
    summary = json.loads((tmp_path / "fake" / "summary.json").read_text(encoding="utf-8"))
    m = summary["models"][0]
    assert summary["cases"] == ["kim_doyun_v2"] and m["error"] is None and m["llm"] >= 1
    assert m["max_context_tokens"] == 60
    assert (tmp_path / "fake" / "summary.md").exists() and gw.calls > 1
    assert KEY not in (tmp_path / "fake" / "summary.json").read_text(encoding="utf-8")


def test_pipeline_llm_suffix_writes_separate_files(monkeypatch, tmp_path):
    from planning.pipeline import POLICY_DB, ROOT, main
    if not POLICY_DB.exists():
        pytest.skip("정책 DB 없음")
    gw = EchoGateway()
    monkeypatch.setattr(urllib.request, "urlopen", gw)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    args = ["--case", "kim_doyun", "--step2", str(ROOT / "planning" / "cases" / "kim_doyun_step2.json"),
            "--out-dir", str(tmp_path), "--llm-provider", "anthropic", "--llm-model", "claude-sonnet-5-5",
            "--llm-base-url", "https://gw.example.test/x", "--suffix", "_llm"]
    assert main(args) == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["ai_log_plan_kim_doyun_certain_v1_llm.json", "dashboard_kim_doyun_certain_v1_llm.html",
                     "plan_kim_doyun_certain_v1_llm.json"]
    plan = json.loads((tmp_path / names[2]).read_text(encoding="utf-8"))
    log = json.loads((tmp_path / names[0]).read_text(encoding="utf-8"))
    assert plan["advice"]["provider"] == "anthropic" and plan["advice"]["accepted"] >= 1
    assert plan["advice"]["totals"]["input_tokens"] == 50 * plan["advice"]["calls"]
    assert log["calls"] and all(c["input_tokens"] == 50 and c["latency_ms"] is not None for c in log["calls"])
    assert {c["stage"] for c in log["calls"]} == {"소비 보고서", "에이전트"}
    for p in tmp_path.iterdir():
        assert KEY not in p.read_text(encoding="utf-8")
    assert main(args[:-2] + ["--suffix", "../x"]) == 2


def _openai(content, usage=None):
    return 200, {"choices": [{"message": {"content": content}}], "usage": usage or {}}


def test_server_body_unchanged_without_reasoning_env(fake, monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("LLM_REASONING_HEADROOM", raising=False)
    f = fake([_openai("네.")])
    ServerLLM("Qwen/Qwen3-4B-Instruct-2507", "https://srv.example.test/v1", retry_wait=0).chat(MSGS, max_tokens=120)
    body = f.requests[0]["body"]
    assert body == {"model": "Qwen/Qwen3-4B-Instruct-2507", "messages": MSGS, "max_tokens": 120,
                    "temperature": 0, "seed": 42}
    assert list(body) == ["model", "messages", "max_tokens", "temperature", "seed"]


def test_server_reasoning_effort_and_headroom(fake, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("LLM_REASONING_HEADROOM", "500")
    f = fake([_openai("네.", {"prompt_tokens": 40, "completion_tokens": 520})])
    c = ServerLLM("gpt-6-luna", "https://gw.example.test/v1", retry_wait=0)
    assert c.chat(MSGS, max_tokens=120) == "네."
    body = f.requests[0]["body"]
    assert "max_tokens" not in body and body["max_completion_tokens"] == 620 and body["reasoning_effort"] == "low"
    assert c.last_usage["completion_tokens"] == 520


def test_server_headroom_only_adds_to_max_tokens(fake, monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("LLM_REASONING_HEADROOM", "50")
    f = fake([_openai("네.")])
    ServerLLM("m", "https://srv.example.test/v1", retry_wait=0).chat(MSGS, max_tokens=100)
    assert f.requests[0]["body"]["max_tokens"] == 150 and "reasoning_effort" not in f.requests[0]["body"]


@pytest.mark.parametrize("content", ["", None, "<think>추론만</think>"])
def test_server_empty_content_is_unavailable(fake, monkeypatch, content):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    fake([_openai(content)])
    with pytest.raises(LLMUnavailable, match="빈 응답"):
        ServerLLM("gpt-6-luna", "https://gw.example.test/v1", retry_wait=0).chat(MSGS)


def test_llm_eval_token_totals_both_formats():
    from spending_report.llm_eval import aggregate, case_stats, summary_markdown
    log = [{"problems": [], "latency_ms": 10}, {"problems": [], "latency_ms": 20}]
    rows = [{"source": "llm", "attempts": [{"problems": [], "raw": "x"}]}]
    openai_stats = case_stats(rows, log, [{"prompt_tokens": 100, "completion_tokens": 10},
                                         {"prompt_tokens": 50, "completion_tokens": 5}])
    anthropic_stats = case_stats(rows, log, [{"input_tokens": 70, "output_tokens": 7, "prompt_tokens": 70,
                                            "completion_tokens": 7}, None])
    a = aggregate("gpt-6-luna", {"kim_doyun_v2": openai_stats}, None, None)
    b = aggregate("claude-sonnet-5-5", {"kim_doyun_v2": anthropic_stats}, None, None)
    assert (a["input_tokens_total"], a["output_tokens_total"]) == (150, 15)
    assert (b["input_tokens_total"], b["output_tokens_total"]) == (70, 7)  # 같은 값을 두 번 세지 않음
    empty = aggregate("m", {"x": case_stats([], log, [None, None])}, None, None)
    assert empty["input_tokens_total"] is None
    md = summary_markdown({"label": "t", "generated_at": "x", "cases": ["kim_doyun_v2"], "models": [a, b]})
    assert "## 토큰 합계" in md and "| 150 | 15 |" in md and "| 70 | 7 |" in md
    assert "| 모델 | 라이선스 | 채택 |" in md  # 기존 표 머리 그대로
