"""교체 가능한 LLM 클라이언트. 보고서에서 LLM은 초안 문장을 다듬는 언어 계층이다.

공급자는 환경변수 LLM_PROVIDER로 고른다.
- none   : 기본값. 호출하지 않고 템플릿 문장을 쓴다.
- local  : transformers로 직접 추론. 모델은 미리 받아 두어야 한다(LLM_ALLOW_DOWNLOAD=1이면 자동 다운로드).
- server : OpenAI 호환 서버(llama.cpp, vLLM, Ollama 등). LLM_BASE_URL로 주소,
           LLM_API_KEY로 키(Bearer), LLM_TIMEOUT으로 제한 시간(초)을 준다.
- anthropic : Anthropic Messages 형식(학교 게이트웨이 등). LLM_BASE_URL + LLM_MESSAGES_PATH(기본 /v1/messages).
           LLM_API_KEY는 x-api-key와 Bearer 두 헤더로 보낸다. 모델은 LLM_MODEL(예: claude-sonnet-5-5).

모델은 LLM_MODEL로 바꾼다. 실패하면 LLMUnavailable을 던지고 호출 측이 템플릿으로 되돌린다.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "Qwen/Qwen3-4B-Instruct-2507"  # Apache-2.0. 고른 근거는 .scratch/spending-report/issues/02
DEFAULT_BASE_URL = "http://localhost:8080/v1"


class LLMUnavailable(RuntimeError):
    """LLM을 쓸 수 없을 때. 서버가 HTTP 오류를 돌려주면 status에 코드를 둔다."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# 터널·프록시를 거칠 때 잠깐 생기는 오류. 한 번 더 부른다
TRANSIENT_STATUS = {429, 502, 503, 504, 520, 521, 522, 523, 524}


def _clean(text: str) -> str:
    # Qwen3 계열의 추론 블록은 보고서에 넣지 않는다
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


class NoLLM:
    provider, model = "none", None

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        raise LLMUnavailable("LLM_PROVIDER=none")


class LocalLLM:
    provider = "local"

    def __init__(self, model: str):
        self.model = model
        self._tokenizer = None
        self._model = None

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ModuleNotFoundError as exc:
            raise LLMUnavailable(f"로컬 추론 패키지 없음: {exc.name}") from exc
        local_only = os.getenv("LLM_ALLOW_DOWNLOAD") != "1"
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(self.model, local_files_only=local_only)
            model = AutoModelForCausalLM.from_pretrained(self.model, dtype="auto", local_files_only=local_only)
            self._model = model.to("cuda" if torch.cuda.is_available() else "cpu").eval()
        except (OSError, RuntimeError, ValueError) as exc:
            raise LLMUnavailable(f"모델 로드 실패: {exc}") from exc

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        self._load()
        prompt = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self._tokenizer([prompt], return_tensors="pt").to(self._model.device)
        output = self._model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        generated = output[0][inputs["input_ids"].shape[1]:]
        return _clean(self._tokenizer.decode(generated, skip_special_tokens=True))


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def post_json(url: str, body: dict, headers: dict, *, timeout: float, retries: int, retry_wait: float,
              secret: str | None = None) -> dict:
    """JSON POST. 일시 오류(TRANSIENT_STATUS·연결 오류)는 retries번 다시 부른다. 실패는 LLMUnavailable.
    HTTP 오류면 응답 본문 앞 200자를 메시지와 exc.body에 남긴다. 메시지에 키(secret)는 넣지 않는다."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    error = LLMUnavailable("LLM 서버 호출 실패")
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = _redact(exc.read().decode("utf-8", "replace")[:200], secret)
            except Exception:  # noqa: BLE001 - 본문을 못 읽어도 상태 코드는 남긴다
                detail = ""
            error = LLMUnavailable(f"LLM 서버 호출 실패: HTTP {exc.code}" + (f" {detail}" if detail else ""),
                                   status=exc.code)
            error.body = detail
            if exc.code not in TRANSIENT_STATUS:
                raise error from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            error = LLMUnavailable(_redact(f"LLM 서버 호출 실패: {exc}", secret))
        except (TimeoutError, ValueError) as exc:
            raise LLMUnavailable(_redact(f"LLM 서버 호출 실패: {exc}", secret)) from exc
        if attempt < retries:
            time.sleep(retry_wait)
    raise error


def _compat_retry(send, body: dict, fixes) -> dict:
    """HTTP 400 본문에 특정 매개변수가 언급되면 고친 본문으로 한 번만 다시 부른다. fixes: [(낱말, 본문 → 고친 본문)]."""
    try:
        return send(body)
    except LLMUnavailable as exc:
        detail = getattr(exc, "body", "") or ""
        if exc.status != 400:
            raise
        fixed = body
        for word, fix in fixes:
            if word in detail:
                fixed = fix(fixed)
        if fixed is body:
            raise
        return send(fixed)


class ServerLLM:
    provider = "server"

    def __init__(self, model: str, base_url: str, timeout: float = 120.0, api_key: str | None = None,
                 retries: int = 1, retry_wait: float = 2.0, reasoning_effort: str | None = None,
                 reasoning_headroom: int | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.retries, self.retry_wait = retries, retry_wait
        self.last_usage: dict | None = None  # 서버가 돌려준 토큰 수·생성 시간(실험 기록용)
        self.last_timings: dict | None = None  # llama.cpp 서버가 잰 프롬프트 처리·생성 시간(실험 기록용)
        # 환경변수가 없으면 요청 본문은 예전과 같다(Qwen·llama.cpp 경로)
        self.reasoning_effort = (reasoning_effort if reasoning_effort is not None
                                 else os.getenv("LLM_REASONING_EFFORT") or None)
        self.reasoning_headroom = (reasoning_headroom if reasoning_headroom is not None
                                   else int(os.getenv("LLM_REASONING_HEADROOM") or 0))

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        limit =max_tokens + self.reasoning_headroom
        body = {"model": self.model, "messages": messages, "max_tokens": limit, "temperature": 0, "seed": 42}
        if self.reasoning_effort:
            # 추론 모델(예: gpt-6-luna)은 추론 토큰이 한도를 먹어 빈 답을 낸다: 노력 수준을 낮추고 completion 한도를 쓴다
            body = {"model": self.model, "messages": messages, "max_completion_tokens": limit,
                    "reasoning_effort": self.reasoning_effort, "temperature": 0, "seed": 42}
        max_tokens = limit
        headers = {"Content-Type": "application/json", "User-Agent": "afpk-spending-report/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.last_usage = self.last_timings = None

        def send(b):
            return post_json(f"{self.base_url}/chat/completions", b, headers, timeout=self.timeout,
                             retries=self.retries, retry_wait=self.retry_wait, secret=self.api_key)

        def completion_tokens(b):
            out = {k: v for k, v in b.items() if k != "max_tokens"}
            out["max_completion_tokens"] = max_tokens
            return out

        def no_sampling(b):
            return {k: v for k, v in b.items() if k not in ("temperature", "seed")}
        # 일부 OpenAI 호환 서버는 max_tokens 대신 max_completion_tokens만, 또는 기본 temperature만 받는다
        data = _compat_retry(send, body, [("max_tokens", completion_tokens), ("temperature", no_sampling)])
        self.last_usage, self.last_timings = data.get("usage"), data.get("timings")
        try:
            text = _clean(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"LLM 서버 응답 형식 오류: {exc}") from exc
        if not text:
            raise LLMUnavailable("빈 응답")
        return text


DEFAULT_MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicLLM:
    """Anthropic Messages 형식(학교 게이트웨이 등). system 역할 메시지는 본문 system으로 뺀다."""
    provider = "anthropic"

    def __init__(self, model: str, base_url: str, timeout: float = 120.0, api_key: str | None = None,
                 messages_path: str = DEFAULT_MESSAGES_PATH, retries: int = 1, retry_wait: float = 2.0,
                 thinking_headroom: int | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.messages_path = "/" + messages_path.lstrip("/")
        self.thinking_headroom = thinking_headroom if thinking_headroom is not None else \
            int(os.getenv("LLM_THINKING_HEADROOM") or 2000)
        self.timeout = timeout
        self.api_key = api_key
        self.retries, self.retry_wait = retries, retry_wait
        self.last_usage: dict | None = None
        self.last_timings: dict | None = None

    @staticmethod
    def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        return system, [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        system, rest = self.split_system(messages)
        # 게이트웨이는 생각(thinking)이 기본으로 켜져 있어 max_tokens를 먹는다. 먼저 끄도록 요청한다
        body = {"model": self.model, "max_tokens": max_tokens, "messages": rest, "temperature": 0,
                "thinking": {"type": "disabled"}}
        if system:
            body["system"] = system
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION,
                   "User-Agent": "afpk-spending-report/1.0"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.last_usage = self.last_timings = None

        def send(b):
            return post_json(f"{self.base_url}{self.messages_path}", b, headers, timeout=self.timeout,
                             retries=self.retries, retry_wait=self.retry_wait, secret=self.api_key)

        data = None
        for _ in range(3):
            try:
                data = send(body)
                break
            except LLMUnavailable as exc:
                if exc.status != 400:
                    raise
                detail = getattr(exc, "body", "") or ""
                fixed = dict(body)
                if "thinking" in fixed:
                    # thinking 필드를 받지 않는 게이트웨이: 빼고, 생각이 쓸 몫만큼 max_tokens에 여유를 둔다
                    fixed.pop("thinking")
                    fixed["max_tokens"] = max_tokens + self.thinking_headroom
                if "temperature" in fixed and "temperature" in detail:
                    fixed.pop("temperature")
                if fixed == body:
                    raise
                body = fixed
        usage = data.get("usage") or {}
        if usage:
            # llm_eval 집계 키(prompt_tokens·completion_tokens)도 함께 둔다
            self.last_usage = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                               "prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMUnavailable(f"LLM 서버 응답 형식 오류: {exc}") from exc
        if not any(isinstance(b, dict) and b.get("type") == "text" for b in data["content"]):
            raise LLMUnavailable("빈 응답")
        return _clean(text)


# ── 주 모델 → 보조 모델 → 템플릿 대체 ─────────────────────────────
# 소진형 오류: 이 상태 코드이거나 오류 본문에 아래 낱말이 있으면 주 모델을 다시 부르지 않는다(영구 전환).
# 429는 post_json이 일시 오류로 한 번 다시 부른 뒤에도 실패한 경우에만 여기까지 온다.
EXHAUSTION_STATUS = frozenset({401, 402, 403, 429})
EXHAUSTION_WORDS = ("quota", "insufficient", "credit", "balance", "exceeded", "limit", "한도", "소진", "잔액")
MAX_PRIMARY_FAILURES = 3  # 소진형이 아닌 오류가 이만큼 이어지면 영구 전환
DEFAULT_FALLBACK_MODEL = DEFAULT_MODEL
DEFAULT_FALLBACK_BASE_URL = DEFAULT_BASE_URL


def is_exhaustion(exc: Exception) -> bool:
    """토큰·크레딧 소진이나 권한 문제처럼 다시 불러도 나아지지 않을 오류인지."""
    if getattr(exc, "status", None) in EXHAUSTION_STATUS:
        return True
    body = getattr(exc, "body", "") or ""
    text = f"{body} {exc}".lower()
    return any(word in text for word in EXHAUSTION_WORDS)


class FallbackLLM:
    """주 모델이 실패하면 보조 모델로 넘긴다. 소진형 오류나 연속 실패면 그 뒤로 보조 모델만 쓴다.
    보조 모델도 실패하면 LLMUnavailable을 던져 호출 측이 템플릿으로 되돌리게 한다."""

    def __init__(self, primary, secondary, max_failures: int = MAX_PRIMARY_FAILURES):
        self.primary, self.secondary = primary, secondary
        self.max_failures = max_failures
        self.switched = False
        self.switch_reason: str | None = None
        self.switched_at_call: int | None = None
        self.consecutive_failures = 0
        self.calls = 0
        self.events: list[dict] = []
        self.last_role: str | None = None
        self.last_provider: str | None = None
        self.last_model: str | None = None
        self.last_usage: dict | None = None
        self.last_timings: dict | None = None

    def _current(self):
        return self.secondary if self.switched else self.primary

    @property
    def provider(self):
        return self.last_provider or self._current().provider

    @property
    def model(self):
        return self.last_model or self._current().model

    def _answered(self, role: str, client) -> None:
        self.last_role, self.last_provider, self.last_model = role, client.provider, client.model
        self.last_usage = getattr(client, "last_usage", None)
        self.last_timings = getattr(client, "last_timings", None)

    def _switch(self, reason: str) -> None:
        if not self.switched:
            self.switched, self.switch_reason, self.switched_at_call = True, reason, self.calls
            self.events.append({"call": self.calls, "event": "switch", "reason": reason})

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        self.calls += 1
        self.last_role = self.last_provider = self.last_model = None
        self.last_usage = self.last_timings = None
        primary_error = None
        if not self.switched:
            try:
                out = self.primary.chat(messages, max_tokens=max_tokens)
                self.consecutive_failures = 0
                self._answered("primary", self.primary)
                self.events.append({"call": self.calls, "event": "call", "role": "primary"})
                return out
            except LLMUnavailable as exc:
                primary_error = str(exc)
                self.consecutive_failures += 1
                if is_exhaustion(exc):
                    self._switch(f"주 모델 소진형 오류: {primary_error[:120]}")
                elif self.consecutive_failures >= self.max_failures:
                    self._switch(f"주 모델 연속 {self.consecutive_failures}회 실패: {primary_error[:120]}")
        try:
            out = self.secondary.chat(messages, max_tokens=max_tokens)
        except LLMUnavailable as exc:
            self.events.append({"call": self.calls, "event": "fail", "primary_error": primary_error,
                                "secondary_error": str(exc)[:200]})
            raise LLMUnavailable(f"주 모델: {primary_error or '전환 후 사용 안 함'} / 보조 모델: {exc}",
                                 status=getattr(exc, "status", None)) from exc
        self._answered("secondary", self.secondary)
        self.events.append({"call": self.calls, "event": "call", "role": "secondary",
                            "primary_error": primary_error and primary_error[:200]})
        return out

    def summary(self) -> dict:
        return {"enabled": True,
                "primary": {"provider": self.primary.provider, "model": self.primary.model},
                "secondary": {"provider": self.secondary.provider, "model": self.secondary.model},
                "switched": self.switched, "switch_reason": self.switch_reason,
                "switched_at_call": self.switched_at_call, "calls": self.calls,
                "secondary_calls": sum(e.get("role") == "secondary" for e in self.events),
                "events": list(self.events)}


def _make(provider: str, model: str, base_url: str | None, api_key: str | None, *, fallback: bool = False):
    timeout = float(os.getenv("LLM_TIMEOUT") or 120)
    if provider == "none":
        return NoLLM()
    if provider == "local":
        return LocalLLM(model)
    if provider == "server":
        # 보조 모델에는 주 모델용 추론 설정(LLM_REASONING_*)을 쓰지 않는다: Qwen 요청 본문은 예전과 같다
        extra = {"reasoning_effort": "", "reasoning_headroom": 0} if fallback else {}
        return ServerLLM(model, base_url or DEFAULT_BASE_URL, timeout=timeout, api_key=api_key, **extra)
    if provider == "anthropic":
        if not base_url:
            name = "LLM_FALLBACK_BASE_URL" if fallback else "LLM_BASE_URL"
            raise ValueError(f"anthropic에는 {name}이 필요함(예: https://<host>/v1/gateway/claude)")
        return AnthropicLLM(model, base_url, timeout=timeout, api_key=api_key,
                            messages_path=os.getenv("LLM_MESSAGES_PATH") or DEFAULT_MESSAGES_PATH)
    raise ValueError(f"알 수 없는 LLM 공급자: {provider}")


def fallback_client(fallback_provider: str | None = None, fallback_model: str | None = None,
                    fallback_base_url: str | None = None):
    """LLM_FALLBACK_* 설정으로 보조 클라이언트를 만든다. 설정이 없거나 none이면 None."""
    provider = (fallback_provider or os.getenv("LLM_FALLBACK_PROVIDER") or "none").strip().lower()
    if provider == "none":
        return None
    model = fallback_model or os.getenv("LLM_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    base = fallback_base_url or os.getenv("LLM_FALLBACK_BASE_URL") or DEFAULT_FALLBACK_BASE_URL
    return _make(provider, model, base, os.getenv("LLM_FALLBACK_API_KEY"), fallback=True)


def with_fallback(primary, **fallback_args):
    """보조 설정이 있으면 FallbackLLM으로 감싼다. 주 공급자가 none이면 그대로(템플릿)."""
    if primary.provider == "none":
        return primary
    secondary = fallback_client(**fallback_args)
    return primary if secondary is None else FallbackLLM(primary, secondary)


def get_client(provider: str | None = None, model: str | None = None, base_url: str | None = None, *,
               fallback_provider: str | None = None, fallback_model: str | None = None,
               fallback_base_url: str | None = None):
    provider = (provider or os.getenv("LLM_PROVIDER") or "none").strip().lower()
    model = model or os.getenv("LLM_MODEL") or DEFAULT_MODEL
    if provider not in ("none", "local", "server", "anthropic"):
        raise ValueError(f"알 수 없는 LLM_PROVIDER: {provider}")
    if provider == "anthropic" and not (base_url or os.getenv("LLM_BASE_URL")):
        raise ValueError("LLM_PROVIDER=anthropic에는 LLM_BASE_URL이 필요함(예: https://<host>/v1/gateway/claude)")
    # 키는 환경변수로만 받는다. 파일에 적지 않는다
    primary = _make(provider, model, base_url or os.getenv("LLM_BASE_URL"), os.getenv("LLM_API_KEY"))
    return with_fallback(primary, fallback_provider=fallback_provider, fallback_model=fallback_model,
                         fallback_base_url=fallback_base_url)
