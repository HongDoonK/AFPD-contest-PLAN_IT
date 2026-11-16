# 청년 재무설계 지원 에이전트

청년 고객의 Fact Finding 문서와 원천징수영수증을 읽어 정책을 매칭하고, 고정·변동 지출을 나눠 여유자금을 계산한 뒤, 주거·결혼·양육·노후 목표에 비율로 배분해 단기·중기·장기 전략을 만든다. 결과는 재무설계사가 보는 대시보드(PDF)로 나온다.

금액과 비율은 모두 계산 엔진이 정한다. LLM은 전략 카드의 순서와 문장만 맡고, 문장 속 숫자는 엔진 값과 맞는지 검사한다. LLM 없이도 템플릿 문장으로 끝까지 실행된다.

## 준비

Python 3.11 이상이 필요하다(3.13에서 확인).

```bash
pip install -r requirements.txt
```

```bash
python -X utf8 setup_data.py
```

`setup_data.py`는 압축된 정책 DB를 풀고(약 117MB), 가상 고객 6명의 프로필 DB, 정책 매칭 결과, 소비 보고서를 만든다. API 키는 필요 없다. 테스트 일부가 이 결과물을 읽으므로 테스트보다 먼저 실행한다.

## 환경설정

LLM을 쓰지 않으면 건너뛴다. 쓰려면 `.env.example`을 `.env`로 복사해 값을 채운 뒤 환경변수로 불러온다. 코드는 환경변수만 읽으며 `.env`는 저장소에 올라가지 않는다.

bash:

```bash
set -a; source .env; set +a
```

PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=.' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
```

| 환경변수 | 내용 |
|---|---|
| `LLM_PROVIDER` | `none`(기본, 템플릿 문장), `server`(OpenAI 호환), `anthropic`(Anthropic Messages 형식), `local`(transformers) |
| `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY` | 모델 이름, 주소, 키 |
| `LLM_REASONING_EFFORT` | 추론 모델은 `none`으로 꺼야 빈 응답이 나오지 않는다 |
| `LLM_FALLBACK_*` | 주 모델의 토큰이 떨어지면 넘어갈 예비 모델. 없으면 템플릿 문장으로 넘어간다 |
| `FINLIFE_API_KEY`, `BOKJIRO_API_KEY` | 정책 DB를 새로 수집할 때만 필요 (`policy_enrich/README.md`) |

## 실행

```bash
python -X utf8 -m planning.pipeline --case kim_doyun --step2 planning/cases/kim_doyun_step2.json
```

```bash
python -X utf8 -m planning.pipeline --case blank_01
```

새 고객 문서 폴더(Fact Finding `.hwp` + 원천징수영수증 `.docx`)에서 바로 실행:

```bash
python -X utf8 -m planning.pipeline --case new_01 --docs <고객 문서 폴더>
```

LLM을 붙여 실행(환경변수 대신 옵션으로도 줄 수 있다. 키는 환경변수로만 준다):

```bash
python -X utf8 -m planning.pipeline --case kim_doyun --llm-provider server --llm-model <모델> --llm-base-url <주소> --suffix _llm --force
```

`output/`에 세 파일이 생긴다.

| 파일 | 내용 |
|---|---|
| `dashboard_<case>_<scenario>_v1.pdf` | 재무설계사용 대시보드 (A4 가로) |
| `plan_<case>_<scenario>_v1.json` | 계산 결과 전체 |
| `ai_log_plan_<case>_<scenario>_v1.json` | LLM 호출과 문장 채택·거절 기록 |

PDF는 PC에 설치된 Edge·Chrome·Chromium을 창 없이 실행해 만든다(추가 패키지 없음). 브라우저를 못 찾으면 `PDF_BROWSER` 환경변수에 실행 파일 경로를 넣거나, `--format html`로 HTML만 받는다. `--format both`는 둘 다 만든다.

주요 옵션: `--format pdf|html|both`, `--scenario certain|potential`(확정 혜택만 / 확인 필요한 혜택 포함), `--goal-basis customer|statistical`, `--inflation 2.0`, `--force`(덮어쓰기).

가상 고객: `kim_doyun`, `lee_seojun`, `blank_01`~`blank_04`.

## 테스트

```bash
python -X utf8 -m pytest -q tests
```

## 구성

| 폴더 | 역할 |
|---|---|
| `policy_enrich/` | 청년 정책·금융상품·복지서비스 DB 구축 |
| `customer/` | 고객 입력 스키마, 문서에서 프로필 만들기, 정책 매칭 |
| `spending_report/` | 현금흐름표 파싱, 소비 진단, LLM 연결과 문장 검증 |
| `planning/` | 여유자금 계산, 목표별 배분, 전략, 노후 수령 세금 구간, 대시보드, 전체 파이프라인 |
| `colab/` | Colab에서 돌리는 LLM 문장 실험 도구 |
| `가상고객데이터/` | 가상 고객 입력 문서 |
| `data/` | 정책 DB 압축본 |
| `tests/` | 테스트 |

각 폴더의 `README.md`에 자세한 설명이 있다.

## 알아둘 점

- 고객은 모두 가상 인물이다.
- 정책 DB는 2026-11 수집본이다. 지원금은 확정 전에는 계획 재원에 넣지 않는다.
- 수익률·물가는 계산 전제이며 예측이 아니다. 세법 한도 값과 양육비 산정기준표는 원문 확인이 필요한 항목으로 표시된다.
- 결과는 재무설계사의 검토를 돕는 자료이며 세무·투자 자문이 아니다.
