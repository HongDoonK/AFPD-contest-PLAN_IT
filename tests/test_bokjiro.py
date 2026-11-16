"""복지로 수집기의 변환과 저장을 키 없이 시험한다. 응답 모양은 공공데이터포털 활용가이드의 항목 이름을 따른다."""
import json
import sqlite3

import pytest

from policy_enrich import bokjiro

CENTRAL_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<wantedList><totalCount>2</totalCount><pageNo>1</pageNo><numOfRows>100</numOfRows><resultCode>0</resultCode>
<resultMessage>SUCCESS</resultMessage>
<servList><servId>WLF00000001</servId><servNm>청년월세 한시 특별지원</servNm><jurMnofNm>국토교통부</jurMnofNm>
<jurOrgNm>주거복지지원과</jurOrgNm><servDgst>청년의 월세 부담을 덜어 줍니다.</servDgst>
<servDtlLink>https://www.bokjiro.go.kr/x?wlfareInfoId=WLF00000001</servDtlLink><lifeArray>청년</lifeArray>
<intrsThemaArray>주거</intrsThemaArray><trgterIndvdlArray>저소득</trgterIndvdlArray><sprtCycNm>월</sprtCycNm>
<srvPvsnNm>현금지급</srvPvsnNm><onapPsbltYn>Y</onapPsbltYn><svcfrstRegTs>20260101</svcfrstRegTs></servList>
<servList><servId>WLF00000002</servId><servNm>기초연금</servNm><jurMnofNm>보건복지부</jurMnofNm>
<lifeArray>노년</lifeArray></servList></wantedList>"""

CENTRAL_DETAIL = """<?xml version="1.0" encoding="UTF-8"?>
<wantedDtl><resultCode>0</resultCode><resultMessage>SUCCESS</resultMessage><servId>WLF00000001</servId>
<tgtrDtlCn>만 19~34세 무주택 청년</tgtrDtlCn><slctCritCn>청년가구 중위소득 60% 이하</slctCritCn>
<alwServCn>월 최대 20만원, 최대 24개월</alwServCn>
<applmetList><servSeCode>010</servSeCode><servSeDetailNm>복지로 온라인 신청</servSeDetailNm>
<servSeDetailLink>https://www.bokjiro.go.kr</servSeDetailLink></applmetList>
<applmetList><servSeCode>010</servSeCode><servSeDetailNm>주민센터 방문</servSeDetailNm></applmetList>
<inqplCtadrList><servSeDetailNm>마이홈</servSeDetailNm><servSeDetailLink>1600-1004</servSeDetailLink></inqplCtadrList>
<baslawList><servSeDetailNm>주거기본법</servSeDetailNm></baslawList></wantedDtl>"""

LOCAL_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<wantedList><totalCount>1</totalCount><resultCode>0</resultCode>
<servList><servId>WLF00009999</servId><servNm>성남시 청년기본소득</servNm><ctpvNm>경기도</ctpvNm><sggNm>성남시</sggNm>
<bizChrDeptNm>경기도 성남시 청년정책과</bizChrDeptNm><lifeNmArray>청년, 중장년</lifeNmArray>
<intrsThemaNmArray>생활지원</intrsThemaNmArray><sprtCycNm>분기</sprtCycNm><srvPvsnNm>지역화폐</srvPvsnNm>
<lastModYmd>20260901</lastModYmd></servList></wantedList>"""

LOCAL_DETAIL = """<wantedDtl><resultCode>0</resultCode><servId>WLF00009999</servId>
<sprtTrgtCn>성남시에 3년 이상 거주한 만 24세 청년</sprtTrgtCn><alwServCn>분기별 25만원</alwServCn>
<aplyMtdCn>경기도일자리재단 통합접수시스템에서 신청</aplyMtdCn>
<inqplCtadrList><wlfareInfoDtlCd>010</wlfareInfoDtlCd><wlfareInfoReldCn>031-729-0000</wlfareInfoReldCn>
<wlfareInfoReldNm>성남시 청년정책과</wlfareInfoReldNm></inqplCtadrList>
<baslawList><wlfareInfoDtlCd>030</wlfareInfoDtlCd><wlfareInfoReldNm>성남시 청년기본소득 지급 조례</wlfareInfoReldNm></baslawList>
</wantedDtl>"""

KEY_ERROR = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""


def _conn():
    conn = sqlite3.connect(":memory:")
    bokjiro.ensure_schema(conn)
    return conn


def test_central_list_marks_youth_services():
    items, total = bokjiro.parse_list(CENTRAL_LIST)
    rows = [bokjiro.list_row("central", item, "2026-11-11 00:00:00") for item in items]
    assert total == 2 and [r["is_youth"] for r in rows] == [1, 0]
    assert rows[0]["agency"] == "국토교통부 주거복지지원과" and rows[0]["provision_method"] == "현금지급"
    assert rows[0]["region_sido"] is None and rows[1]["summary"] is None  # 없는 값은 비워 둔다


def test_local_list_keeps_region():
    items, _ = bokjiro.parse_list(LOCAL_LIST)
    row = bokjiro.list_row("local", items[0], "t")
    assert (row["region_sido"], row["region_sigungu"], row["is_youth"]) == ("경기도", "성남시", 1)
    assert row["agency"] == "경기도 성남시 청년정책과" and row["last_modified"] == "20260901"


def test_detail_joins_repeated_entries():
    row = bokjiro.detail_row(bokjiro.parse_detail(CENTRAL_DETAIL))
    assert row["support_content"] == "월 최대 20만원, 최대 24개월"
    assert row["application_method"] == "복지로 온라인 신청(https://www.bokjiro.go.kr) / 주민센터 방문"
    assert row["contact"] == "마이홈(1600-1004)" and row["base_law"] == "주거기본법" and row["homepage"] is None
    local = bokjiro.detail_row(bokjiro.parse_detail(LOCAL_DETAIL))
    assert local["target_detail"].startswith("성남시에 3년") and local["application_method"].startswith("경기도일자리재단")
    assert local["selection_criteria"] is None
    # 실제 지자체 응답(2026-11-11)은 문의처·근거법령을 wlfareInfoReldNm/Cn으로 준다
    assert local["contact"] == "성남시 청년정책과(031-729-0000)" and local["base_law"] == "성남시 청년기본소득 지급 조례"


def test_portal_errors_are_raised_with_their_code():
    with pytest.raises(bokjiro.BokjiroError) as exc:
        bokjiro.parse_list(KEY_ERROR)
    assert exc.value.code == "30" and "NOT_REGISTERED" in str(exc.value)


def test_relisting_keeps_collected_details():
    conn = _conn()
    items, _ = bokjiro.parse_list(CENTRAL_LIST)
    bokjiro.store_list(conn, [bokjiro.list_row("central", i, "t1") for i in items])
    bokjiro.store_detail(conn, "WLF00000001", bokjiro.parse_detail(CENTRAL_DETAIL), "t2")
    bokjiro.store_list(conn, [bokjiro.list_row("central", i, "t3") for i in items])  # 다음 날 목록을 다시 받음
    row = conn.execute("SELECT support_content, detail_collected_at, collected_at, raw_json FROM welfare_services "
                       "WHERE service_id='WLF00000001'").fetchone()
    assert row[:3] == ("월 최대 20만원, 최대 24개월", "t2", "t3")
    assert set(json.loads(row[3])) == {"list", "detail"}


def test_collect_fetches_youth_details_first_and_stops_at_daily_limit(monkeypatch):
    calls = []

    def fake_get(path, params, api_key, retries=2):
        assert api_key == "test-key"
        if path.endswith("listV001"):
            return CENTRAL_LIST
        calls.append(params["servId"])
        if len(calls) == 2:
            return KEY_ERROR.replace("<returnReasonCode>30", "<returnReasonCode>22")
        return CENTRAL_DETAIL

    monkeypatch.setattr(bokjiro, "_get", fake_get)
    conn = _conn()
    report = bokjiro.collect(conn, "test-key", scopes=("central",), sleep_sec=0)
    assert calls == ["WLF00000001", "WLF00000002"]  # 청년 대상부터
    assert report["details"] == {"fetched": 1, "pending": 1} and "한도" in report["stopped"]
    assert conn.execute("SELECT COUNT(*) FROM welfare_services WHERE detail_collected_at IS NULL").fetchone()[0] == 1


def test_collect_without_key_is_skipped(monkeypatch):
    monkeypatch.delenv("BOKJIRO_API_KEY", raising=False)
    assert bokjiro.collect(_conn()) == {"skipped": "BOKJIRO_API_KEY 없음"}


def test_details_only_skips_list_calls(monkeypatch):
    paths = []

    def fake_get(path, params, api_key, retries=2):
        paths.append(path)
        return CENTRAL_LIST if path.endswith("listV001") else CENTRAL_DETAIL

    monkeypatch.setattr(bokjiro, "_get", fake_get)
    conn = _conn()
    bokjiro.collect(conn, "k", scopes=("central",), details=False, sleep_sec=0)
    paths.clear()
    report = bokjiro.collect(conn, "k", scopes=("central",), lists=False, sleep_sec=0)
    assert paths and not any(p.endswith("listV001") for p in paths)  # 이어 받을 때 목록을 다시 부르지 않는다
    assert report["details"] == {"fetched": 2, "pending": 0}
