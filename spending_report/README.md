# spending_report

Fact Finding 7번 현금흐름표를 기준으로 서비스 적용 전·후를 비교하는 소비습관 개선 보고서를 만든다. 계획과 남은 일은 `.scratch/spending-report/`에 있다.

## 실행

고객 프로필과 정책 요약본이 먼저 있어야 한다.

```bash
python -m customer.load customer/cases/kim_doyun.json
```

```bash
python -m customer.policy_match kim_doyun
```

```bash
python -m customer.spending_handoff kim_doyun
```

```bash
python -m spending_report kim_doyun
```

문서에서 바로 프로필을 만들 때는 `customer.load` 대신 다음을 쓴다.

```bash
python -m customer.from_documents 가상고객데이터/빈칸형_가상고객_4명/01_김민지 --case-id blank_01
```

`output/`에 `spending_report_*.json`, `spending_report_*.html`, `ai_log_*.json`이 생기고 `module_results`에도 저장된다.

## 구성

| 파일 | 역할 |
|---|---|
| `hwp.py` | HWP 5.0 문서의 표 읽기 |
| `intake.py` | FF 7번과 원천징수영수증(근로·사업) 파싱, 저장값 대조 |
| `inputs.py` | 프로필과 정책 요약본을 같은 버전으로 읽기 |
| `rules.py` | 소비 분류, 임시 절감률, 유지 사유. 임시값은 이 파일에서 고친다 |
| `engine.py` | 적용 전·후 표, 소비 진단, 지표. 모든 금액의 출처 |
| `guidance.py` | 자료 요청표, 실행·점검 계획, 공제 확인 항목 |
| `llm.py`, `narrative.py` | 문장 생성과 숫자 검증 |
| `report.py`, `render.py`, `templates/report.html.j2` | JSON 조립과 HTML. 보고서 형태는 템플릿 한 파일에서 고친다 |

## 입력이 비어 있을 때

빈칸과 '잘 모르겠음'은 0원이 아니라 미확인이다. '60 정도', '350~390'은 추정이며 범위와 중간값을 함께 저장한다. 미확인 행이 있으면 비율 지표와 소비 진단을 만들지 않고, 쓰임을 확인하지 못한 금액과 자료 요청표를 보여 준다. 어림값에는 절감률을 곱하지 않는다.

## LLM

기본값 `LLM_PROVIDER=none`은 템플릿 문장을 쓴다. 금액과 비율은 어떤 공급자를 써도 계산 엔진이 정한다.

| 환경변수 | 값 |
|---|---|
| `LLM_PROVIDER` | `none`, `local`(transformers), `server`(OpenAI 호환 서버), `anthropic`(Anthropic Messages 형식 게이트웨이) |
| `LLM_MODEL` | 기본 `Qwen/Qwen3-4B-Instruct-2507` |
| `LLM_BASE_URL` | `server`일 때 주소. 기본 `http://localhost:8080/v1` |
| `LLM_ALLOW_DOWNLOAD` | `1`이면 `local`에서 모델을 자동으로 받음. 기본은 받지 않음 |
| `LLM_API_KEY` | `server`일 때 Bearer 키. 환경변수로만 준다 |
| `LLM_TIMEOUT` | `server` 제한 시간(초). 기본 120 |
| `LLM_MESSAGES_PATH` | `anthropic`일 때 `LLM_BASE_URL` 뒤에 붙는 경로. 기본 `/v1/messages` |
| `LLM_THINKING_HEADROOM` | `anthropic`에서 `thinking: disabled`가 거부될 때 max_tokens에 더할 여유. 기본 2000 |

`anthropic`은 키를 `x-api-key`와 `Authorization: Bearer` 두 헤더로 보내고, system 역할을 본문 `system`으로 뺀다. 응답 `content`에서 text 블록만 이어 붙인다(thinking 블록은 버림). HTTP 400 본문에 매개변수가 언급되면 한 번 고쳐 다시 부른다(anthropic: thinking 제거와 max_tokens 여유, temperature 제거. server: `max_completion_tokens`로 바꾸기, temperature·seed 제거). 오류 메시지에는 응답 본문 앞 200자만 남기고 키는 지운다.

기본 모델 `Qwen/Qwen3-4B-Instruct-2507`(Apache-2.0)은 2026-11-10 Colab 실험에서 16문장 모두 1회차에 통과해 가장 안정적이었다(`.scratch/spending-report/issues/02-qwen-korean-eval.md`). 처음 잡았던 `Qwen/Qwen2.5-3B-Instruct`는 Qwen Research 라이선스(비상업적 연구·평가 한정)이고 같은 실험에서 16문장 중 10개만 채택됐다. 기본 모델은 bf16으로 약 8GB라 GPU 없는 PC에서 `local`로 돌리기는 어렵다. 실행 환경은 `.scratch/spending-report/issues/09-llm-deploy.md`에서 정한다.

LLM은 초안을 다듬기만 하고 새 행동을 제안하지 않는다. 거절되면 사유를 알려 한 번 더 쓰게 하고, 그래도 안 되면 그 문장만 템플릿으로 되돌린다. 거절 사유: 사실에 없는 금액·비율, 초안 숫자의 누락·순서 뒤바뀜, 숫자와 항목의 짝 어긋남, 시뮬레이션·확정 불가 단서 누락, 정책 문장의 조건 표현('조건을 채우면') 누락, 한자·일본어, 확정·투자 권유, 목록·머리말, 반말, 고객 대신 다짐하거나 이미 한 일처럼 쓴 말.

### 주 모델 → 대체 모델 → 템플릿

환경변수를 주지 않으면 예전과 같다(대체 없음). `LLM_FALLBACK_PROVIDER`를 주면 `get_client`가 `FallbackLLM`으로 감싼다.

| 환경변수 | 값 |
|---|---|
| `LLM_REASONING_EFFORT` | 주 모델(OpenAI 호환, 예: gpt-6-luna)의 `reasoning_effort`(none, low 등). 주면 `max_completion_tokens`를 쓴다 |
| `LLM_REASONING_HEADROOM` | 주 모델 토큰 한도에 더할 여유. 기본 0 |
| `LLM_FALLBACK_PROVIDER` | `none`(기본), `server`, `local`, `anthropic` |
| `LLM_FALLBACK_MODEL` | 기본 `Qwen/Qwen3-4B-Instruct-2507` |
| `LLM_FALLBACK_BASE_URL` | 기본 `http://localhost:8080/v1` |
| `LLM_FALLBACK_API_KEY` | 보조 모델 키(선택). 환경변수로만 |

- 주 모델이 소진형 오류(HTTP 401·402·403·429, 또는 본문에 quota·insufficient·credit·balance·exceeded·limit·한도·소진·잔액)를 내면 그 뒤로 보조 모델만 쓴다. 429는 일시 재시도 후에도 실패한 경우다.
- 그 밖의 오류(시간 초과, 빈 응답, 5xx)는 그 호출만 보조 모델로 넘기고, 주 모델이 연속 3회 실패하면 보조 모델로 영구 전환한다.
- 보조 모델에는 `LLM_REASONING_*`를 쓰지 않는다(Qwen 요청 본문은 예전과 같다). 보조 모델도 실패하면 `LLMUnavailable`이 나가고, 소비 보고서·에이전트는 남은 문장을 템플릿으로 쓴다.
- 기록: ai_log와 plan `advice`에 호출별 실제 공급자·모델(`role`: primary·secondary), 전환 여부·사유·시점(`fallback`), 문장 출처(`sentence_sources`). 대시보드 AI 기록에 "주 모델 N문장, 대체 모델 M문장, 템플릿 K문장"과 전환 사유. llm_eval summary에 모델별 `fallback_calls`.

### 노트북 PC(CPU)에서 쓰기

GPU가 없으면 양자화 모델을 llama.cpp 서버로 띄우고 `server` 공급자로 부른다. 2026-11-10 Colab CPU 4스레드 시험에서 `unsloth/Qwen3-4B-Instruct-2507-GGUF`의 Q4_K_M(2.5GB)이 16문장을 모두 통과했고 문장 하나에 약 8.5초 걸렸다(09 티켓).

```bash
llama-server -m Qwen3-4B-Instruct-2507-Q4_K_M.gguf --host 127.0.0.1 --port 8080 -t 4 -c 2048 -np 1
```

`--port`를 주지 않으면 llama.cpp v0.9.0은 9931번을 쓴다. `-c`를 주지 않으면 모델 최대 문맥만큼 메모리를 잡는다. 보고서 쪽에는 `LLM_PROVIDER=server`와 `LLM_MODEL=unsloth/Qwen3-4B-Instruct-2507-Q4_K_M`(기록용 이름)을 준다.

## 문장 실험

```bash
python -X utf8 -m spending_report.llm_eval --colab-results <Colab에서 저장한 노트북.ipynb> --label colab_3
```

GPU 서버가 있으면 `LLM_BASE_URL`, `LLM_API_KEY`를 주고 `--models`로 바로 돌린다. Colab 사용법은 `colab/README.md`. 결과는 `output/llm_eval/<label>/`의 `summary.md`(채택률·거절 사유·속도), `samples.md`(문장 비교), 모델별 전체 보고서.
