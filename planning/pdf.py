"""대시보드 HTML → PDF. 설치된 Edge·Chrome·Chromium을 창 없이 실행해 인쇄한다(추가 패키지 없음).

- 브라우저 위치는 환경변수 PDF_BROWSER로 지정할 수 있다. 없으면 흔한 설치 위치와 PATH에서 찾는다.
- 접힌 카드(<details>)는 PDF에서 펼칠 수 없으므로 모두 펼친 상태로 인쇄한다.
- 브라우저는 임시 폴더의 HTML만 읽는다. 네트워크 요청은 없다(대시보드에 외부 자원이 없음).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

NAMES = ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")
WINDOWS = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")
MAC = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
       "/Applications/Chromium.app/Contents/MacOS/Chromium")
PAGE_CSS = "<style>@page { size:A4 landscape; margin:12mm 12mm; } html { -webkit-print-color-adjust:exact; print-color-adjust:exact; }</style>"


class PdfError(RuntimeError):
    pass


def find_browser() -> Path:
    given = os.environ.get("PDF_BROWSER")
    if given:
        if Path(given).is_file():
            return Path(given)
        raise PdfError(f"PDF_BROWSER가 가리키는 파일이 없음: {given}")
    candidates = [Path(p) for p in MAC]
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(base)
        if root:
            candidates += [Path(root) / rel for rel in WINDOWS]
    for path in candidates:
        if path.is_file():
            return path
    for name in NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise PdfError("PDF를 만들 브라우저(Edge·Chrome·Chromium)를 찾지 못함. "
                   "PDF_BROWSER 환경변수로 실행 파일을 지정하거나 --format html을 쓴다")


def printable(html: str) -> str:
    """접힌 카드를 모두 펼치고 A4 가로 여백을 넣은 인쇄용 HTML."""
    html = re.sub(r"<details\b(?![^>]*\bopen\b)", "<details open", html)
    return html.replace("</head>", PAGE_CSS + "</head>", 1)


def html_to_pdf(html: str, out: Path, *, timeout: int = 120) -> Path:
    browser = find_browser()
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dashboard_pdf_", ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "dashboard.html"
        target = Path(tmp) / "dashboard.pdf"
        page.write_text(printable(html), encoding="utf-8")
        cmd = [str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
               f"--user-data-dir={Path(tmp) / 'profile'}", "--no-pdf-header-footer",
               f"--print-to-pdf={target}", page.as_uri()]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PdfError(f"브라우저가 {timeout}초 안에 끝나지 않음") from exc
        if not target.is_file() or target.stat().st_size < 1000:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise PdfError(f"브라우저가 PDF를 만들지 못함(종료 코드 {done.returncode}) {tail[0]}".strip())
        shutil.copyfile(target, out)
    return out
