"""
토픽 경계 탐지 실패 진단 스크립트

inventory.json에서 topics_found=0 인 PDF와
잘 동작하는 PDF를 비교해서 PyMuPDF vs ODL 경계 탐지 능력을 분석
"""

import re
import time
import tempfile
from pathlib import Path
import fitz
from opendataloader_pdf import convert

# ─── 진단 대상 PDF ───────────────────────────────────────────────
# topics_found=0 인 실패 케이스 (소스 PDF)
FAILING = [
    ("16주차-멘티출제_1교시_리뷰",
     "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료/19기/주간모의/16주차-4교시 시험/16주차-멘티출제_1교시_리뷰.pdf"),
    ("16주차-멘티출제_2교시_리뷰",
     "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료/19기/주간모의/16주차-4교시 시험/16주차-멘티출제_2교시_리뷰.pdf"),
    ("16주차-멘티출제_4교시_리뷰",
     "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료/19기/주간모의/16주차-4교시 시험/16주차-멘티출제_4교시_리뷰.pdf"),
]

# topics_found=13 인 성공 케이스 (비교용)
WORKING = [
    ("SW_1교시_리뷰",
     "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료/19기/주간모의/1주차-SW/SW_1교시_리뷰.pdf"),
    ("AI_1교시_리뷰",
     "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료/19기/주간모의/7주차-AI/1교시_AI_리뷰.pdf"),
]


# ─── 기존 방식: PyMuPDF 경계 탐지 재현 ─────────────────────────
def extract_pymupdf_raw(pdf_path: str) -> dict:
    """PyMuPDF로 텍스트 추출 + 현재 시스템의 경계 탐지 재현"""
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        t = doc[i].get_text() or ""
        pages.append(t)
    doc.close()

    full_text = "\n\n[PAGE_BREAK]\n\n".join(pages)
    p1_collapsed = re.sub(r'\s+', '', pages[0] if pages else '')

    # 포맷 감지 재현 (split_and_ocr.py 로직)
    fmt = "unknown"
    if '출제영역' in p1_collapsed and '난이도' in p1_collapsed and '★' in (pages[0] if pages else ''):
        fmt = "menti"
    elif '문제중' in p1_collapsed and '선택' in p1_collapsed:
        fmt = "standard"
    else:
        fmt = "bare"

    # menti 포맷 경계 탐지 재현
    boundaries_found = []
    for pi, page_text in enumerate(pages):
        for pat in [
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n출\s*제|$)',
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n\s*출\s*\n?\s*제|$)',
        ]:
            for m in re.finditer(pat, page_text, re.DOTALL):
                num = int(m.group(1))
                title = m.group(2).strip().split('\n')[0]
                if not any(b['num'] == num for b in boundaries_found):
                    boundaries_found.append({'num': num, 'title': title, 'page': pi + 1})

    # standard 포맷 경계 탐지 재현
    std_boundaries = []
    for pi, page_text in enumerate(pages[1:], start=1):  # skip page 1
        lines = page_text.split('\n')
        for li, line in enumerate(lines):
            m = re.match(r'^(\d{1,2})\.\s+(.+)', line.strip())
            if m:
                ctx = '\n'.join(lines[li:min(li+8, len(lines))])
                if any(kw in ctx for kw in ['출제의도', '작성방안']):
                    num = int(m.group(1))
                    if not any(b['num'] == num for b in std_boundaries):
                        std_boundaries.append({'num': num, 'title': m.group(2), 'page': pi + 1})

    return {
        "pages": len(pages),
        "fmt": fmt,
        "p1_collapsed_sample": p1_collapsed[:200],
        "menti_boundaries": boundaries_found,
        "std_boundaries": std_boundaries,
        "p1_raw_sample": pages[0][:500] if pages else "",
    }


# ─── ODL 방식: markdown 파싱으로 경계 탐지 ─────────────────────
def extract_odl_boundaries(pdf_path: str) -> dict:
    """ODL markdown 출력에서 topic 경계 탐지"""
    with tempfile.TemporaryDirectory() as tmpdir:
        start = time.perf_counter()
        convert(
            input_path=pdf_path,
            output_dir=tmpdir,
            format="markdown",
            quiet=True,
            reading_order="xycut",
        )
        elapsed = time.perf_counter() - start

        md_files = [f for f in Path(tmpdir).rglob("*.md") if f.is_file()]
        if not md_files:
            return {"error": "no markdown output", "boundaries": []}

        md_text = md_files[0].read_text(encoding="utf-8", errors="ignore")

    # ODL markdown에서 경계 탐지 전략들
    boundaries = []

    # 전략 1: "## N. 토픽명" 패턴 (heading으로 감지된 경우)
    for m in re.finditer(r'^#{1,4}\s+(\d{1,2})\.\s+(.+)', md_text, re.MULTILINE):
        num = int(m.group(1))
        title = m.group(2).strip()
        if not any(b['num'] == num for b in boundaries):
            boundaries.append({'num': num, 'title': title, 'method': 'heading'})

    # 전략 2: "문제 N. 토픽명" 패턴 (menti 포맷의 카드 헤더)
    for m in re.finditer(r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n|$)', md_text):
        num = int(m.group(1))
        title = m.group(2).strip()
        if not any(b['num'] == num for b in boundaries):
            boundaries.append({'num': num, 'title': title, 'method': 'menti_card'})

    # 전략 3: 표 셀에서 토픽 번호 (137회 기출 포맷)
    # |01|토픽명|...|
    for m in re.finditer(r'^\|(\d{1,2})\|([^|]+)\|', md_text, re.MULTILINE):
        num = int(m.group(1))
        title = m.group(2).strip()
        if len(title) > 3 and not any(b['num'] == num for b in boundaries):
            boundaries.append({'num': num, 'title': title, 'method': 'table_cell'})

    return {
        "elapsed_sec": round(elapsed, 3),
        "md_length": len(md_text),
        "boundaries": sorted(boundaries, key=lambda x: x['num']),
        "md_preview": md_text[:800],
    }


# ─── 진단 실행 ──────────────────────────────────────────────────
def diagnose(label, pdf_path):
    print(f"\n{'═'*70}")
    print(f"  {label}")
    print(f"  {Path(pdf_path).name}")
    print(f"{'═'*70}")

    # PyMuPDF
    print("\n[1] PyMuPDF 분석")
    r = extract_pymupdf_raw(pdf_path)
    print(f"  페이지수: {r['pages']}")
    print(f"  감지 포맷: {r['fmt']}")
    print(f"  menti 경계 탐지: {len(r['menti_boundaries'])}개 → {[(b['num'], b['title'][:20]) for b in r['menti_boundaries']]}")
    print(f"  standard 경계 탐지: {len(r['std_boundaries'])}개 → {[(b['num'], b['title'][:20]) for b in r['std_boundaries']]}")
    print(f"\n  [1페이지 원문 처음 500자]")
    print("  " + r['p1_raw_sample'][:500].replace('\n', '\n  '))
    print(f"\n  [p1_collapsed 처음 200자]")
    print("  " + r['p1_collapsed_sample'][:200])

    # ODL
    print(f"\n[2] ODL markdown 분석")
    odl = extract_odl_boundaries(pdf_path)
    if "error" in odl:
        print(f"  ERROR: {odl['error']}")
    else:
        print(f"  처리시간: {odl['elapsed_sec']}s | markdown 크기: {odl['md_length']:,} chars")
        print(f"  경계 탐지: {len(odl['boundaries'])}개")
        for b in odl['boundaries']:
            print(f"    Q{b['num']:02d} [{b['method']:12s}]: {b['title'][:50]}")
        print(f"\n  [ODL markdown 처음 800자]")
        print("  " + odl['md_preview'][:800].replace('\n', '\n  '))


def run():
    print("=" * 70)
    print("토픽 경계 탐지 실패 진단: PyMuPDF vs ODL")
    print("=" * 70)

    print("\n\n### 실패 케이스 (inventory에서 topics_found=0)")
    for label, path in FAILING:
        diagnose(label, path)

    print("\n\n### 성공 케이스 (비교 기준)")
    for label, path in WORKING:
        diagnose(label, path)

    # 요약
    print(f"\n\n{'═'*70}")
    print("요약 비교표")
    print(f"{'═'*70}")
    print(f"{'PDF':<30} {'PyMuPDF 포맷':>12} {'PyMuPDF 탐지':>12} {'ODL 탐지':>10}")
    print(f"{'─'*70}")
    for label, path in FAILING + WORKING:
        r = extract_pymupdf_raw(path)
        odl = extract_odl_boundaries(path)
        mupdf_count = max(len(r['menti_boundaries']), len(r['std_boundaries']))
        odl_count = len(odl.get('boundaries', []))
        status = "❌ 실패" if mupdf_count == 0 else "✅ 성공"
        odl_status = "✅" if odl_count > 1 else ("⚠" if odl_count == 1 else "❌")
        print(f"{label:<30} {r['fmt']:>12} {status:>12} {odl_status} {odl_count}개")


if __name__ == "__main__":
    run()
