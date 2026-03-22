#!/usr/bin/env python3
"""
ODL 기반 FB반 리뷰 PDF 분할 스크립트

기존 split_and_ocr.py 의 한계:
  - PyMuPDF가 한글을 글자 단위(문\n제\n영\n역)로 분절
  - 페이지별 regex 스캔으로 경계를 추론 → 복잡한 워크어라운드 필요
  - 이미지 페이지(sparse) 완전 스킵

이 스크립트의 접근:
  1. ODL JSON으로 PDF를 파싱 → element 단위(type + page number + content)
  2. heading element에서 토픽 경계 + 정확한 페이지 번호를 직접 추출
  3. fitz(PyMuPDF)로 해당 페이지 범위를 PDF로 분할

사용법:
  python3 split_odl.py --single <path>        # 단일 PDF
  python3 split_odl.py --dry-run              # 전체 dry-run
  python3 split_odl.py                        # 전체 처리
"""

import os
import re
import sys
import json
import unicodedata
import tempfile
from collections import Counter
from pathlib import Path
from typing import List, Optional
from datetime import datetime

import fitz  # PyMuPDF (PDF 분할용)
from opendataloader_pdf import convert as odl_convert

# ─── Configuration ────────────────────────────────────────────────
BASE_DIR = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료"
PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
SPLIT_DIR = os.path.join(PROJECT_DIR, "split_pdfs_odl")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
GENS = ["19기"]  # 테스트용, 전체는 ["19기", "20기", "21기"]


# ─── Helpers ──────────────────────────────────────────────────────
def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def safe_filename(s: str, max_len: int = 80) -> str:
    s = nfc(s)
    s = re.sub(r'[/\\:*?"<>|]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].rstrip()


def extract_subject(week: str, filename: str) -> str:
    combined = nfc(week + " " + filename).upper()
    # (?<![A-Z]): 대문자 앞에 다른 대문자 없음 (HDFS 등 오탐 방지)
    # (?![A-Z0-9]): 뒤에 대문자·숫자 없음 (_포함 비대문자는 OK → DS_, DS 1교시 모두 매칭)
    mapping = [
        ("SW", r"(?<![A-Z])SW(?![A-Z0-9])"), ("DS", r"(?<![A-Z])DS(?![A-Z0-9])"),
        ("DB", r"(?<![A-Z])DB(?![A-Z0-9])"), ("SE", r"(?<![A-Z])SE(?![A-Z0-9])"),
        ("AI", r"(?<![A-Z])AI(?![A-Z0-9])"), ("CAOS", r"(?<![A-Z])CAOS(?![A-Z0-9])"),
        ("NW", r"(?<![A-Z])NW(?![A-Z0-9])"), ("경영", r"경영"),
        ("AL", r"(?<![A-Z])AL(?![A-Z0-9])"), ("OT", r"(?<![A-Z])OT(?![A-Z0-9])"),
    ]
    found = [name for name, pat in mapping if re.search(pat, combined, re.IGNORECASE)]
    if not found:
        for kw, subj in [("보안", "SE"), ("멘티출제", "전범위"), ("자체모의", "전범위"),
                          ("합반", "전범위"), ("특강", "특강"), ("서바이벌", "특강")]:
            if kw in nfc(week):
                return subj
        return "ETC"
    return "+".join(found)


def extract_session(filename: str) -> str:
    m = re.search(r"(\d)교시", nfc(filename))
    return f"{m.group(1)}교시" if m else "0교시"


# ─── ODL JSON 파싱 ────────────────────────────────────────────────
def collect_elements(node: dict, results: list):
    """ODL JSON 노드에서 type/page/content 를 재귀적으로 수집"""
    t = node.get("type", "?")
    pg = node.get("page number")
    content = node.get("content", "").strip()

    if content and pg is not None:
        results.append({"type": t, "page": pg, "content": content})

    for kid in node.get("kids", []):
        collect_elements(kid, results)


def parse_odl_json(pdf_path: str) -> tuple[list, int]:
    """
    ODL로 PDF를 JSON 변환하고 elements를 반환
    sparse(이미지) 페이지는 PyMuPDF OCR로 보강
    Returns: (elements, total_pages)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        odl_convert(
            input_path=pdf_path,
            output_dir=tmpdir,
            format="json",
            quiet=True,
            reading_order="xycut",
        )
        jfiles = [f for f in Path(tmpdir).rglob("*.json") if f.is_file()]
        if not jfiles:
            return [], 0
        data = json.loads(jfiles[0].read_text(encoding="utf-8"))

    elements = []
    for kid in data.get("kids", []):
        collect_elements(kid, elements)

    total_pages = data.get("number of pages", 0)

    # 이미지 페이지 OCR 보강
    elements = _ocr_sparse_pages(pdf_path, elements, total_pages)

    return elements, total_pages


# ODL이 추출 못한 이미지 페이지에서 경계 신호 탐지용 패턴
_OCR_끝_PAT  = re.compile(r'[\u201c\u201d"\'"]?끝[\u201c\u201d"\'"]?\s*$')
_OCR_NUM_PAT = re.compile(r'^(\d{1,2})\.\s+(.{5,})')
_OCR_ROMAN_I_PAT = re.compile(r'^I\.\s+.{8,}')
_OCR_MENTI_PAT = re.compile(r'^문\s*제\s+\d{1,2}\.\s+.{5,}', re.DOTALL)
_OCR_Q_KEYWORDS = re.compile(
    r"설명하시오|논하시오|서술하시오|비교하시오|구분하시오|기술하시오"
    r"|설명하고|논하고|비교하고"
)


def _ocr_sparse_pages(pdf_path: str, elements: list, total_pages: int) -> list:
    """
    의미 있는 element가 없는 '이미지 페이지'에 PyMuPDF Tesseract OCR을 적용해
    경계 탐지에 필요한 신호(끝, 번호 제목, I. 토픽, 문제N., 질문)를 보강한다.

    기존 한계: 반복 헤더만 있는 페이지를 sparse로 인식 못함 (element > 0)
    개선: 반복 헤더를 제외하고 의미 있는 element 수로 sparse 판정

    PyMuPDF 1.18+ 의 Page.get_textpage_ocr() 사용 (Tesseract 필요).
    OCR 실패 시 조용히 원본 elements 반환.
    """
    if total_pages == 0:
        return elements

    # ── 반복 헤더 감지: 40%+ 페이지에 등장하는 content ──────────────
    content_pages: dict[str, set] = {}
    for e in elements:
        c = e["content"].strip()
        if c and len(c) > 5:
            content_pages.setdefault(c, set()).add(e["page"])
    rep_threshold = max(total_pages * 0.4, 5)
    repeated_headers = {c for c, pgs in content_pages.items()
                        if len(pgs) >= rep_threshold}

    # ── 의미 있는 element 수 (반복 헤더 + ignore 패턴 제외) ─────────
    _ignore = re.compile(r'^\d+$|Copyright|FB\d{2}|주간모의')
    page_counts: dict[int, int] = {}
    for e in elements:
        c = e["content"].strip()
        if c in repeated_headers:
            continue
        if not _ignore.search(c) and len(c) > 3:
            page_counts[e["page"]] = page_counts.get(e["page"], 0) + 1

    sparse = [pg for pg in range(1, total_pages + 1)
              if page_counts.get(pg, 0) == 0]
    if not sparse:
        return elements

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return elements

    ocr_extras: list = []
    for pg in sparse:
        try:
            page = doc[pg - 1]
            tp = page.get_textpage_ocr(language="kor+eng", dpi=200, full=False)
            text = page.get_text(textpage=tp)
        except Exception:
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) < 2:
                continue
            # 반복 헤더 스킵
            if line in repeated_headers:
                continue

            # "끝" 마커 (짧은 줄에서만 → 오탐 방지)
            if _OCR_끝_PAT.match(line) or (len(line) <= 6 and '끝' in line):
                ocr_extras.append({"type": "paragraph", "page": pg,
                                   "content": line, "source": "ocr"})
            # "I. 토픽명" (로마 숫자 토픽 시작)
            elif _OCR_ROMAN_I_PAT.match(line):
                ocr_extras.append({"type": "heading", "page": pg,
                                   "content": line, "source": "ocr"})
            # "문 제 N. 질문" (시험문제 형식)
            elif _OCR_MENTI_PAT.match(line):
                ocr_extras.append({"type": "paragraph", "page": pg,
                                   "content": line, "source": "ocr"})
            # 번호 제목 "N. 제목..."
            elif _OCR_NUM_PAT.match(line):
                ocr_extras.append({"type": "heading", "page": pg,
                                   "content": line, "source": "ocr"})
            # 질문 키워드 포함 긴 줄 (토픽 질문문)
            elif len(line) > 15 and _OCR_Q_KEYWORDS.search(line):
                ocr_extras.append({"type": "paragraph", "page": pg,
                                   "content": line, "source": "ocr"})

    doc.close()

    if ocr_extras:
        print(f"  [OCR] {len(sparse)}개 이미지 페이지 → "
              f"{len(ocr_extras)}개 element 추가")

    return elements + ocr_extras


# ─── 토픽 경계 탐지 ───────────────────────────────────────────────

# 2교시 서술형 문제에서 토픽 제목임을 나타내는 키워드
_Q_KEYWORDS = re.compile(
    r"설명하시오|논하시오|서술하시오|비교하시오|구분하시오|기술하시오"
    r"|설명하고|논하고|비교하고|이다\.|하시오\.|있다\."
)

def _heading_priority(content: str, page: int = 0) -> int:
    """
    heading 이 토픽 시작점일 가능성 점수 (높을수록 토픽 시작 가능성 높음)
    서브섹션 오탐 방지용
    """
    score = 0
    if _Q_KEYWORDS.search(content):
        score += 10   # 서술형 질문 키워드 포함
    if len(content) > 50:
        score += 5    # 긴 제목은 토픽 제목일 가능성 높음
    if len(content) > 20:
        score += 2
    return score


_IGNORE_PAT = re.compile(r'^\d+$|Copyright|FB\d{2}|주간모의|교시|^※|^다음 문제|문제를 선택')
_끝_PAT = re.compile(r'^[\u201c\u201d"]?끝[\u201c\u201d"]?\s*$')

# 기출풀이 서브분리용 패턴 (로마자 개요체: I. 주제 / II. 세부 / III. ...)
_ROMAN_I_PAT  = re.compile(r'^I\.\s+.{8,}')
_ROMAN_II_PAT = re.compile(r'^(II|III|IV|V)\.\s+')


def _교시_cover_pages(elements: list) -> list:
    """
    시험문제 표지 페이지 탐지.
    '국가기술자격 기술사 시험문제' 또는 '시험시간' 등이 포함된 페이지를
    교시 경계로 반환 (단, 첫 번째 표지 이후 10페이지 초과 간격인 것만).
    """
    candidate_pages = sorted(set(
        e["page"] for e in elements
        if "시험문제" in e["content"] or "시험시간" in e["content"]
    ))
    if not candidate_pages:
        return []
    # 첫 번째 표지(p1~) 이후, 직전 표지와 10페이지 이상 떨어진 것만 교시 경계
    covers = [candidate_pages[0]]
    for p in candidate_pages[1:]:
        if p - covers[-1] > 10:
            covers.append(p)
    return covers[1:]  # 문서 첫 표지 제외, 2교시+ 표지만 반환


def _sub_split_by_roman_I(elements: list, start_page: int, end_page: int,
                           skip: set) -> list:
    """
    '끝' 섹션 [start_page, end_page] 내에서 'I.' 로마자 헤더로 서브 토픽 분리.

    기술사 답안 형식: 각 토픽 답안은 'I. 개요' → 'II./III. 세부'/'가./나./다.'
    하나의 답안 내에서 'I.'은 정확히 한 번만 등장하므로
    섹션 내 복수의 'I.' = 복수의 토픽.

    skip: repeated_headings (문서 전체 반복 헤더) — 제목 추출 시 제외

    Returns: [{'page': int, 'title': str}, ...]  비어있으면 서브분리 없음
    """
    sec_elems = sorted(
        [e for e in elements if start_page <= e["page"] <= end_page],
        key=lambda x: x["page"],
    )

    sub_starts: list = []
    seen_pages: set = set()

    for e in sec_elems:
        c = e["content"].strip()
        if c in skip or _IGNORE_PAT.search(c):
            continue
        if _ROMAN_I_PAT.match(c):
            page = e["page"]
            if page not in seen_pages:
                seen_pages.add(page)
                title = c[3:].strip()[:70]  # "I. " 제거
                sub_starts.append({"page": page, "title": title})

    return sub_starts


def _끝_boundaries(elements: list, total_pages: int, session: str = "") -> list:
    """
    2교시 형식: 각 토픽 끝에 "끝" 마커로 구분.
    섹션 = prev_끝+1 ~ curr_끝 (부가자료는 curr_끝 이후 ~ next_토픽_start-1 도 포함)
    제목은 섹션 내 첫 번째 유효 heading/paragraph 에서 추출.
    """
    # 1교시는 단답형 → 끝 포맷 미적용, standard/menti 탐지 사용
    if "1교시" in session:
        return []

    # ODL 네이티브(source != "ocr") "끝" 마커 수 확인
    # OCR만으로 탐지된 "끝"는 오탐 가능성이 높아 단독으로 끝 포맷을 확정하지 않음
    native_끝_count = sum(
        1 for e in elements
        if e.get("source") != "ocr" and _끝_PAT.match(e["content"].strip())
    )
    if native_끝_count < 2:
        return []

    끝_pages = sorted(set(
        e["page"] for e in elements if _끝_PAT.match(e["content"].strip())
    ))
    if not 끝_pages:
        return []

    # 반복 등장하는 문서 헤더/푸터 탐지 → 제목 추출 시 제외
    # (total_pages * 15% 이상 페이지에 등장하는 heading은 반복 헤더로 간주)
    heading_counts = Counter(
        e["content"].strip() for e in elements if e["type"] == "heading"
    )
    repeat_threshold = max(3, total_pages * 0.15)
    repeated_headings = {c for c, n in heading_counts.items() if n >= repeat_threshold}

    # 섹션 시작: 1, 이전_끝+1, ...
    # 섹션 끝: 각 끝_page
    # 마지막 "끝" 이후 잔여 페이지가 2+ 이면 별도 섹션으로 추가
    section_starts = [1] + [p + 1 for p in 끝_pages]
    section_ends   = list(끝_pages)
    if total_pages - 끝_pages[-1] >= 2:
        section_ends.append(total_pages)
    else:
        section_starts = section_starts[:-1]  # 잔여 없음 → 마지막 start 제거

    boundaries = []
    for i, (start, end) in enumerate(zip(section_starts, section_ends)):
        title = None
        for e in elements:
            if not (start <= e["page"] <= end):
                continue
            c = e["content"].strip()
            if _끝_PAT.match(c) or _IGNORE_PAT.search(c) or len(c) < 5:
                continue
            if c in repeated_headings:  # 문서 전체 반복 헤더 → 스킵
                continue
            # "N. 내용" 형식
            m = re.match(r'^\d{1,2}\.\s+(.+)', c)
            if m:
                title = m.group(1).strip()[:70]
                break
            # "- 내용" 형식 (충분히 길 때만)
            m2 = re.match(r'^-\s+(.+)', c)
            if m2 and len(m2.group(1)) > 10:
                title = m2.group(1).strip()[:70]
                break
            # heading 타입이면 그냥 사용
            if e["type"] == "heading" and len(c) > 10:
                title = c[:70]
                break
        if not title:
            title = f"토픽{i + 1}"
        boundaries.append({
            "num": i + 1, "title": title,
            "page": start, "page_start": start, "page_end": end,
            "fmt": "끝",
        })

    # ── 교시 표지 페이지 기반 강제 분리 ────────────────────────────
    # '시험문제/시험시간' 페이지가 섹션 중간에 있으면 그 지점에서 강제 분리
    교시_covers = _교시_cover_pages(elements)
    if 교시_covers:
        expanded: list = []
        for b in boundaries:
            splits = [p for p in 교시_covers
                      if b["page_start"] < p <= b["page_end"]]
            if not splits:
                expanded.append(b)
                continue
            prev = b["page_start"]
            for sp in sorted(splits):
                if sp > prev:
                    expanded.append(dict(b, page=prev, page_start=prev,
                                         page_end=sp - 1, title=b["title"]))
                prev = sp
            expanded.append(dict(b, page=prev, page_start=prev,
                                 page_end=b["page_end"], title="시험문제"))
        boundaries = expanded

    # ── 섹션 내 복수 토픽 분리: I. → II./III. → I. 패턴 ──────────
    # 각 '끝' 섹션 안에서 로마숫자 I.이 재등장하면 새 토픽으로 분리
    final: list = []
    num = 1
    for b in boundaries:
        subs = _sub_split_by_roman_I(elements, b["page_start"], b["page_end"],
                                     skip=repeated_headings)
        if len(subs) <= 1:
            # 서브분리 없음: 제목만 I. 에서 업데이트 (더 나은 제목이면)
            if subs and len(subs[0]["title"]) > len(b["title"]):
                b["title"] = subs[0]["title"]
            b["num"] = num; num += 1
            final.append(b)
        else:
            # 복수 토픽 → 분리
            # 첫 I. 이전의 페이지(시험문제 표지 등)는 첫 서브섹션에 포함
            for j, sub in enumerate(subs):
                sub_start = b["page_start"] if j == 0 else sub["page"]
                sub_end = subs[j + 1]["page"] - 1 if j + 1 < len(subs) else b["page_end"]
                final.append({
                    "num": num, "title": sub["title"],
                    "page": sub_start, "page_start": sub_start,
                    "page_end": sub_end, "fmt": b["fmt"],
                })
                num += 1
    return final


def detect_boundaries(elements: list, total_pages: int, session: str = "") -> list:
    """
    ODL elements에서 토픽 경계(시작 페이지)를 탐지

    전략:
    0. 끝 포맷: "끝" 마커가 있으면 최우선 사용 (2교시 형식)
    1. menti 포맷: "문 제 N. 토픽명" heading이 ≥3개이면 menti 확정
       → menti heading 만 사용 (서브섹션 오탐 방지)
    2. standard 포맷: "N. 토픽명" heading 탐지
       → 같은 번호가 여러 개면 우선순위(질문 키워드/길이)가 높은 것 선택
    3. dash 포맷: "- 섹션명" heading 만 있는 특수 포맷
       → 페이지 첫 등장 섹션 기준으로 추출

    Returns: [{'num': int, 'title': str, 'page_start': int, 'page_end': int}]
    """
    # ── 0. 끝 포맷 ────────────────────────────────────────────────
    끝_result = _끝_boundaries(elements, total_pages, session)
    if 끝_result:
        return 끝_result

    headings = [e for e in elements if e["type"] == "heading"]

    # ── 1. menti 포맷 ────────────────────────────────────────────
    menti_pat = re.compile(r"^문\s*제\s+(\d{1,2})\.\s+(.+)", re.DOTALL)
    menti_hits = []
    for h in headings:
        m = menti_pat.match(h["content"])
        if m:
            num = int(m.group(1))
            title = m.group(2).strip().split("\n")[0]
            menti_hits.append({"num": num, "title": title, "page": h["page"]})

    # 최소 3개 이상이어야 menti 포맷으로 확정 (1~2개는 문서 내 삽입 예시일 수 있음)
    if len(menti_hits) >= 3:
        seen: dict[int, dict] = {}
        for b in menti_hits:
            if b["num"] not in seen:
                seen[b["num"]] = b
        boundaries = sorted(seen.values(), key=lambda x: x["page"])
        fmt = "menti"

    else:
        # ── 2. standard 포맷 ──────────────────────────────────────
        std_pat = re.compile(r"^(\d{1,2})\.\s+(.+)")
        # 같은 번호 → 우선순위가 가장 높은 것 선택
        candidates: dict[int, dict] = {}
        for h in headings:
            m = std_pat.match(h["content"])
            if not m:
                continue
            num = int(m.group(1))
            if not (1 <= num <= 16):
                continue
            title = m.group(2).strip()
            priority = _heading_priority(h["content"], h["page"])
            existing = candidates.get(num)
            if existing is None or priority > existing["priority"]:
                candidates[num] = {
                    "num": num, "title": title,
                    "page": h["page"], "priority": priority,
                }

        boundaries = sorted(candidates.values(), key=lambda x: x["page"])

        # ── same-page dedup: 같은 page에 두 경계 → 우선순위 높은 것만 유지 ──
        deduped: list = []
        for b in boundaries:
            if deduped and deduped[-1]["page"] == b["page"]:
                if b["priority"] > deduped[-1]["priority"]:
                    deduped[-1] = b   # 현재가 더 높으면 교체
                # else: 현재를 버림
            else:
                deduped.append(b)
        boundaries = deduped
        fmt = "standard"

        # ── 3. dash 포맷 폴백 (번호 없는 - 섹션명 형식) ─────────
        if not boundaries:
            dash_pat = re.compile(r"^-\s+(.+)")
            seen_pages: set = set()
            dash_boundaries = []
            for h in headings:
                m = dash_pat.match(h["content"])
                if m and h["page"] not in seen_pages:
                    title = m.group(1).strip()
                    # 짧은 메타 heading 제외 (저작권, 날짜 등)
                    if len(title) > 8:
                        seen_pages.add(h["page"])
                        dash_boundaries.append({
                            "num": len(dash_boundaries) + 1,
                            "title": title,
                            "page": h["page"],
                        })
            boundaries = dash_boundaries
            fmt = "dash"

    if not boundaries:
        return []

    # ── page_end 계산 ──────────────────────────────────────────────
    for i, b in enumerate(boundaries):
        if i + 1 < len(boundaries):
            b["page_end"] = boundaries[i + 1]["page"] - 1
        else:
            b["page_end"] = total_pages
        if b["page_end"] < b["page"]:
            b["page_end"] = b["page"]
        b["page_start"] = b["page"]
        b["fmt"] = fmt

    return boundaries


# ─── PDF 분할 ─────────────────────────────────────────────────────
def split_pdf(source_path: str, boundaries: list, output_dir: str,
              gen: str, week: str, subject: str, session: str) -> list:
    """
    탐지된 경계 기준으로 PDF를 토픽별 파일로 분할
    """
    doc = fitz.open(source_path)
    results = []

    for b in boundaries:
        sp = b["page_start"] - 1  # 0-indexed
        ep = min(b["page_end"] - 1, doc.page_count - 1)

        if sp > ep or sp < 0:
            continue

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=sp, to_page=ep)

        topic_name = safe_filename(b["title"], max_len=60)
        fname = (f"{gen}_{safe_filename(week, 20)}_{subject}"
                 f"_{session}_Q{b['num']:02d}_{topic_name}.pdf")
        out_path = os.path.join(output_dir, fname)

        new_doc.save(out_path)
        new_doc.close()

        img_pages = sum(
            1 for pi in range(sp, ep + 1)
            if len((doc[pi].get_text() or "").strip()) < 50
        )

        results.append({
            "filename": fname,
            "path": out_path,
            "gen": gen, "week": week, "subject": subject, "session": session,
            "q_num": b["num"], "q_title": b["title"],
            "pages": ep - sp + 1,
            "image_pages": img_pages,
            "fmt": b.get("fmt", "?"),
            "page_start": b["page_start"],
            "page_end": b["page_end"],
        })

    doc.close()
    return results


# ─── PDF 탐색 ─────────────────────────────────────────────────────
def find_review_pdfs() -> list:
    pdfs = []
    for gen in GENS:
        gen_path = os.path.join(BASE_DIR, gen)
        for root, dirs, files in os.walk(gen_path):
            for f in files:
                if not f.endswith(".pdf"):
                    continue
                fn = nfc(f)
                rn = nfc(root)
                if "리뷰" not in fn or "복사본" in fn:
                    continue
                full = os.path.join(root, f)
                parts = rn.split("/")
                week_parts = [p for p in parts if any(kw in p for kw in
                    ["주차", "오리엔테이션", "멘티출제", "특강", "합반", "자체모의", "서바이벌"])]
                week = nfc(week_parts[-1]) if week_parts else "UNKNOWN"
                pdfs.append({
                    "path": full, "filename": fn, "gen": gen, "week": week,
                    "subject": extract_subject(week, fn),
                    "session": extract_session(fn),
                })
    return sorted(pdfs, key=lambda x: (x["gen"], x["week"], x["session"]))


# ─── 메인 파이프라인 ───────────────────────────────────────────────
def run_pipeline(dry_run: bool = False, single_path: str = None):
    os.makedirs(SPLIT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    if single_path:
        fn = nfc(os.path.basename(single_path))
        parts = nfc(single_path).split("/")
        # FB반 자료/22기/3_DS/... 구조에서 gen/week 추출
        base_idx = next((i for i, p in enumerate(parts) if "FB반 자료" in p), None)
        if base_idx is not None and base_idx + 2 < len(parts):
            gen = parts[base_idx + 1]
            week = parts[base_idx + 2]
        else:
            gen, week = "single", "single"
        pdfs = [{"path": single_path, "filename": fn, "gen": gen,
                 "week": week, "subject": extract_subject(week, fn),
                 "session": extract_session(fn)}]
    else:
        pdfs = find_review_pdfs()

    print(f"\n{'='*70}")
    print(f" ODL 기반 FB반 리뷰 PDF 분할 파이프라인")
    print(f" 대상: {len(pdfs)}개 | Dry-run: {'ON' if dry_run else 'OFF'}")
    print(f" 출력: {SPLIT_DIR}")
    print(f"{'='*70}\n")

    all_results = []
    failed = []
    total_topics = 0

    for i, pdf in enumerate(pdfs):
        label = f"[{i+1}/{len(pdfs)}] {pdf['gen']}/{pdf['week']}/{pdf['filename']}"
        print(label)

        try:
            elements, total_pages = parse_odl_json(pdf["path"])
        except Exception as e:
            print(f"  ✗ ODL 파싱 실패: {e}")
            failed.append({"pdf": pdf["filename"], "error": str(e)})
            continue

        if not elements:
            print(f"  ✗ ODL 출력 없음")
            failed.append({"pdf": pdf["filename"], "error": "no elements"})
            continue

        boundaries = detect_boundaries(elements, total_pages, pdf.get("session", ""))

        if not boundaries:
            print(f"  ✗ 경계 미탐지 (elements={len(elements)}, pages={total_pages})")
            failed.append({"pdf": pdf["filename"], "error": "no boundaries"})
            continue

        fmt = boundaries[0].get("fmt", "?")
        summary = [(b["num"], f"p{b['page_start']}-{b['page_end']}") for b in boundaries]
        print(f"  포맷: {fmt} | 페이지: {total_pages} | 경계: {len(boundaries)}개 → {summary}")

        total_topics += len(boundaries)

        if dry_run:
            continue

        out_dir = os.path.join(SPLIT_DIR, pdf["gen"], safe_filename(pdf["week"], 30))
        os.makedirs(out_dir, exist_ok=True)

        results = split_pdf(
            pdf["path"], boundaries, out_dir,
            pdf["gen"], pdf["week"], pdf["subject"], pdf["session"]
        )
        all_results.extend(results)
        print(f"  → {len(results)}개 토픽 PDF 생성")

    # ── 리포트 ──────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f" 완료: 처리 {len(pdfs)}개 | 토픽 {total_topics}개 | 실패 {len(failed)}개")
    if failed:
        for f in failed:
            print(f"   ✗ {f['pdf']}: {f['error']}")

    if not dry_run:
        report = {
            "timestamp": datetime.now().isoformat(),
            "total_pdfs": len(pdfs),
            "total_topics": total_topics,
            "failed": failed,
            "results": all_results,
        }
        rp = os.path.join(DATA_DIR, "split_odl_report.json")
        with open(rp, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
        print(f" 리포트: {rp}")

    return all_results


# ─── CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    single = None
    if "--single" in sys.argv:
        idx = sys.argv.index("--single")
        if idx + 1 < len(sys.argv):
            single = sys.argv[idx + 1]

    run_pipeline(dry_run=dry_run, single_path=single)
