"""복지로 복지서비스를 고객 지역·나이·소득으로 판정하고 온통청년과 겹치는 사업을 빼는지 시험한다."""
import json
import sqlite3

from customer.policy_match import MET, NOT_MET, UNKNOWN
from customer.welfare_match import core_key, customer_region, is_youth_specific, judge_welfare, match_welfare, parse_ages
from policy_enrich import bokjiro

MEDIAN = {1: 2_564_238, 2: 4_199_292}


def _customer(**kw):
    base = {"case_id": "t", "profile_version": "v1", "as_of": "2026-11-12", "customer_type": "근로", "age": 34,
            "region_code": "41133", "sex": "남", "household_size": 1, "marital_status": "미혼", "home_owner": False,
            "employment_status": "재직", "employer_size": None, "parents_household": "별도 거주",
            "income": {("근로", "총급여"): {"amount_won": 57_600_000, "tax_year": 2025, "status": "확인"}},
            "net_assets_won": 100_000_000, "goals": "", "facts": {}}
    base.update(kw)
    return base


def _row(**kw):
    base = {"service_id": "WLF1", "scope": "local", "service_name": "성남시 청년 마음건강 지원", "region_sido": "경기도",
            "region_sigungu": "성남시", "agency": "성남시 청년정책과", "summary": "", "life_cycle": "청년",
            "target_group": None, "interest_theme": "정신건강", "support_cycle": "1회성", "provision_method": "바우처",
            "online_apply_yn": None, "service_url": "u", "target_detail": "성남시 거주 만 19~39세 청년",
            "selection_criteria": None, "support_content": "심리상담 바우처 최대 30만원", "application_method": None,
            "contact": None, "homepage": None, "base_law": None, "last_modified": None, "is_youth": 1,
            "detail_collected_at": "t", "collected_at": "t", "raw_json": json.dumps({"detail": {"enfcEndYmd": "99991231"}})}
    base.update(kw)
    return base


def _result(finding, name):
    return next(x["result"] for x in finding["conditions"] if x["condition"] == name)


def test_region_code_maps_to_bokjiro_names():
    assert customer_region("41133") == ("경기도", {"중원구", "성남시"})  # 복지로는 성남시 단위로 적는다
    assert customer_region("11215")[0] == "서울특별시" and "광진구" in customer_region("11215")[1]
    assert customer_region("4111")[1] == {"수원시"}


def test_age_ranges_ignore_child_ages():
    assert parse_ages("만 19~39세 청년") == [(19, 39)]
    assert parse_ages("39세 이하 구민") == [(0, 39)] and parse_ages("35세 미만") == [(0, 34)]
    f = judge_welfare(_row(target_detail="0~7세 자녀를 둔 청년 부모"), _customer(), MEDIAN)
    assert _result(f, "연령") == MET  # 자녀 나이는 고객 나이 조건이 아니다


def test_youth_service_without_income_text_is_met():
    f = judge_welfare(_row(), _customer(), MEDIAN)
    assert f["verdict"] == "입력상 충족" and _result(f, "소득") == MET
    assert f["evidence"]["source"] == "복지로 지자체 복지서비스" and f["youth_specific"]


def test_age_and_income_limits_are_judged():
    assert _result(judge_welfare(_row(target_detail="만 19~29세"), _customer(), MEDIAN), "연령") == NOT_MET
    f = judge_welfare(_row(selection_criteria="기준 중위소득 100% 이하"), _customer(), MEDIAN)
    assert f["verdict"] == "불충족"  # 5,760만원/12 ÷ 1인 중위소득 = 187%


def test_special_groups_and_school_programs():
    f = judge_welfare(_row(target_group="장애인"), _customer(), MEDIAN)
    assert f["verdict"] == "확인 필요" and _result(f, "특화 대상") == UNKNOWN
    g = judge_welfare(_row(service_name="고교 취업연계 장려금", life_cycle="청소년, 청년", target_detail=None), _customer(), MEDIAN)
    assert g["verdict"] == "불충족"


def test_broad_life_cycle_is_situational():
    assert not is_youth_specific(_row(service_name="의사상자지원", life_cycle="청년, 중장년, 노년"))
    assert is_youth_specific(_row(service_name="마음건강 바우처", life_cycle="청년, 청소년"))


def test_match_keeps_region_and_drops_youth_policy_duplicates():
    conn = sqlite3.connect(":memory:")
    bokjiro.ensure_schema(conn)
    rows = [_row(),
            _row(service_id="WLF2", service_name="성남시 청년기본소득"),
            _row(service_id="WLF3", region_sigungu="수원시"),
            _row(service_id="WLF4", region_sigungu=None, service_name="경기도 청년 노동자 통장"),
            _row(service_id="WLF5", scope="central", region_sido=None, region_sigungu=None, service_name="청년 마음건강"),
            _row(service_id="WLF6", raw_json=json.dumps({"detail": {"enfcEndYmd": "20260101"}}))]
    cols = list(rows[0])
    conn.executemany(f"INSERT INTO welfare_services ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [tuple(r[c] for c in cols) for r in rows])
    found, counts = match_welfare(conn, _customer(), MEDIAN, ["청년기본소득"])
    assert sorted(f["policy_id"] for f in found) == ["WLF1", "WLF4", "WLF5"]
    assert counts["same_as_youth_policy"] == 1 and counts["closed"] == 1
    assert core_key("성남시 청년기본소득") == core_key("청년기본소득")


def test_name_variants_of_the_same_program_are_duplicates():
    from customer.welfare_match import is_duplicate
    known = {core_key("청년내일저축계좌 지원사업"), core_key("2026 희망두배 청년통장")}
    assert is_duplicate(core_key("청년내일저축계좌"), known)
    assert is_duplicate(core_key("희망두배 청년통장"), known)
    assert not is_duplicate(core_key("청년 마음건강 지원"), known)
