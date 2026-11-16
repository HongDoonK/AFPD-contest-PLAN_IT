import json

from spending_report.llm import LLMUnavailable
from spending_report.llm_eval import license_of, model_slug, reason_kind, run


class FakeServer:
    """초안을 그대로 돌려주거나(echo), 없는 금액을 지어내거나(invent), 연결이 안 되거나(down),
    키가 틀리거나(unauthorized), 모델을 올리는 동안 두 번 끊기는(slow_start) 가짜 서버."""
    provider = "server"

    def __init__(self, model, mode):
        self.model, self.mode = model, mode
        self.last_usage = None
        self.calls = 0

    def chat(self, messages, max_tokens=200):
        self.calls += 1
        if self.mode == "down":
            raise LLMUnavailable("연결 실패")
        if self.mode == "unauthorized":
            raise LLMUnavailable("LLM 서버 호출 실패: HTTP 401", status=401)
        if self.mode == "slow_start" and self.calls <= 2:
            raise LLMUnavailable("LLM 서버 호출 실패: HTTP 524", status=524)
        self.last_usage = {"completion_tokens": 20, "generation_ms": 400}
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "[초안]" not in user:
            return "네"
        if self.mode == "invent":
            return "월 999만원을 모을 수 있습니다."
        return user.split("[초안]\n", 1)[1].split("\n\n[요청]", 1)[0]


def _run(make_input, tmp_path, modes, warmup_timeout=0.3):
    inp = make_input("kim_doyun")
    servers = {}

    def make_client(model):
        servers[model] = FakeServer(model, modes[model])
        return servers[model]

    summary = run(list(modes), [inp], make_client, tmp_path / "eval", "test", warmup_timeout=warmup_timeout,
                  progress=lambda m: None)
    return summary, {m["model"]: m for m in summary["models"]}, servers


def test_grounded_model_is_fully_adopted(make_input, tmp_path):
    summary, by_model, servers = _run(make_input, tmp_path, {"org/echo": "echo"})
    echo = by_model["org/echo"]
    assert echo["sections"] > 1 and echo["llm"] == echo["sections"] == echo["first_pass"]
    assert echo["identical_retry"] == 0 and echo["reasons"] == {}
    assert echo["tokens_per_second"] == 50.0
    out = tmp_path / "eval"
    assert (out / "echo" / "spending_report_kim_doyun_v2.html").exists()
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["cases"] == ["kim_doyun_v2"]
    assert "| org/echo |" in (out / "summary.md").read_text(encoding="utf-8")


def test_invented_numbers_fall_back_and_are_counted(make_input, tmp_path):
    _, by_model, _ = _run(make_input, tmp_path, {"org/invent": "invent"})
    invent = by_model["org/invent"]
    assert invent["llm"] == 0 and invent["first_pass"] == 0
    assert invent["reasons"]["사실에 없는 금액"] >= invent["sections"]
    samples = (tmp_path / "eval" / "samples.md").read_text(encoding="utf-8")
    assert "거절: 사실에 없는 금액" in samples and "- 초안:" in samples


def test_unreachable_model_is_reported_not_fatal(make_input, tmp_path):
    _, by_model, _ = _run(make_input, tmp_path, {"org/down": "down", "org/echo": "echo"})
    assert by_model["org/down"]["error"] == "연결 실패" and by_model["org/down"]["sections"] == 0
    assert by_model["org/echo"]["llm"] == by_model["org/echo"]["sections"]
    assert "실행 실패" in (tmp_path / "eval" / "summary.md").read_text(encoding="utf-8")


def test_warm_up_waits_for_loading_but_not_for_wrong_key(make_input, tmp_path):
    _, by_model, servers = _run(make_input, tmp_path, {"org/slow": "slow_start", "org/key": "unauthorized"},
                                warmup_timeout=5)
    assert by_model["org/slow"]["error"] is None and by_model["org/slow"]["llm"] > 0
    assert servers["org/key"].calls == 1 and "401" in by_model["org/key"]["error"]


def test_gguf_builds_follow_the_base_model_license():
    assert license_of("unsloth/Qwen3-4B-Instruct-2507-Q4_K_M") == "Apache-2.0"
    assert license_of("unsloth/Qwen3-4B-Instruct-2507-UD-Q4_K_XL") == "Apache-2.0"
    assert license_of("Qwen/Qwen2.5-3B-Instruct") == "Qwen Research(비상업)"
    assert license_of("someone/Unknown-7B-Q4_K_M") == "확인 필요"


def test_helpers():
    assert model_slug("Qwen/Qwen2.5-3B-Instruct") == "Qwen2.5-3B-Instruct"
    assert reason_kind("사실에 없는 금액: 200,000원") == "사실에 없는 금액"
    assert reason_kind("길이 초과(450자)") == "길이 초과"
