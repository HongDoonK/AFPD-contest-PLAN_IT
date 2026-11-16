"""planning.pipeline 시험. 정책 DB가 없으면 건너뛴다. 출력은 tmp_path에만 쓴다."""
import os
from pathlib import Path

import pytest

from planning.pipeline import POLICY_DB, ROOT, StepFailed, main, out_paths, run_pipeline
from spending_report.llm import NoLLM

needs_db = pytest.mark.skipif(not POLICY_DB.exists(), reason="정책 DB 없음")
KIM_STEP2 = ROOT / "planning" / "cases" / "kim_doyun_step2.json"
CUSTOMER_DB = ROOT / "data" / "customer_profiles.db"
TOP_KEYS = ["module", "version", "case_id", "scenario", "generated_at", "customer", "step2", "policy", "spending",
            "strategy", "advice", "pipeline", "limitations", "review"]


@pytest.fixture(scope="module")
def kim_plan():
    if not POLICY_DB.exists():
        pytest.skip("정책 DB 없음")
    return run_pipeline("kim_doyun", step2_path=KIM_STEP2, scenario="certain", client=NoLLM())


@needs_db
def test_plan_shape(kim_plan):
    plan, ai_log = kim_plan
    for k in TOP_KEYS:
        assert k in plan, k
    assert plan["review"]["state"] == "담당자 검토 전"
    assert plan["step2_source"] == "팀원 step2"
    names = [s["step"] for s in plan["pipeline"]["steps"]]
    assert names == ["문서→프로필", "프로필 적재", "정책 매칭", "지출 연결", "소비 보고서", "step2", "정책 절감",
                     "반영본 step2", "전략 엔진", "에이전트"]
    assert all(s["status"] in ("ok", "skipped") for s in plan["pipeline"]["steps"])
    assert all("seconds" in s for s in plan["pipeline"]["steps"])
    # 팀원 step2의 p·q·r·s·M은 그대로, deltaM만 반영
    assert (plan["step2"]["p"], plan["step2"]["r"], plan["step2"]["M"]) == (300.0, 500.0, 4700.0)
    assert "policy_reflection" in plan["step2"]["_meta"] and "_allocation" in plan["step2"]
    assert plan["advice"]["calls"] == 0
    assert set(ai_log) >= {"spending_report", "advisor"}


@needs_db
def test_pipeline_without_team_step2_uses_build_step2():
    plan, _ = run_pipeline("blank_01", client=NoLLM())
    assert plan["step2_source"].startswith("프로필에서 유도")
    assert plan["step2"]["_meta"]["derived"] is True
    assert plan["strategy"]["data_requests"]


@needs_db
def test_main_writes_three_files_and_refuses_overwrite(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    mtime = CUSTOMER_DB.stat().st_mtime if CUSTOMER_DB.exists() else None
    pmtime = POLICY_DB.stat().st_mtime
    args = ["--case", "kim_doyun", "--step2", str(KIM_STEP2), "--scenario", "potential", "--out-dir", str(tmp_path)]
    assert main(args) == 0
    paths = out_paths(tmp_path, "kim_doyun", "potential")
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths.values())
    html = paths["dashboard"].read_text(encoding="utf-8")
    assert "대안 비교" in html
    capsys.readouterr()
    assert main(args) == 2  # --force 없이 덮어쓰지 않음
    assert "덮어쓰지 않음" in capsys.readouterr().err
    assert main(args + ["--force"]) == 0
    # 두 DB는 건드리지 않는다
    if mtime is not None:
        assert CUSTOMER_DB.stat().st_mtime == mtime
    assert POLICY_DB.stat().st_mtime == pmtime


def test_failure_is_one_line(tmp_path, capsys):
    assert main(["--case", "no_such_case", "--out-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err.strip()
    assert "\n" not in err and "프로필 적재" in err and "Traceback" not in err
    assert not list(tmp_path.iterdir())


def test_missing_policy_db_fails_at_policy_step(tmp_path):
    with pytest.raises(StepFailed) as exc:
        run_pipeline("kim_doyun", client=NoLLM(), policy_db=tmp_path / "none.db")
    assert exc.value.step == "정책 매칭"


@needs_db
def test_docs_folder_builds_profile_in_memory():
    folder = ROOT / "가상고객데이터" / "빈칸형_가상고객_4명" / "01_김민지"
    if not folder.exists():
        pytest.skip("빈칸형 문서 폴더 없음")
    before = sorted(os.listdir(ROOT / "customer" / "cases" / "auto"))
    plan, _ = run_pipeline("doc_test", docs=folder, client=NoLLM())
    assert plan["pipeline"]["steps"][0] == {**plan["pipeline"]["steps"][0], "step": "문서→프로필", "status": "ok"}
    assert plan["case_id"] == "doc_test"
    assert sorted(os.listdir(ROOT / "customer" / "cases" / "auto")) == before  # 케이스 파일을 남기지 않음

