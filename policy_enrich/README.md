# policy_enrich

[HongDoonK/-AI-](https://github.com/HongDoonK/-AI-)의 `data/youth_policy.db`를 복사해, 금융 습관 개선 에이전트가 쓸 테이블을 덧붙인 `data/youth_policy_enriched.db`를 만든다. 원본 테이블은 바꾸지 않는다. 단, 원본에 남아 있던 `users` 실사용 프로필은 비운다.

```bash
python -m policy_enrich.build --source <원본 youth_policy.db> --out data/youth_policy_enriched.db --as-of 20261104
```

```bash
python -m pytest -q tests
```

## 추가되는 테이블

| 구분 | 테이블 / 뷰 | 내용 |
|---|---|---|
| (가) | `policy_conditions` | 본인·가구 중위소득 상한 %, 본인/부부합산 연소득·월소득·재산·건강보험료 상한, 근로소득 요건, 모집 상태(`as_of` 기준), 중복 묶음 키 |
| (나) | `policy_benefits` | 혜택 유형(매칭저축·저축상품·이자지원·보증료지원·대출·월세·주거비·현금지원·바우처·현물 등), 월액·기간·총액, 대상별 금액 범위(`amount_min_won`~`amount_max_won`), 매칭저축의 본인 저축액·매칭액·배수, 대출 한도, 이자지원율, 한 줄 요약 |
| (나) | `asset_products` | 직접 확인한 중앙정부 자산형성 상품(청년미래적금, 청년도약계좌, 청년내일저축계좌), 출처와 확인일 포함 |
| (나) | `median_income` | 2026년 기준 중위소득(가구원 수별, 월) |
| (다) | `fin_products`, `fin_product_options` | 금감원 예금·적금(`FINLIFE_API_KEY` 필요), 서민금융 청년 대출 21건(`youth_loan`) |
| (다) | `fin_loan_options` | 금감원 전세자금·개인신용·주택담보 대출 금리. 전세·주담대는 상환방식·금리유형별 최저/최고/평균, 신용대출은 신용점수 구간별 |
| 뷰 | `v_policy_finance` | 정책 + 조건 + 혜택을 한 줄로. `recruit_status IN ('open','always')`로 지금 신청 가능한 것만 거른다 |
| 뷰 | `v_fin_product_best_rate` | 상품·기간별 기본금리와 최고금리 |
| 뷰 | `v_fin_loan_rate` | 대출 상품별 금리(신용대출은 실제 적용 '대출금리' 행만) |

## 신뢰도 표시

원문에 없는 수치는 만들지 않는다. 대신 각 행에 `parse_confidence`를 남긴다.

- `high`: 소득 전용 칸(`income_etc`)이나 지원내용에서 핵심 수치를 2개 이상 찾음
- `partial`: 보조 문장에서 찾았거나 수치가 1개뿐, 또는 대상별 금액 범위만 있음(출산장려금 첫째~넷째 등)
- `none`: 찾지 못함. 에이전트는 "공고 확인 필요"로 안내해야 한다
- `not_applicable`: 소득 조건 없음

## 금감원 API 키

[금융상품 한눈에](https://finlife.fss.or.kr) 오픈 API에서 인증키를 발급받아 환경변수로 넣고 빌드를 다시 돌린다.

```bash
FINLIFE_API_KEY=발급받은키 python -m policy_enrich.build --source <원본 DB>
```

## 복지로 API 키

`welfare_services`는 복지로(한국사회보장정보원)의 중앙부처·지자체 복지서비스를 전국 단위로 담는 테이블이다. 원본 DB의 `welfare_central`(159건), `welfare_chungbuk_local`(충북 68건)은 그대로 둔다. `scope`(central/local), 지역, 생애주기, 지원 주기, 제공 유형, 상세(지원대상·선정기준·지원내용·신청방법), `is_youth`(생애주기에 청년 포함)를 갖는다.

[공공데이터포털](https://www.data.go.kr)에서 '한국사회보장정보원_중앙부처복지서비스'와 '한국사회보장정보원_지자체복지서비스'를 활용신청하고, 일반 인증키(Decoding)를 환경변수로 넣는다.

```bash
BOKJIRO_API_KEY=발급받은키 python -X utf8 -m policy_enrich.bokjiro --db data/youth_policy_enriched.db
```

상세는 서비스마다 한 번씩 부르므로 수천 건이다. 청년 대상부터 받고, 하루 호출 한도에 걸리면 멈춘다. 다음 날 같은 명령을 돌리면 남은 것만 이어 받는다. `--limit 5`로 먼저 시험한다.

2026-11-11 수집 상태: 목록은 전부 받았다(중앙부처 461건, 지자체 4,828건, 청년 대상 1,408건). 상세는 하루 한도에 걸려 중앙부처 97건, 지자체 934건까지 받았다(모두 청년 대상). 한도는 API별로 따로이고 상세 기준으로 중앙부처 약 100건, 지자체 약 1,000건이었다. 2026-11-12에 이어 받아 청년 대상 1,408건(중앙부처 152, 지자체 1,256)은 상세까지 모두 받았다. 남은 상세 약 3,800건은 청년 대상이 아니어서 정책 매칭에는 쓰지 않는다. 더 받을 때는 목록 호출로 한도를 쓰지 않도록 `--details-only`를 붙인다. 동시에 6건씩 부르면 초당 요청 제한(23)에 걸리므로 `--workers 2`를 쓴다. 같은 DB에 수집을 두 개 동시에 돌리면 잠금 오류가 난다. 원본 `welfare_central` 159건은 모두 새 테이블에도 있다.

## 알려진 한계

- 금감원 연금저축 API는 2026-11-04 기준 대출 상품 목록을 돌려줘서 수집하지 않는다(`finlife.py` 주석 참고).
- 저축은행 대출은 금리 폭이 넓다(예: 전세 3.08~17%). 최저금리만 보여주면 오해를 사므로 최고금리와 함께 쓴다.
- 대출 7개 상품(신용 2, 주담대 5)은 금리 옵션 없이 공시돼 금리 비교에서 빠진다.
- 저축은행 적금 중에는 최고금리가 10%를 넘는 이벤트성 상품이 있다. 우대조건(`spcl_cnd`)과 가입한도(`max_limit_won`)를 함께 보여줘야 한다.

- '소득 8분위 이하', '유형별 상이'처럼 금액이나 %가 없는 조건은 `none`으로 남는다.
- 매칭저축 분류에 소수의 오탐이 있다(예: 충북 다자녀 행복카드). 금액이 없는 행은 요약에 "공고 확인"이 붙는다.
- `asset_products`는 사람이 관리하는 값이다. 바꿀 때는 `reference.py`의 `source_url`, `verified_at`도 같이 고친다.
- 정책 원문 자체가 2026-05~06 수집본이라, 최신화하려면 원 저장소의 수집기(`backend/api_collector.py`)를 `YOUTH_POLICY_API_KEY`로 다시 돌린 뒤 이 빌드를 실행해야 한다.
