"""복지로(한국사회보장정보원) 복지서비스 오픈 API 수집기.

중앙부처복지서비스와 지자체복지서비스(전국)를 welfare_services 한 테이블에 넣는다. 원본 DB의
welfare_central(159건), welfare_chungbuk_local(충북 68건)은 건드리지 않는다.
인증키는 공공데이터포털(data.go.kr)에서 두 API를 활용신청해 받은 일반 인증키(Decoding)를
환경변수 BOKJIRO_API_KEY에 둔다. 파일에 적지 않는다.

    BOKJIRO_API_KEY=... python -X utf8 -m policy_enrich.bokjiro --db data/youth_policy_enriched.db

목록을 먼저 넣고 상세(지원대상·선정기준·지원내용·신청방법)를 서비스마다 한 번씩 부른다. 상세가 이미 있는
서비스는 건너뛰므로, 하루 호출 한도에 걸리면 다음 날 같은 명령으로 이어서 받는다.
네트워크 호출(fetch_*)과 변환(parse_*, *_row)을 나눠, 변환은 키 없이 테스트한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE_URL = "https://apis.data.go.kr/B554287"
ENDPOINTS = {
    "central": {"list": "NationalWelfareInformationsV001/NationalWelfarelistV001",
                "detail": "NationalWelfareInformationsV001/NationalWelfaredetailedV001",
                # 중앙부처 API는 호출 구분(L 목록, D 상세)과 검색 분류(003 = 서비스명·내용 전체)를 요구한다
                "list_params": {"callTp": "L", "srchKeyCode": "003"}, "detail_params": {"callTp": "D"}},
    "local": {"list": "LocalGovernmentWelfareInformations/LcgvWelfarelist",
              "detail": "LocalGovernmentWelfareInformations/LcgvWelfaredetailed",
              "list_params": {}, "detail_params": {}},
}
PAGE_SIZE = 100
DAILY_LIMIT_CODES = {"22"}  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR

SCHEMA = """
CREATE TABLE IF NOT EXISTS welfare_services (
    service_id          TEXT PRIMARY KEY,
    scope               TEXT NOT NULL,      -- central(중앙부처) | local(지자체)
    service_name        TEXT NOT NULL,
    region_sido         TEXT,               -- 지자체만
    region_sigungu      TEXT,
    agency              TEXT,               -- 소관 부처 또는 담당 부서
    summary             TEXT,
    life_cycle          TEXT,               -- 생애주기(청년 등), 쉼표 구분
    target_group        TEXT,               -- 가구 상황(저소득 등)
    interest_theme      TEXT,               -- 관심 주제(생활지원, 주거 등)
    support_cycle       TEXT,               -- 지원 주기(월, 1회성 등)
    provision_method    TEXT,               -- 제공 유형(현금지급, 현물 등)
    online_apply_yn     TEXT,
    service_url         TEXT,
    target_detail       TEXT,               -- 이하 상세 API
    selection_criteria  TEXT,
    support_content     TEXT,
    application_method  TEXT,
    contact             TEXT,
    homepage            TEXT,
    base_law            TEXT,
    last_modified       TEXT,
    is_youth            INTEGER NOT NULL,   -- 생애주기에 '청년'이 있으면 1
    detail_collected_at TEXT,               -- 상세를 받은 시각. NULL이면 아직 목록만 있음
    collected_at        TEXT NOT NULL,
    raw_json            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_welfare_services_region ON welfare_services (scope, region_sido, region_sigungu);
CREATE INDEX IF NOT EXISTS idx_welfare_services_youth ON welfare_services (is_youth);
"""
LIST_COLUMNS = ("service_id", "scope", "service_name", "region_sido", "region_sigungu", "agency", "summary", "life_cycle",
                "target_group", "interest_theme", "support_cycle", "provision_method", "online_apply_yn", "service_url",
                "last_modified", "is_youth", "collected_at", "raw_json")
DETAIL_COLUMNS = ("target_detail", "selection_criteria", "support_content", "application_method", "contact", "homepage",
                  "base_law")


class BokjiroError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _text(node: ET.Element | None) -> str | None:
    text = (node.text or "").strip() if node is not None else ""
    return text or None


def _check(root: ET.Element) -> None:
    """공공데이터포털 공통 오류(키 미등록, 한도 초과)와 서비스 오류를 예외로 바꾼다."""
    header = root.find(".//cmmMsgHeader")
    if header is not None:
        code = _text(header.find("returnReasonCode"))
        raise BokjiroError(f"{_text(header.find('returnAuthMsg'))} ({code} {_text(header.find('errMsg'))})", code)
    code = _text(root.find(".//resultCode"))
    if code not in (None, "0", "00"):
        raise BokjiroError(f"복지로 API 오류 {code}: {_text(root.find('.//resultMessage'))}", code)


def parse_list(xml_text: str) -> tuple[list[dict], int]:
    """목록 응답 → (서비스 dict 목록, 전체 건수)."""
    root = ET.fromstring(xml_text)
    _check(root)
    items = [{child.tag: _text(child) for child in item} for item in root.iter("servList")]
    return items, int(_text(root.find(".//totalCount")) or len(items))


def parse_detail(xml_text: str) -> dict:
    """상세 응답 → dict. 신청방법·문의처·근거법령처럼 여러 개인 항목은 dict 목록으로 둔다."""
    root = ET.fromstring(xml_text)
    _check(root)
    out: dict = {}
    for node in root.iter():
        if node is root or node.tag in ("resultCode", "resultMessage"):
            continue
        children = list(node)
        if children and all(len(child) == 0 for child in children) and node.tag.endswith("List"):
            out.setdefault(node.tag, []).append({child.tag: _text(child) for child in children})
        elif not children and node.tag not in out and not _inside_list(root, node):
            out[node.tag] = _text(node)
    return out


def _inside_list(root: ET.Element, node: ET.Element) -> bool:
    return any(node in list(parent) for parent in root.iter() if parent.tag.endswith("List"))


def _join(entries: list[dict] | None) -> str | None:
    """[{이름, 연락처·링크}] → '이름(링크) / 이름'. 중앙부처는 servSeDetailNm/Link, 지자체는 wlfareInfoReldNm/Cn을 쓴다."""
    parts = []
    for entry in entries or []:
        name = entry.get("servSeDetailNm") or entry.get("wlfareInfoReldNm")
        link = entry.get("servSeDetailLink") or entry.get("wlfareInfoReldCn")
        text = f"{name}({link})" if name and link and link != name else (name or link)
        if text and text not in parts:
            parts.append(text)
    return " / ".join(parts) or None


def _first(item: dict, *keys: str) -> str | None:
    return next((item[k] for k in keys if item.get(k)), None)


def list_row(scope: str, item: dict, collected_at: str) -> dict:
    """목록 한 건 → welfare_services 행. 중앙부처와 지자체는 칸 이름이 조금 다르다."""
    life = _first(item, "lifeArray", "lifeNmArray")
    agency = _first(item, "bizChrDeptNm") or " ".join(filter(None, [item.get("jurMnofNm"), item.get("jurOrgNm")])) or None
    return {
        "service_id": item["servId"], "scope": scope, "service_name": item.get("servNm") or "",
        "region_sido": item.get("ctpvNm"), "region_sigungu": item.get("sggNm"), "agency": agency,
        "summary": item.get("servDgst"), "life_cycle": life,
        "target_group": _first(item, "trgterIndvdlArray", "trgterIndvdlNmArray"),
        "interest_theme": _first(item, "intrsThemaArray", "intrsThemaNmArray"),
        "support_cycle": item.get("sprtCycNm"), "provision_method": item.get("srvPvsnNm"),
        "online_apply_yn": item.get("onapPsbltYn"), "service_url": item.get("servDtlLink"),
        "last_modified": _first(item, "lastModYmd", "svcfrstRegTs"),
        "is_youth": int("청년" in (life or "")), "collected_at": collected_at,
        "raw_json": json.dumps({"list": item}, ensure_ascii=False),
    }


def detail_row(detail: dict) -> dict:
    """상세 한 건 → 상세 칸. 값이 없는 칸은 None으로 둔다(지어내지 않는다)."""
    return {
        "target_detail": _first(detail, "tgtrDtlCn", "sprtTrgtCn"),
        "selection_criteria": detail.get("slctCritCn"),
        "support_content": detail.get("alwServCn"),
        "application_method": _first(detail, "aplyMtdCn") or _join(detail.get("applmetList")),
        "contact": _join(detail.get("inqplCtadrList")),
        "homepage": _join(detail.get("inqplHmpgReldList")),
        "base_law": _join(detail.get("baslawList")),
    }


def _get(path: str, params: dict, api_key: str, retries: int = 2) -> str:
    query = urllib.parse.urlencode({"serviceKey": api_key, **params})
    request = urllib.request.Request(f"{BASE_URL}/{path}?{query}", headers={"User-Agent": "afpk-policy-enrich/1.0"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if "<cmmMsgHeader>" in body:  # 키 오류·한도 초과는 401/429 본문에 사유가 있다
                return body
            if attempt == retries:
                raise BokjiroError(f"복지로 API HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == retries:
                raise BokjiroError(f"복지로 API 연결 실패: {exc}") from exc
        time.sleep(2.0 * (attempt + 1))
    raise BokjiroError("복지로 API 호출 실패")


def fetch_list(scope: str, api_key: str, sleep_sec: float = 0.2, limit: int | None = None) -> list[dict]:
    spec, items, page = ENDPOINTS[scope], [], 1
    while True:
        batch, total = parse_list(_get(spec["list"], {**spec["list_params"], "pageNo": page, "numOfRows": PAGE_SIZE}, api_key))
        items += batch
        if not batch or len(items) >= total or (limit and len(items) >= limit):
            return items[:limit] if limit else items
        page += 1
        time.sleep(sleep_sec)


def fetch_detail(scope: str, service_id: str, api_key: str) -> dict:
    spec = ENDPOINTS[scope]
    return parse_detail(_get(spec["detail"], {**spec["detail_params"], "servId": service_id}, api_key))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def store_list(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """목록을 넣는다. 이미 있는 서비스는 목록 칸만 갱신하고 받아 둔 상세는 지킨다."""
    updates = ", ".join(f"{c}=excluded.{c}" for c in LIST_COLUMNS if c not in ("service_id", "raw_json"))
    conn.executemany(
        f"INSERT INTO welfare_services ({','.join(LIST_COLUMNS)}) VALUES ({','.join('?' * len(LIST_COLUMNS))}) "
        f"ON CONFLICT(service_id) DO UPDATE SET {updates}, "
        "raw_json=json_set(welfare_services.raw_json, '$.list', json(json_extract(excluded.raw_json, '$.list')))",
        [tuple(row[c] for c in LIST_COLUMNS) for row in rows])


def store_detail(conn: sqlite3.Connection, service_id: str, detail: dict, collected_at: str) -> None:
    row = detail_row(detail)
    conn.execute(
        f"UPDATE welfare_services SET {', '.join(f'{c}=?' for c in DETAIL_COLUMNS)}, detail_collected_at=?, "
        "raw_json=json_set(raw_json, '$.detail', json(?)) WHERE service_id=?",
        [*(row[c] for c in DETAIL_COLUMNS), collected_at, json.dumps(detail, ensure_ascii=False), service_id])


def collect(conn: sqlite3.Connection, api_key: str | None = None, scopes=("central", "local"), details: bool = True,
            youth_first: bool = True, limit: int | None = None, sleep_sec: float = 0.2, progress=lambda m: None,
            workers: int = 1, lists: bool = True) -> dict:
    """키가 없으면 아무것도 하지 않고 skipped를 돌려준다(빌드는 계속 진행)."""
    api_key = api_key or os.getenv("BOKJIRO_API_KEY", "")
    if not api_key:
        return {"skipped": "BOKJIRO_API_KEY 없음"}
    ensure_schema(conn)
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report: dict = {}
    for scope in scopes if lists else ():  # 이어 받을 때는 목록 호출로 하루 한도를 쓰지 않는다
        items = fetch_list(scope, api_key, sleep_sec, limit)
        store_list(conn, [list_row(scope, item, collected_at) for item in items if item.get("servId")])
        conn.commit()
        report[scope] = {"listed": len(items)}
        progress(f"{scope} 목록 {len(items)}건")
    if details:
        # 청년 대상 서비스부터 받는다. 하루 한도에 걸려도 에이전트가 쓸 것이 먼저 채워진다
        order = "is_youth DESC, service_id" if youth_first else "service_id"
        pending = conn.execute(
            f"SELECT service_id, scope FROM welfare_services WHERE detail_collected_at IS NULL "
            f"AND scope IN ({','.join('?' * len(scopes))}) ORDER BY {order}", list(scopes)).fetchall()
        done = 0

        def one(task):
            try:
                return task[0], fetch_detail(task[1], task[0], api_key)
            except BokjiroError as exc:
                return task[0], exc

        # 호출 하나가 1초 넘게 걸린다(2026-11-11). 받기만 여러 개를 동시에 하고 DB에는 이 스레드에서만 쓴다
        with ThreadPoolExecutor(max_workers=workers) as pool:
            todo = pending[:limit] if limit else pending
            for start in range(0, len(todo), workers * 5):
                results = list(pool.map(one, todo[start:start + workers * 5]))
                for service_id, detail in results:
                    if isinstance(detail, BokjiroError):
                        if detail.code not in DAILY_LIMIT_CODES:
                            report.setdefault("detail_errors", []).append(f"{service_id}: {detail}")
                        continue
                    store_detail(conn, service_id, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    done += 1
                conn.commit()
                progress(f"상세 {done}/{len(pending)}")
                if any(isinstance(d, BokjiroError) and d.code in DAILY_LIMIT_CODES for _, d in results):
                    report["stopped"] = f"하루 호출 한도 초과. 상세 {len(pending) - done}건 남음"
                    break
                time.sleep(sleep_sec)
        report["details"] = {"fetched": done, "pending": len(pending) - done}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("data/youth_policy_enriched.db"))
    parser.add_argument("--scope", nargs="+", choices=list(ENDPOINTS), default=list(ENDPOINTS))
    parser.add_argument("--no-detail", action="store_true", help="목록만 받는다")
    parser.add_argument("--details-only", action="store_true", help="목록은 다시 받지 않고 남은 상세만 이어 받는다")
    parser.add_argument("--limit", type=int, help="시험용. 목록·상세를 이 건수까지만")
    parser.add_argument("--workers", type=int, default=2,
                        help="상세를 동시에 받는 수. 6으로 올리면 초당 요청 제한(23)에 걸린다")
    args = parser.parse_args()
    if not os.getenv("BOKJIRO_API_KEY"):
        parser.error("환경변수 BOKJIRO_API_KEY가 필요합니다")
    conn = sqlite3.connect(args.db)
    report = collect(conn, scopes=tuple(args.scope), details=not args.no_detail, lists=not args.details_only, limit=args.limit, workers=args.workers,
                     progress=lambda m: print(m, flush=True))
    report["table"] = dict(conn.execute("SELECT scope, COUNT(*) FROM welfare_services GROUP BY scope").fetchall())
    report["youth"] = conn.execute("SELECT COUNT(*) FROM welfare_services WHERE is_youth=1").fetchone()[0]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
