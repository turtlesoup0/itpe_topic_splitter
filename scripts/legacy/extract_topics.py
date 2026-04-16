#!/usr/bin/env python3
"""
정보관리 기술사 FB반 리뷰 PDF -> 토픽별 텍스트 추출 스크립트
Phase 2: PDF 텍스트 추출 + 토픽별 분할
Phase 3: 이미지 페이지 OCR 처리
"""

import os
import re
import sys
import json
import unicodedata
import fitz  # PyMuPDF
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple
import subprocess
import tempfile

# ─── Configuration ───────────────────────────────────────────────
BASE_DIR = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료"
PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
DATA_DIR = os.path.join(PROJECT_DIR, "data")
GENS = ["19기", "20기", "21기"]

# OCR settings
OCR_LANG = "kor+eng"
TESSERACT_CMD = "/opt/homebrew/bin/tesseract"

# ─── Data Classes ────────────────────────────────────────────────
@dataclass
class TopicInfo:
    """단일 토픽 정보"""
    gen: str           # 기수 (19기/20기/21기)
    week: str          # 주차 (1주차-SW 등)
    subject: str       # 과목 (SW/DS/DB/SE/AI/CAOS/NW/경영/AL)
    session: str       # 교시 (1교시/2교시/3교시/4교시)
    q_num: int         # 문제 번호
    q_title: str       # 문제 제목
    intent: str = ""   # 출제의도
    approach: str = "" # 작성방안
    content: str = ""  # 해설 본문
    page_start: int = 0
    page_end: int = 0
    has_ocr_pages: bool = False
    source_pdf: str = ""

@dataclass
class PDFInventory:
    """PDF 파일 인벤토리"""
    path: str
    gen: str
    week: str
    subject: str
    session: str
    total_pages: int
    text_pages: int
    image_pages: int
    topics: List[TopicInfo] = field(default_factory=list)


# ─── Utility Functions ───────────────────────────────────────────
def normalize(s: str) -> str:
    """Unicode NFC 정규화"""
    return unicodedata.normalize('NFC', s)


def extract_subject_from_path(week_folder: str, filename: str) -> str:
    """폴더명/파일명에서 과목 추출"""
    combined = normalize(week_folder + " " + filename).upper()
    subjects = {
        'SW': r'\bSW\b',
        'DS': r'\bDS\b',
        'DB': r'\bDB\b',
        'SE': r'\bSE\b',
        'AI': r'\bAI\b',
        'CAOS': r'\bCAOS\b',
        'NW': r'\bNW\b',
        '경영': r'경영',
        'AL': r'\bAL\b',
        'OT': r'\bOT\b',
    }
    found = []
    for subj, pattern in subjects.items():
        if re.search(pattern, combined, re.IGNORECASE):
            found.append(subj)

    if not found:
        # 특수 케이스
        if '보안' in combined:
            return 'SE'  # 보안 -> Security Engineering
        if '멘티출제' in normalize(week_folder):
            return '전범위'
        if '자체모의' in normalize(week_folder):
            return '전범위'
        if '특강' in normalize(week_folder):
            return '특강'
        if '합반' in normalize(week_folder):
            return '전범위'
        return 'UNKNOWN'

    # 여러 과목 결합 (CAOS+NW 등)
    return '+'.join(found)


def extract_session(filename: str) -> str:
    """파일명에서 교시 추출"""
    fn = normalize(filename)
    m = re.search(r'(\d)교시', fn)
    if m:
        return f"{m.group(1)}교시"
    return "UNKNOWN"


# ─── Problem List Extraction ─────────────────────────────────────
def detect_pdf_format(doc: fitz.Document) -> str:
    """
    PDF 포맷 자동 감지
    Returns: 'standard' | 'menti' | 'inline' | 'bare'
    """
    if doc.page_count == 0:
        return 'bare'

    page1_text = doc[0].get_text() or ""
    # 줄바꿈 포함 공백 모두 제거 (PyMuPDF가 한글을 글자 단위로 분리하는 경우 대응)
    p1_collapsed = re.sub(r'\s+', '', page1_text)

    # Format C: 멘티출제 (문제 N. + 출제영역/난이도 카드)
    if '출제영역' in p1_collapsed and '난이도' in p1_collapsed and '★' in page1_text:
        return 'menti'

    # Format A: Standard (다음 문제 중 N 문제를 선택)
    if '문제중' in p1_collapsed and '선택' in p1_collapsed:
        # Check if answer content is also on page 1 (Format B: inline)
        if '출제의도' in p1_collapsed or '작성방안' in p1_collapsed:
            return 'inline'
        return 'standard'

    # Format D: bare (no problem list, topics start directly)
    return 'bare'


def extract_problem_list(doc: fitz.Document) -> List[Tuple[int, str]]:
    """
    PDF 첫 1-2페이지에서 문제 목록 추출 (포맷에 따라 다른 전략)
    Returns: [(문제번호, 문제제목), ...]
    """
    fmt = detect_pdf_format(doc)

    if fmt == 'standard':
        return _extract_standard_problems(doc)
    elif fmt == 'inline':
        return _extract_inline_problems(doc)
    elif fmt == 'menti':
        return _extract_menti_problems(doc)
    elif fmt == 'bare':
        return _extract_bare_problems(doc)
    return []


def _extract_standard_problems(doc: fitz.Document) -> List[Tuple[int, str]]:
    """Format A: 표준 문제지 (문제 목록만 있는 페이지)"""
    problems = []
    for page_idx in range(min(2, doc.page_count)):
        text = doc[page_idx].get_text()
        if not text:
            continue

        lines = text.split('\n')
        in_problem_section = False

        for line in lines:
            line_s = line.strip()
            if '문제 중' in line_s and '선택' in line_s:
                in_problem_section = True
                continue

            if in_problem_section:
                m = re.match(r'^(\d{1,2})\.\s+(.+)', line_s)
                if m:
                    num = int(m.group(1))
                    title = m.group(2).strip()
                    problems.append((num, title))

        if problems:
            break
    return problems


def _extract_inline_problems(doc: fitz.Document) -> List[Tuple[int, str]]:
    """Format B: 인라인 (문제 목록 + 바로 이어지는 해설)"""
    problems = []
    text = doc[0].get_text() or ""
    lines = text.split('\n')
    in_problem_section = False
    found_first_intent = False

    for line in lines:
        line_s = line.strip()
        if '문제 중' in line_s and '선택' in line_s:
            in_problem_section = True
            continue

        if in_problem_section:
            # 출제의도를 만나면 문제 목록 영역 끝
            if '출제의도' in line_s:
                found_first_intent = True

            if not found_first_intent:
                m = re.match(r'^(\d{1,2})\.\s+(.+)', line_s)
                if m:
                    num = int(m.group(1))
                    title = m.group(2).strip()
                    # 하위 번호(1. 개념...) 와 문제 번호 구분
                    # 문제 번호는 보통 1~13 순서
                    if not problems or num == problems[-1][0] + 1:
                        problems.append((num, title))

    # 인라인이지만 문제 목록이 안 보이면 전체 스캔
    if not problems:
        return _extract_bare_problems(doc)
    return problems


def _extract_menti_problems(doc: fitz.Document) -> List[Tuple[int, str]]:
    """Format C: 멘티출제 (문제 카드 포맷)"""
    problems = []
    for page_idx in range(doc.page_count):
        text = doc[page_idx].get_text()
        if not text or not text.strip():
            continue

        # "문제  N. 토픽명" 또는 "문제 N. 토픽명" 패턴
        # 멘티출제는 각 문제가 새 페이지 또는 카드로 시작
        # 줄바꿈으로 분리된 한글 대응: 두 패턴 순차 시도
        for pat in [
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n출\s*제|$)',
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n\s*출\s*\n?\s*제|$)',
        ]:
            matches = re.finditer(pat, text, re.DOTALL)
            for m in matches:
                num = int(m.group(1))
                title = m.group(2).strip().split('\n')[0]  # 첫 줄만
                if not any(p[0] == num for p in problems):
                    problems.append((num, title))
            if problems:
                break  # 첫 번째 패턴으로 찾았으면 두 번째 불필요

    return sorted(problems, key=lambda x: x[0])


def _extract_bare_problems(doc: fitz.Document) -> List[Tuple[int, str]]:
    """Format D: 목록 없이 바로 토픽 시작"""
    problems = []
    seen_nums = set()

    for page_idx in range(doc.page_count):
        text = doc[page_idx].get_text()
        if not text or len(text.strip()) < 30:
            continue

        lines = text.split('\n')
        for i, line in enumerate(lines):
            line_s = line.strip()
            # 페이지 상단에 "N. 토픽명" 패턴 (보통 첫 5줄 이내)
            if i < 8:
                m = re.match(r'^(\d{1,2})\.\s+(.+)', line_s)
                if m:
                    num = int(m.group(1))
                    title = m.group(2).strip()
                    # 최소 길이 및 중복 체크
                    if len(title) > 3 and num not in seen_nums:
                        # 다음 줄에 출제의도/키워드/회차 참조가 있으면 토픽 시작 신호
                        next_lines = '\n'.join(lines[i:min(i+8, len(lines))])
                        is_topic_start = any(kw in next_lines for kw in
                            ['출제의도', '작성방안', '회 ', 'Keyword', '출제빈도',
                             '출제배경', '풀이', '관리', '응용', '난이도'])
                        if is_topic_start:
                            seen_nums.add(num)
                            problems.append((num, title))

    return sorted(problems, key=lambda x: x[0])


# ─── Topic Boundary Detection ────────────────────────────────────
def find_topic_boundaries(doc: fitz.Document, problems: List[Tuple[int, str]]) -> List[dict]:
    """
    문제 목록을 기반으로 각 토픽의 시작/끝 페이지 + 텍스트 경계 찾기
    포맷에 따라 다른 전략 사용
    """
    fmt = detect_pdf_format(doc)
    problem_nums = set(p[0] for p in problems)
    problem_titles = {p[0]: p[1] for p in problems}

    if fmt == 'menti':
        return _find_menti_boundaries(doc, problems, problem_titles)

    boundaries = []

    # 시작 페이지 결정 (standard: 1페이지 스킵, inline/bare: 0부터)
    start_page = 0 if fmt in ('inline', 'bare') else 1

    for page_idx in range(start_page, doc.page_count):
        text = doc[page_idx].get_text()
        if not text or len(text.strip()) < 30:
            continue

        lines = text.split('\n')
        for line_idx, line in enumerate(lines):
            line_s = line.strip()
            m = re.match(r'^(\d{1,2})\.\s+(.+)', line_s)
            if not m:
                continue

            num = int(m.group(1))
            title_text = m.group(2).strip()

            if num not in problem_nums:
                continue

            # 출제의도/작성방안 근처 확인
            context_range = min(line_idx + 8, len(lines))
            nearby_text = '\n'.join(lines[line_idx:context_range])
            has_intent = any(kw in nearby_text for kw in ['출제의도', '작성방안'])

            is_near_top = line_idx < 10

            # 제목 유사도 체크 - 문제 목록의 제목과 매칭
            expected_title = problem_titles.get(num, "")
            # 짧은 키워드라도 포함되면 OK
            title_match = False
            if expected_title:
                # 첫 5글자 매칭 또는 핵심 단어 포함
                clean_expected = re.sub(r'[(\[（].*?[)\]）]', '', expected_title).strip()
                clean_found = re.sub(r'[(\[（].*?[)\]）]', '', title_text).strip()
                if clean_expected[:5] == clean_found[:5]:
                    title_match = True
                elif any(word in clean_found for word in clean_expected.split()[:3] if len(word) > 2):
                    title_match = True

            # 점수 기반 판정
            score = 0
            if has_intent:
                score += 10  # 출제의도가 있으면 거의 확실
            if is_near_top:
                score += 3
            if title_match:
                score += 5

            # 기존 항목과 비교
            existing = [b for b in boundaries if b['num'] == num]
            if existing:
                if score > existing[0].get('score', 0):
                    boundaries = [b for b in boundaries if b['num'] != num]
                else:
                    continue

            if score >= 3:  # 최소 점수 충족
                boundaries.append({
                    'num': num,
                    'title': problem_titles.get(num, title_text),
                    'page_idx': page_idx,
                    'line_idx': line_idx,
                    'has_intent': has_intent,
                    'is_near_top': is_near_top,
                    'score': score,
                })

    # 번호순 정렬 후 중복 최종 제거
    seen = {}
    for b in sorted(boundaries, key=lambda x: (x['num'], -x.get('score', 0))):
        if b['num'] not in seen:
            seen[b['num']] = b
    boundaries = sorted(seen.values(), key=lambda x: (x['page_idx'], x['line_idx']))

    # 끝 페이지 계산
    _assign_page_ends(doc, boundaries)

    return boundaries


def _find_menti_boundaries(doc: fitz.Document, problems: List[Tuple[int, str]],
                           problem_titles: dict) -> List[dict]:
    """멘티출제 포맷의 경계 탐지"""
    boundaries = []

    for page_idx in range(doc.page_count):
        text = doc[page_idx].get_text()
        if not text or not text.strip():
            continue

        # "문제  N." 또는 상단에 "N. 토픽명" + 출제영역
        lines = text.split('\n')
        for line_idx, line in enumerate(lines):
            line_s = line.strip()
            # 멘티출제 카드 시작: "문제  N. 토픽명"
            # 줄바꿈 분리된 한글 대응: 두 패턴 시도
            m = re.match(r'문\s*제\s+(\d{1,2})\.\s+(.+)', line_s)
            if not m:
                # "문\n제  N." 패턴: 이전 줄이 "문"이고 현재 줄이 "제  N."
                m = re.match(r'제\s+(\d{1,2})\.\s+(.+)', line_s)
                if m and line_idx > 0 and lines[line_idx - 1].strip() == '문':
                    pass  # m이 유효
                else:
                    m = None
            if m:
                num = int(m.group(1))
                title_text = m.group(2).strip()
                if not any(b['num'] == num for b in boundaries):
                    boundaries.append({
                        'num': num,
                        'title': problem_titles.get(num, title_text),
                        'page_idx': page_idx,
                        'line_idx': line_idx,
                        'has_intent': True,
                        'is_near_top': True,
                        'score': 15,
                    })

    boundaries = sorted(boundaries, key=lambda x: (x['page_idx'], x['line_idx']))
    _assign_page_ends(doc, boundaries)
    return boundaries


def _assign_page_ends(doc: fitz.Document, boundaries: List[dict]):
    """경계 목록에 page_end 할당"""
    for i, b in enumerate(boundaries):
        if i + 1 < len(boundaries):
            next_page = boundaries[i+1]['page_idx']
            # 다음 토픽이 같은 페이지면 같은 page_end
            b['page_end'] = next_page - 1 if next_page > b['page_idx'] else next_page
        else:
            b['page_end'] = doc.page_count - 1

        # 마지막 페이지가 문제지 반복이면 제외
        if b['page_end'] < doc.page_count:
            last_text = doc[b['page_end']].get_text() or ""
            if '다음 문제 중' in last_text and b['page_end'] > b['page_idx']:
                b['page_end'] -= 1


# ─── Text Extraction ─────────────────────────────────────────────
def extract_topic_text(doc: fitz.Document, boundary: dict) -> Tuple[str, str, str]:
    """
    토픽 경계 정보를 기반으로 출제의도, 작성방안, 본문 추출
    Returns: (intent, approach, content)
    """
    full_text = ""
    for page_idx in range(boundary['page_idx'], min(boundary['page_end'] + 1, doc.page_count)):
        page_text = doc[page_idx].get_text()
        if page_text and len(page_text.strip()) > 10:
            full_text += page_text + "\n\n"

    # 출제의도 추출
    intent = ""
    intent_m = re.search(r'출제의도[:：]?\s*(.+?)(?=\n\s*[-\n]|작성방안|$)', full_text, re.DOTALL)
    if intent_m:
        intent = intent_m.group(1).strip()

    # 작성방안 추출
    approach = ""
    approach_m = re.search(r'작성방안[:：]?\s*(.+?)(?=\n\s*\n|\n\s*\d+\.)', full_text, re.DOTALL)
    if approach_m:
        approach = approach_m.group(1).strip()

    # 본문 (출제의도/작성방안 이후의 내용)
    content = full_text

    return intent, approach, content


# ─── OCR Processing ──────────────────────────────────────────────
def ocr_page(doc: fitz.Document, page_idx: int) -> str:
    """단일 페이지를 이미지로 추출 후 OCR"""
    page = doc[page_idx]
    # 고해상도 렌더링
    mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better OCR
    pix = page.get_pixmap(matrix=mat)

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        pix.save(tmp.name)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [TESSERACT_CMD, tmp_path, 'stdout', '-l', OCR_LANG, '--psm', '6'],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"  OCR error page {page_idx}: {e}")
        return ""
    finally:
        os.unlink(tmp_path)


def extract_with_ocr(doc: fitz.Document, start_page: int, end_page: int) -> str:
    """텍스트 + OCR 혼합 추출"""
    full_text = ""
    for page_idx in range(start_page, min(end_page + 1, doc.page_count)):
        page_text = doc[page_idx].get_text().strip()
        if len(page_text) > 50:
            full_text += page_text + "\n\n"
        else:
            # 이미지 페이지 -> OCR
            ocr_text = ocr_page(doc, page_idx)
            if ocr_text:
                full_text += f"[OCR p{page_idx+1}]\n{ocr_text}\n\n"
    return full_text


# ─── Main Processing ─────────────────────────────────────────────
def find_review_pdfs() -> List[dict]:
    """모든 리뷰 PDF 찾기"""
    review_pdfs = []

    for gen in GENS:
        gen_path = os.path.join(BASE_DIR, gen)
        for root, dirs, files in os.walk(gen_path):
            for f in files:
                if not f.endswith('.pdf'):
                    continue
                f_nfc = normalize(f)
                if '리뷰' not in f_nfc:
                    continue
                if '복사본' in f_nfc:
                    continue
                # bak 폴더 제외
                root_nfc = normalize(root)
                if '/bak/' in root_nfc or root_nfc.endswith('/bak'):
                    continue

                full_path = os.path.join(root, f)
                # 주차 폴더 추출
                parts = root_nfc.split('/')
                week_parts = [p for p in parts if '주차' in p or '오리엔테이션' in p or '멘티출제' in p or '특강' in p or '합반' in p or '자체모의' in p or '서바이벌' in p]
                week = week_parts[-1] if week_parts else 'UNKNOWN'

                subject = extract_subject_from_path(week, f_nfc)
                session = extract_session(f_nfc)

                review_pdfs.append({
                    'path': full_path,
                    'filename': f_nfc,
                    'gen': gen,
                    'week': week,
                    'subject': subject,
                    'session': session,
                })

    return sorted(review_pdfs, key=lambda x: (x['gen'], x['week'], x['session']))


def process_single_pdf(pdf_info: dict, do_ocr: bool = False) -> List[TopicInfo]:
    """단일 리뷰 PDF 처리"""
    path = pdf_info['path']
    gen = pdf_info['gen']
    week = pdf_info['week']
    subject = pdf_info['subject']
    session = pdf_info['session']

    try:
        doc = fitz.open(path)
    except Exception as e:
        print(f"  ERROR opening {path}: {e}")
        return []

    # 1. 문제 목록 추출
    problems = extract_problem_list(doc)
    if not problems:
        print(f"  WARNING: No problem list found in {pdf_info['filename']}")
        doc.close()
        return []

    # 2. 토픽 경계 탐지
    boundaries = find_topic_boundaries(doc, problems)
    if not boundaries:
        print(f"  WARNING: No topic boundaries found in {pdf_info['filename']}")
        doc.close()
        return []

    # 3. 각 토픽별 텍스트 추출
    topics = []
    for b in boundaries:
        # 이미지 페이지 비율 체크
        image_count = 0
        for pi in range(b['page_idx'], min(b['page_end'] + 1, doc.page_count)):
            if len(doc[pi].get_text().strip()) < 50:
                image_count += 1

        has_ocr = image_count > 0

        if do_ocr and has_ocr:
            full_content = extract_with_ocr(doc, b['page_idx'], b['page_end'])
        else:
            full_content = ""
            for pi in range(b['page_idx'], min(b['page_end'] + 1, doc.page_count)):
                pt = doc[pi].get_text()
                if pt and len(pt.strip()) > 10:
                    full_content += pt + "\n\n"

        # 출제의도/작성방안 추출
        intent = ""
        approach = ""
        intent_m = re.search(r'출제의도[:：]?\s*(.+?)(?=\n.*작성방안|\n\s*\n)', full_content, re.DOTALL)
        if intent_m:
            intent = intent_m.group(1).strip().replace('\n', ' ')

        approach_m = re.search(r'작성방안[:：]?\s*(.+?)(?=\n\s*\n|\n\s*\d+\.)', full_content, re.DOTALL)
        if approach_m:
            approach = approach_m.group(1).strip().replace('\n', ' ')

        topic = TopicInfo(
            gen=gen,
            week=week,
            subject=subject,
            session=session,
            q_num=b['num'],
            q_title=b['title'],
            intent=intent,
            approach=approach,
            content=full_content,
            page_start=b['page_idx'] + 1,  # 1-based
            page_end=b['page_end'] + 1,
            has_ocr_pages=has_ocr,
            source_pdf=pdf_info['filename'],
        )
        topics.append(topic)

    doc.close()
    return topics


def process_all(do_ocr: bool = False):
    """전체 리뷰 PDF 처리"""
    review_pdfs = find_review_pdfs()
    print(f"\n{'='*60}")
    print(f"총 리뷰 PDF: {len(review_pdfs)}개")
    print(f"OCR 처리: {'활성' if do_ocr else '비활성'}")
    print(f"{'='*60}\n")

    all_topics = []
    inventory = []

    for i, pdf_info in enumerate(review_pdfs):
        print(f"[{i+1}/{len(review_pdfs)}] {pdf_info['gen']} / {pdf_info['week']} / {pdf_info['filename']}")

        topics = process_single_pdf(pdf_info, do_ocr=do_ocr)
        all_topics.extend(topics)

        # 인벤토리 요약
        inv = {
            'gen': pdf_info['gen'],
            'week': pdf_info['week'],
            'subject': pdf_info['subject'],
            'session': pdf_info['session'],
            'filename': pdf_info['filename'],
            'topics_found': len(topics),
            'topic_list': [(t.q_num, t.q_title[:50]) for t in topics],
        }
        inventory.append(inv)
        print(f"  -> {len(topics)}개 토픽 추출")

    # 결과 저장
    print(f"\n{'='*60}")
    print(f"총 추출 토픽: {len(all_topics)}개")

    # JSON 인벤토리 저장
    inv_path = os.path.join(DATA_DIR, "inventory.json")
    with open(inv_path, 'w', encoding='utf-8') as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)
    print(f"인벤토리 저장: {inv_path}")

    # 토픽 데이터 JSON 저장
    topics_data = []
    for t in all_topics:
        topics_data.append(asdict(t))
    topics_path = os.path.join(DATA_DIR, "topics.json")
    with open(topics_path, 'w', encoding='utf-8') as f:
        json.dump(topics_data, f, ensure_ascii=False, indent=2)
    print(f"토픽 데이터 저장: {topics_path}")

    return all_topics, inventory


if __name__ == "__main__":
    do_ocr = "--ocr" in sys.argv
    all_topics, inventory = process_all(do_ocr=do_ocr)

    # 요약 출력
    print(f"\n{'='*60}")
    print("요약")
    print(f"{'='*60}")
    for gen in GENS:
        gen_topics = [t for t in all_topics if t.gen == gen]
        print(f"\n{gen}: {len(gen_topics)}개 토픽")
        subjects = set(t.subject for t in gen_topics)
        for subj in sorted(subjects):
            subj_topics = [t for t in gen_topics if t.subject == subj]
            print(f"  {subj}: {len(subj_topics)}개")
