#!/usr/bin/env python3
"""
정보관리 기술사 FB반 리뷰 PDF 분할 + OCR 파이프라인

기능:
1. 합쳐진 리뷰 PDF를 토픽별 개별 PDF로 분리
2. 이미지 페이지에 OCR 적용 (tesseract kor+eng)
3. OCR 적용된 검색 가능 PDF 출력

사용법:
  python3 split_and_ocr.py                    # 전체 처리 (OCR 없이 분할만)
  python3 split_and_ocr.py --ocr              # 분할 + OCR
  python3 split_and_ocr.py --single <path>    # 단일 PDF 처리
  python3 split_and_ocr.py --dry-run          # 미리보기 (실제 파일 생성 안함)
"""

import os
import re
import sys
import json
import unicodedata
import fitz  # PyMuPDF
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional
import subprocess
import tempfile
import shutil
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────
BASE_DIR = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료"
PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
SPLIT_DIR = os.path.join(PROJECT_DIR, "split_pdfs")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
GENS = ["19기", "20기", "21기"]

TESSERACT_CMD = "/opt/homebrew/bin/tesseract"
OCRMYPDF_CMD = "/Users/turtlesoup0-macmini/Library/Python/3.9/bin/ocrmypdf"
OCR_LANG = "kor+eng"


# ─── Helpers ─────────────────────────────────────────────────────
def nfc(s: str) -> str:
    return unicodedata.normalize('NFC', s)


def safe_filename(s: str, max_len: int = 80) -> str:
    """파일명에 안전한 문자열로 변환"""
    s = nfc(s)
    # 파일명 불가 문자 제거
    s = re.sub(r'[/\\:*?"<>|]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def extract_subject(week: str, filename: str) -> str:
    """주차/파일명에서 과목 추출"""
    combined = nfc(week + " " + filename).upper()
    mapping = [
        ('SW', r'\bSW\b'), ('DS', r'\bDS\b'), ('DB', r'\bDB\b'),
        ('SE', r'\bSE\b'), ('AI', r'\bAI\b'), ('CAOS', r'\bCAOS\b'),
        ('NW', r'\bNW\b'), ('경영', r'경영'), ('AL', r'\bAL\b'), ('OT', r'\bOT\b'),
    ]
    found = [name for name, pat in mapping if re.search(pat, combined, re.IGNORECASE)]
    if not found:
        for kw, subj in [('보안', 'SE'), ('멘티출제', '전범위'), ('자체모의', '전범위'),
                          ('합반', '전범위'), ('특강', '특강'), ('서바이벌', '특강')]:
            if kw in nfc(week):
                return subj
        return 'ETC'
    return '+'.join(found)


def extract_session(filename: str) -> str:
    m = re.search(r'(\d)교시', nfc(filename))
    return f"{m.group(1)}교시" if m else "0교시"


# ─── PDF Format Detection ───────────────────────────────────────
def detect_format(doc: fitz.Document) -> str:
    """PDF 포맷 자동 감지: 'standard' | 'menti' | 'inline' | 'bare' | 'sparse' | 'problem_only' | 'merged'"""
    if doc.page_count == 0:
        return 'bare'

    p1 = doc[0].get_text() or ""
    # 줄바꿈 포함 공백 모두 제거하여 분리된 한글 키워드 탐지
    # (PyMuPDF가 '출제영역'을 '출\n제\n영\n역'으로 추출하는 경우 대응)
    p1_collapsed = re.sub(r'\s+', '', p1)

    # 1페이지만 있는 문제지 (답안 없음)
    if doc.page_count == 1:
        return 'problem_only'

    # 텍스트 희소 문서 (이미지 전용 / 공백)
    sample_pages = min(5, doc.page_count)
    total_text = sum(len((doc[i].get_text() or '').strip()) for i in range(sample_pages))
    if total_text < 200 and doc.page_count > 3:
        return 'sparse'

    # 병합 문서 감지: FB 문서 + 아이리포 문서가 합쳐진 경우
    if '문제중' in p1_collapsed and '선택' in p1_collapsed:
        for pi in range(2, min(6, doc.page_count)):
            pt = doc[pi].get_text() or ''
            pt_collapsed = re.sub(r'\s+', '', pt)
            if '아이리포' in pt_collapsed and ('대비' in pt_collapsed or '기술사회' in pt_collapsed):
                return 'merged'

    # Format C: 멘티출제 (문제 N. + 출제영역/난이도 카드)
    if '출제영역' in p1_collapsed and '난이도' in p1_collapsed and '★' in p1:
        return 'menti'

    # Format A/B: Standard / Inline
    if '문제중' in p1_collapsed and '선택' in p1_collapsed:
        nums_found = re.findall(r'^(\d{1,2})\.\s+', p1, re.MULTILINE)
        if len(nums_found) >= 4:
            return 'standard'
        if '출제의도' in p1_collapsed or '작성방안' in p1_collapsed:
            return 'inline'
        return 'standard'
    return 'bare'


# ─── Problem List Extraction ────────────────────────────────────
def get_problem_list(doc: fitz.Document) -> List[Tuple[int, str]]:
    """문제 목록 추출 (모든 포맷 지원)"""
    fmt = detect_format(doc)
    if fmt == 'standard':
        return _problems_standard(doc)
    elif fmt == 'inline':
        return _problems_inline(doc)
    elif fmt == 'menti':
        return _problems_menti(doc)
    else:
        return _problems_bare(doc)


def _problems_standard(doc: fitz.Document) -> List[Tuple[int, str]]:
    for pi in range(min(2, doc.page_count)):
        text = doc[pi].get_text() or ""
        lines = text.split('\n')
        problems, active = [], False
        for line in lines:
            ls = line.strip()
            if '문제 중' in ls and '선택' in ls:
                active = True
                continue
            if active:
                m = re.match(r'^(\d{1,2})\.\s+(.+)', ls)
                if m:
                    problems.append((int(m.group(1)), m.group(2).strip()))
        if problems:
            return problems
    return []


def _problems_inline(doc: fitz.Document) -> List[Tuple[int, str]]:
    text = doc[0].get_text() or ""
    lines = text.split('\n')
    problems, active, hit_intent = [], False, False
    for line in lines:
        ls = line.strip()
        if '문제 중' in ls and '선택' in ls:
            active = True
            continue
        if active and not hit_intent:
            if '출제의도' in ls:
                hit_intent = True
                continue
            m = re.match(r'^(\d{1,2})\.\s+(.+)', ls)
            if m:
                num = int(m.group(1))
                if not problems or num == problems[-1][0] + 1:
                    problems.append((num, m.group(2).strip()))
    return problems if problems else _problems_bare(doc)


def _problems_menti(doc: fitz.Document) -> List[Tuple[int, str]]:
    problems = []
    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ""
        if not text.strip():
            continue
        # 줄바꿈으로 분리된 한글 대응: '출\n제' 또는 '출\n제\n영' 등
        # 1차: 기존 패턴 (줄바꿈 없는 정상 텍스트)
        # 2차: 줄바꿈 포함 패턴 (PyMuPDF가 한글을 글자 단위로 분리한 경우)
        for pat in [
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n출\s*제|$)',
            r'문\s*제\s+(\d{1,2})\.\s+(.+?)(?=\n\s*출\s*\n?\s*제|$)',
        ]:
            for m in re.finditer(pat, text, re.DOTALL):
                num = int(m.group(1))
                title = m.group(2).strip().split('\n')[0]
                if not any(p[0] == num for p in problems):
                    problems.append((num, title))
            if problems:
                break  # 첫 번째 패턴으로 찾았으면 두 번째 시도 안 함
    return sorted(problems, key=lambda x: x[0])


def _problems_bare(doc: fitz.Document) -> List[Tuple[int, str]]:
    problems, seen = [], set()
    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ""
        if len(text.strip()) < 30:
            continue
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if i >= 8:
                break
            m = re.match(r'^(\d{1,2})\.\s+(.+)', line.strip())
            if m:
                num, title = int(m.group(1)), m.group(2).strip()
                if len(title) > 3 and num not in seen:
                    ctx = '\n'.join(lines[i:min(i+8, len(lines))])
                    if any(kw in ctx for kw in ['출제의도', '작성방안', '회 ', 'Keyword',
                                                 '출제빈도', '출제배경', '풀이', '난이도']):
                        seen.add(num)
                        problems.append((num, title))
    return sorted(problems, key=lambda x: x[0])


# ─── Topic Boundary Detection ───────────────────────────────────
def find_boundaries(doc: fitz.Document, problems: List[Tuple[int, str]]) -> List[dict]:
    """토픽 경계 탐지 (모든 포맷)"""
    fmt = detect_format(doc)
    pnums = set(p[0] for p in problems)
    ptitles = {p[0]: p[1] for p in problems}

    if fmt == 'menti':
        return _boundaries_menti(doc, ptitles)

    boundaries = []
    start = 0 if fmt in ('inline', 'bare') else 1

    for pi in range(start, doc.page_count):
        text = doc[pi].get_text() or ""
        if len(text.strip()) < 30:
            continue
        lines = text.split('\n')
        for li, line in enumerate(lines):
            m = re.match(r'^(\d{1,2})\.\s+(.+)', line.strip())
            if not m:
                continue
            num = int(m.group(1))
            if num not in pnums:
                continue

            ctx = '\n'.join(lines[li:min(li+8, len(lines))])
            has_intent = any(kw in ctx for kw in ['출제의도', '작성방안'])
            near_top = li < 10

            # 제목 매칭
            expected = ptitles.get(num, "")
            clean_exp = re.sub(r'[(\[（].*?[)\]）]', '', expected).strip()
            clean_fnd = re.sub(r'[(\[（].*?[)\]）]', '', m.group(2)).strip()
            title_match = (clean_exp[:5] == clean_fnd[:5]) if clean_exp else False

            score = (10 if has_intent else 0) + (3 if near_top else 0) + (5 if title_match else 0)

            # 기존보다 높은 점수만 교체
            existing = [b for b in boundaries if b['num'] == num]
            if existing:
                if score > existing[0].get('score', 0):
                    boundaries = [b for b in boundaries if b['num'] != num]
                else:
                    continue

            if score >= 3:
                boundaries.append({
                    'num': num, 'title': ptitles.get(num, m.group(2).strip()),
                    'page_idx': pi, 'line_idx': li,
                    'has_intent': has_intent, 'score': score,
                })

    # 중복 제거 & 정렬
    seen = {}
    for b in sorted(boundaries, key=lambda x: (x['num'], -x.get('score', 0))):
        if b['num'] not in seen:
            seen[b['num']] = b
    boundaries = sorted(seen.values(), key=lambda x: (x['page_idx'], x['line_idx']))

    _set_page_ends(doc, boundaries)
    return boundaries


def _boundaries_menti(doc: fitz.Document, ptitles: dict) -> List[dict]:
    boundaries = []
    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ""
        if not text.strip():
            continue
        # 줄바꿈 분리된 한글 대응: 두 패턴 순차 시도
        for pat in [
            r'문\s*제\s+(\d{1,2})\.\s+',
            r'문\s*\n?\s*제\s+(\d{1,2})\.\s+',
        ]:
            for m in re.finditer(pat, text):
                num = int(m.group(1))
                if not any(b['num'] == num for b in boundaries):
                    boundaries.append({
                        'num': num, 'title': ptitles.get(num, f"문제{num}"),
                        'page_idx': pi, 'line_idx': 0, 'has_intent': True, 'score': 15,
                    })
    boundaries.sort(key=lambda x: (x['page_idx'], x['num']))
    _set_page_ends(doc, boundaries)
    return boundaries


def _set_page_ends(doc: fitz.Document, boundaries: List[dict]):
    for i, b in enumerate(boundaries):
        if i + 1 < len(boundaries):
            nxt = boundaries[i+1]['page_idx']
            b['page_end'] = nxt - 1 if nxt > b['page_idx'] else nxt
        else:
            b['page_end'] = doc.page_count - 1
        # 끝 페이지가 문제지 재등장이면 제외
        if b['page_end'] < doc.page_count:
            lt = doc[b['page_end']].get_text() or ""
            if '다음 문제 중' in lt and b['page_end'] > b['page_idx']:
                b['page_end'] -= 1


# ─── PDF Splitting ──────────────────────────────────────────────
def split_pdf(source_path: str, boundaries: List[dict], output_dir: str,
              gen: str, week: str, subject: str, session: str) -> List[dict]:
    """
    원본 PDF를 토픽별 개별 PDF로 분할
    Returns: 생성된 파일 정보 리스트
    """
    doc = fitz.open(source_path)
    results = []

    for b in boundaries:
        start_p = b['page_idx']
        end_p = min(b['page_end'], doc.page_count - 1)

        if start_p > end_p:
            continue

        # 새 PDF 생성
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start_p, to_page=end_p)

        # 파일명: {기수}_{주차}_{과목}_{교시}_Q{번호}_{토픽명}.pdf
        topic_name = safe_filename(b['title'], max_len=60)
        fname = f"{gen}_{safe_filename(week, 20)}_{subject}_{session}_Q{b['num']:02d}_{topic_name}.pdf"
        out_path = os.path.join(output_dir, fname)

        new_doc.save(out_path)
        new_doc.close()

        # 페이지별 텍스트/이미지 분류
        img_pages = 0
        for pi in range(start_p, end_p + 1):
            if len((doc[pi].get_text() or "").strip()) < 50:
                img_pages += 1
        total_pages = end_p - start_p + 1

        results.append({
            'filename': fname,
            'path': out_path,
            'gen': gen, 'week': week, 'subject': subject, 'session': session,
            'q_num': b['num'], 'q_title': b['title'],
            'pages': total_pages,
            'image_pages': img_pages,
            'needs_ocr': img_pages > 0,
            'source': os.path.basename(source_path),
        })

    doc.close()
    return results


# ─── OCR Processing ─────────────────────────────────────────────
def apply_ocr(pdf_path: str) -> bool:
    """
    ocrmypdf로 PDF에 텍스트 레이어 추가
    이미 텍스트가 있는 페이지는 건너뜀
    """
    tmp_out = pdf_path + ".ocr_tmp.pdf"
    try:
        result = subprocess.run(
            [OCRMYPDF_CMD,
             '--language', OCR_LANG,
             '--skip-text',           # 텍스트 있는 페이지 건너뜀
             '--optimize', '1',       # 경량 최적화
             '--output-type', 'pdf',
             '--tesseract-timeout', '60',
             pdf_path, tmp_out],
            capture_output=True, text=True, timeout=180
        )

        if result.returncode == 0 and os.path.exists(tmp_out):
            shutil.move(tmp_out, pdf_path)
            return True
        elif result.returncode == 6:
            # Already has text - no OCR needed
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)
            return True
        else:
            print(f"    OCR warning: {result.stderr[:200]}")
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)
            return False
    except subprocess.TimeoutExpired:
        print(f"    OCR timeout: {os.path.basename(pdf_path)}")
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)
        return False
    except Exception as e:
        print(f"    OCR error: {e}")
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)
        return False


# ─── Main Pipeline ──────────────────────────────────────────────
def find_review_pdfs() -> List[dict]:
    """모든 리뷰 PDF 탐색"""
    pdfs = []
    for gen in GENS:
        gen_path = os.path.join(BASE_DIR, gen)
        for root, dirs, files in os.walk(gen_path):
            for f in files:
                if not f.endswith('.pdf'):
                    continue
                fn = nfc(f)
                rn = nfc(root)
                if '리뷰' not in fn or '복사본' in fn:
                    continue
                # /bak/ 폴더도 포함 (20기 17주차 리뷰 PDF가 bak에 있음)
                full = os.path.join(root, f)
                parts = rn.split('/')
                week_parts = [p for p in parts if any(kw in p for kw in
                    ['주차', '오리엔테이션', '멘티출제', '특강', '합반', '자체모의', '서바이벌'])]
                week = nfc(week_parts[-1]) if week_parts else 'UNKNOWN'
                pdfs.append({
                    'path': full, 'filename': fn, 'gen': gen, 'week': week,
                    'subject': extract_subject(week, fn),
                    'session': extract_session(fn),
                })
    return sorted(pdfs, key=lambda x: (x['gen'], x['week'], x['session']))


def run_pipeline(do_ocr: bool = False, dry_run: bool = False, single_path: str = None):
    """메인 파이프라인 실행"""
    os.makedirs(SPLIT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # PDF 목록
    if single_path:
        fn = nfc(os.path.basename(single_path))
        pdfs = [{
            'path': single_path, 'filename': fn, 'gen': 'single',
            'week': 'single', 'subject': extract_subject('', fn),
            'session': extract_session(fn),
        }]
    else:
        pdfs = find_review_pdfs()

    print(f"\n{'='*70}")
    print(f" 정보관리 기술사 리뷰 PDF 분할 파이프라인")
    print(f" 대상: {len(pdfs)}개 PDF | OCR: {'ON' if do_ocr else 'OFF'} | Dry-run: {'ON' if dry_run else 'OFF'}")
    print(f" 출력: {SPLIT_DIR}")
    print(f"{'='*70}\n")

    all_results = []
    failed = []
    total_topics = 0
    total_ocr = 0

    for i, pdf in enumerate(pdfs):
        label = f"[{i+1}/{len(pdfs)}] {pdf['gen']}/{pdf['week']}/{pdf['filename']}"
        print(label)

        try:
            doc = fitz.open(pdf['path'])
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append({'pdf': pdf['filename'], 'error': str(e)})
            continue

        fmt = detect_format(doc)
        print(f"  포맷: {fmt} | 페이지: {doc.page_count}")

        # 특수 포맷 처리: sparse / problem_only / merged
        if fmt == 'sparse':
            print(f"  ⚠ 텍스트 희소 문서 (이미지 전용) → 건너뜀 (--ocr 필요)")
            failed.append({'pdf': pdf['filename'], 'error': 'sparse/image-only (needs OCR)'})
            doc.close()
            continue

        if fmt == 'problem_only':
            print(f"  ⚠ 1페이지 문제지 (답안 없음) → 건너뜀")
            failed.append({'pdf': pdf['filename'], 'error': 'problem list only (no answers)'})
            doc.close()
            continue

        if fmt == 'merged':
            # 병합 문서: 아이리포 부분 시작점 찾아서 FB 부분만 처리
            merge_page = doc.page_count
            for mpi in range(2, doc.page_count):
                mpt = re.sub(r'\s+', '', doc[mpi].get_text() or '')
                if '아이리포' in mpt and ('대비' in mpt or '기술사회' in mpt):
                    merge_page = mpi
                    break
            print(f"  병합 문서: FB 부분 p1-{merge_page} / 아이리포 부분 p{merge_page+1}+")
            if merge_page <= 2:
                print(f"  ⚠ FB 부분이 너무 짧음 → 건너뜀")
                failed.append({'pdf': pdf['filename'], 'error': f'merged: FB portion too short ({merge_page}p)'})
                doc.close()
                continue
            # FB 부분만 잘라서 임시 문서로 처리
            fb_doc = fitz.open()
            fb_doc.insert_pdf(doc, from_page=0, to_page=merge_page - 1)
            doc.close()
            doc = fb_doc
            fmt = detect_format(doc)
            print(f"  FB 부분 재감지: {fmt} | {doc.page_count}p")

        # 문제 목록 추출
        problems = get_problem_list(doc)
        if not problems:
            print(f"  ⚠ 문제 목록 미탐지 → 건너뜀")
            failed.append({'pdf': pdf['filename'], 'error': 'no problem list'})
            doc.close()
            continue

        print(f"  문제: {len(problems)}개 → {[p[0] for p in problems]}")

        # 경계 탐지
        boundaries = find_boundaries(doc, problems)
        if not boundaries:
            print(f"  ⚠ 토픽 경계 미탐지 → 건너뜀")
            failed.append({'pdf': pdf['filename'], 'error': 'no boundaries'})
            doc.close()
            continue

        boundary_summary = [(b['num'], 'p%d-%d' % (b['page_idx']+1, b['page_end']+1)) for b in boundaries]
        print(f"  경계: {len(boundaries)}개 → {boundary_summary}")
        doc.close()

        if dry_run:
            total_topics += len(boundaries)
            continue

        # 출력 폴더 생성
        out_dir = os.path.join(SPLIT_DIR, pdf['gen'], safe_filename(pdf['week'], 30))
        os.makedirs(out_dir, exist_ok=True)

        # PDF 분할
        results = split_pdf(
            pdf['path'], boundaries, out_dir,
            pdf['gen'], pdf['week'], pdf['subject'], pdf['session']
        )

        # OCR 적용
        if do_ocr:
            for r in results:
                if r['needs_ocr']:
                    print(f"    OCR: {r['filename']} ({r['image_pages']}/{r['pages']}p)")
                    ok = apply_ocr(r['path'])
                    r['ocr_applied'] = ok
                    if ok:
                        total_ocr += 1

        all_results.extend(results)
        total_topics += len(results)
        print(f"  → {len(results)}개 토픽 PDF 생성 완료")

    # 리포트
    print(f"\n{'='*70}")
    print(f" 완료 리포트")
    print(f"{'='*70}")
    print(f" 처리 PDF: {len(pdfs)}개")
    print(f" 추출 토픽: {total_topics}개")
    if do_ocr:
        print(f" OCR 적용: {total_ocr}개")
    if failed:
        print(f" 실패: {len(failed)}개")
        for f in failed:
            print(f"   - {f['pdf']}: {f['error']}")

    # 결과 JSON 저장
    if not dry_run:
        report = {
            'timestamp': datetime.now().isoformat(),
            'total_pdfs': len(pdfs),
            'total_topics': total_topics,
            'ocr_applied': total_ocr,
            'failed': failed,
            'results': all_results,
        }
        report_path = os.path.join(DATA_DIR, "split_report.json")
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n 리포트 저장: {report_path}")

    return all_results


# ─── CLI ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    do_ocr = "--ocr" in sys.argv
    dry_run = "--dry-run" in sys.argv
    single = None
    if "--single" in sys.argv:
        idx = sys.argv.index("--single")
        if idx + 1 < len(sys.argv):
            single = sys.argv[idx + 1]

    run_pipeline(do_ocr=do_ocr, dry_run=dry_run, single_path=single)
