#!/usr/bin/env python3
"""
정보관리 기술사 600제 교본 PDF 분할 스크립트

8개 과목 통합본(4,547 페이지)을 문제별 개별 PDF로 분할

사용법:
  python3 split_600.py                      # 전체 dry-run
  python3 split_600.py --run                 # 실제 분할
  python3 split_600.py --run --ocr           # 분할 + OCR
  python3 split_600.py --subject 경영        # 특정 과목만
  python3 split_600.py --single <path>       # 단일 PDF
"""

import os
import re
import sys
import json
import unicodedata
import fitz  # PyMuPDF
from typing import List, Tuple, Optional
from datetime import datetime
import subprocess
import shutil

# ─── Configuration ───────────────────────────────────────────────
BASE_DIR = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/4_FB반 자료"
TEXTBOOK_DIR = os.path.join(BASE_DIR, "교본 (600제)")
PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
SPLIT_DIR = os.path.join(PROJECT_DIR, "split_pdfs", "600제")
DATA_DIR = os.path.join(PROJECT_DIR, "data")

OCRMYPDF_CMD = "/Users/turtlesoup0-macmini/Library/Python/3.9/bin/ocrmypdf"
OCR_LANG = "kor+eng"

BOOKS = {
    '경영': '1_경영_600제_통합본_v4.0.pdf',
    '소공': '2_소공_600제_통합본_v4.0.pdf',
    'DB':   '3_DB_600제_통합본_v4.0.pdf',
    'DS':   '4_DS_600제_통합본_v4.0.pdf',
    'NW':   '5_NW_600제_통합본_v4.0.pdf',
    'CAOS': '6_CAOS_600제_통합본_v4.0.pdf',
    '보안': '7_보안_600제_통합본_v4.0.pdf',
    '인알통': '8_인알통_600제_통합본_v4.0.pdf',
}

SKIP_PAGES = 3  # 표지 + 목차 건너뜀


# ─── Helpers ─────────────────────────────────────────────────────
def nfc(s: str) -> str:
    return unicodedata.normalize('NFC', s)


def safe_filename(s: str, max_len: int = 80) -> str:
    """파일명에 안전한 문자열로 변환"""
    s = nfc(s)
    s = re.sub(r'[/\\:*?"<>|]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


# ─── Question Detection ─────────────────────────────────────────
def find_questions(doc: fitz.Document, skip_pages: int = SKIP_PAGES) -> List[dict]:
    """
    600제 문제 경계 탐지

    패턴:
      [토픽명]         ← '문제' 윗줄
      문제              ← 독립 줄 (PRIMARY MARKER)
      [질문 텍스트]
      도메인|출제영역   ← 검증 마커 (12줄 이내)
    """
    questions = []

    for pi in range(skip_pages, doc.page_count):
        text = doc[pi].get_text() or ''
        if len(text.strip()) < 50:
            continue

        lines = text.strip().split('\n')
        for li, line in enumerate(lines):
            ls = line.strip()

            # Primary marker: 독립 '문제' 줄
            if ls != '문제':
                continue

            # '도메인' 또는 '출제영역' 검증: 다음 12줄 이내에 있어야 함
            ctx_end = min(li + 12, len(lines))
            ctx_after = '\n'.join(lines[li:ctx_end])
            has_domain = '도메인' in ctx_after
            has_area = '출제영역' in ctx_after
            if not has_domain and not has_area:
                continue

            # 토픽명: '문제' 바로 윗줄
            topic_title = ''
            if li > 0:
                candidate = lines[li - 1].strip()
                # 페이지번호, 빈줄, 불릿 등 제외
                if (len(candidate) >= 2
                    and not candidate.startswith('-')
                    and not candidate.startswith('•')
                    and not re.match(r'^\d+$', candidate)
                    and len(candidate) <= 120):
                    topic_title = candidate
                elif li > 1:
                    # 2줄 위 시도
                    candidate2 = lines[li - 2].strip()
                    if (len(candidate2) >= 2
                        and not candidate2.startswith('-')
                        and not re.match(r'^\d+$', candidate2)
                        and len(candidate2) <= 120):
                        topic_title = candidate2

            if not topic_title:
                continue

            # 질문 텍스트 추출 (문제 다음줄 ~ 도메인/출제영역 전)
            q_text = ''
            q_lines = []
            for ql in range(li + 1, min(li + 6, len(lines))):
                qls = lines[ql].strip()
                if qls in ('도메인', '출제영역') or qls.startswith('도메인') or qls.startswith('출제영역'):
                    break
                q_lines.append(qls)
            q_text = ' '.join(q_lines)[:200]

            # 도메인/출제영역 추출
            domain = ''
            domain_m = re.search(r'(?:도메인|출제영역)\s*\n\s*(.+)', ctx_after)
            if domain_m:
                domain = domain_m.group(1).strip()

            # 키워드 추출
            keywords = ''
            kw_m = re.search(
                r'키워드\s*\n\s*(.+?)(?=\n\s*(?:목차|출제|참고|해설|난이도|채점)|\n\s*\n)',
                ctx_after, re.DOTALL)
            if kw_m:
                keywords = kw_m.group(1).strip().replace('\n', ', ')

            questions.append({
                'topic_title': topic_title,
                'q_text': q_text,
                'domain': domain,
                'keywords': keywords,
                'page_idx': pi,
                'line_idx': li,
            })

    # 페이지 끝 계산
    for i, q in enumerate(questions):
        if i + 1 < len(questions):
            next_page = questions[i + 1]['page_idx']
            # 다음 문제와 같은 페이지면 같은 페이지까지
            q['page_end'] = max(next_page - 1, q['page_idx'])
        else:
            q['page_end'] = doc.page_count - 1

    return questions


# ─── PDF Splitting ───────────────────────────────────────────────
def split_pdf(doc_path: str, questions: List[dict],
              output_dir: str, subject: str) -> List[dict]:
    """600제 PDF를 문제별 개별 PDF로 분할"""
    doc = fitz.open(doc_path)
    results = []

    for i, q in enumerate(questions):
        start_p = q['page_idx']
        end_p = min(q['page_end'], doc.page_count - 1)
        if start_p > end_p:
            continue

        # 새 PDF 생성
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start_p, to_page=end_p)

        # 파일명
        title = safe_filename(q['topic_title'], max_len=60)
        fname = f"600제_{subject}_Q{i+1:03d}_{title}.pdf"
        out_path = os.path.join(output_dir, fname)

        new_doc.save(out_path)
        new_doc.close()

        # 이미지 페이지 체크
        img_pages = 0
        for pi in range(start_p, end_p + 1):
            if len((doc[pi].get_text() or '').strip()) < 50:
                img_pages += 1
        total_pages = end_p - start_p + 1

        results.append({
            'filename': fname,
            'path': out_path,
            'subject': subject,
            'q_num': i + 1,
            'q_title': q['topic_title'],
            'q_text': q['q_text'],
            'domain': q['domain'],
            'keywords': q['keywords'],
            'pages': total_pages,
            'image_pages': img_pages,
            'needs_ocr': img_pages > 0,
            'source': os.path.basename(doc_path),
        })

    doc.close()
    return results


# ─── OCR Processing ─────────────────────────────────────────────
def apply_ocr(pdf_path: str) -> bool:
    """ocrmypdf로 PDF에 텍스트 레이어 추가"""
    tmp_out = pdf_path + ".ocr_tmp.pdf"
    try:
        result = subprocess.run(
            [OCRMYPDF_CMD,
             '--language', OCR_LANG,
             '--skip-text',
             '--optimize', '1',
             '--output-type', 'pdf',
             '--tesseract-timeout', '60',
             pdf_path, tmp_out],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0 and os.path.exists(tmp_out):
            shutil.move(tmp_out, pdf_path)
            return True
        elif result.returncode == 6:
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


# ─── Main Pipeline ───────────────────────────────────────────────
def run_pipeline(do_ocr: bool = False, dry_run: bool = False,
                 subject_filter: str = None, single_path: str = None):
    """메인 파이프라인 실행"""
    os.makedirs(SPLIT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 대상 PDF 목록
    if single_path:
        fn = nfc(os.path.basename(single_path))
        # 과목 추측
        subj = 'UNKNOWN'
        for s, bf in BOOKS.items():
            if s in fn or bf in fn:
                subj = s
                break
        books = [(subj, single_path)]
    else:
        books = []
        for subj, fname in sorted(BOOKS.items(), key=lambda x: x[1]):
            path = os.path.join(TEXTBOOK_DIR, fname)
            # macOS NFC 정규화
            if not os.path.exists(path):
                # glob 방식으로 찾기
                import glob
                candidates = glob.glob(os.path.join(TEXTBOOK_DIR, f"*{subj}*600제*"))
                if candidates:
                    path = candidates[0]
                else:
                    print(f"  ⚠ {fname} 파일 없음 → 건너뜀")
                    continue
            if subject_filter and subj != subject_filter:
                continue
            books.append((subj, path))

    print(f"\n{'='*70}")
    print(f" 정보관리 기술사 600제 교본 PDF 분할")
    print(f" 대상: {len(books)}개 교본 | OCR: {'ON' if do_ocr else 'OFF'} | Dry-run: {'ON' if dry_run else 'OFF'}")
    print(f" 출력: {SPLIT_DIR}")
    print(f"{'='*70}\n")

    all_results = []
    failed = []
    total_questions = 0
    total_ocr = 0

    for subj, path in books:
        fn = nfc(os.path.basename(path))
        print(f"[{subj}] {fn}")

        try:
            doc = fitz.open(path)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append({'subject': subj, 'file': fn, 'error': str(e)})
            continue

        print(f"  페이지: {doc.page_count}")

        # 문제 탐지
        questions = find_questions(doc)
        print(f"  문제 탐지: {len(questions)}개")

        if not questions:
            print(f"  ⚠ 문제 미탐지 → 건너뜀")
            failed.append({'subject': subj, 'file': fn, 'error': 'no questions found'})
            doc.close()
            continue

        # 처음/마지막 문제 표시
        first = questions[0]
        last = questions[-1]
        print(f"  첫 문제: Q1 [{first['topic_title'][:30]}...] p{first['page_idx']+1}")
        print(f"  끝 문제: Q{len(questions)} [{last['topic_title'][:30]}...] p{last['page_idx']+1}-{last['page_end']+1}")
        doc.close()

        if dry_run:
            total_questions += len(questions)
            # dry-run 상세 목록
            for qi, q in enumerate(questions[:5]):
                print(f"    Q{qi+1:03d}: {q['topic_title'][:50]} | {q['domain']} | p{q['page_idx']+1}-{q['page_end']+1}")
            if len(questions) > 5:
                print(f"    ... +{len(questions)-5}개 더")
            continue

        # 출력 디렉토리
        out_dir = os.path.join(SPLIT_DIR, subj)
        os.makedirs(out_dir, exist_ok=True)

        # 분할
        results = split_pdf(path, questions, out_dir, subj)

        # OCR
        if do_ocr:
            for r in results:
                if r['needs_ocr']:
                    print(f"    OCR: {r['filename']} ({r['image_pages']}/{r['pages']}p)")
                    ok = apply_ocr(r['path'])
                    r['ocr_applied'] = ok
                    if ok:
                        total_ocr += 1

        all_results.extend(results)
        total_questions += len(results)
        print(f"  → {len(results)}개 문제 PDF 생성 완료")

    # 리포트
    print(f"\n{'='*70}")
    print(f" 완료 리포트")
    print(f"{'='*70}")
    print(f" 처리 교본: {len(books)}개")
    print(f" 추출 문제: {total_questions}개")
    if do_ocr:
        print(f" OCR 적용: {total_ocr}개")
    if failed:
        print(f" 실패: {len(failed)}개")
        for f in failed:
            print(f"   - {f['subject']}: {f['error']}")

    # 과목별 통계
    if all_results or dry_run:
        print(f"\n 과목별 통계:")
        from collections import Counter
        if not dry_run:
            subj_counts = Counter(r['subject'] for r in all_results)
        else:
            subj_counts = {}
        for subj, _ in sorted(BOOKS.items(), key=lambda x: x[1]):
            cnt = subj_counts.get(subj, '?')
            print(f"   {subj}: {cnt}개")

    # JSON 리포트 저장
    if not dry_run and all_results:
        report = {
            'timestamp': datetime.now().isoformat(),
            'total_books': len(books),
            'total_questions': total_questions,
            'ocr_applied': total_ocr,
            'failed': failed,
            'results': all_results,
        }
        report_path = os.path.join(DATA_DIR, "600je_report.json")
        with open(report_path, 'w', encoding='utf-8') as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
        print(f"\n 리포트 저장: {report_path}")

    return all_results


# ─── CLI ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    do_ocr = "--ocr" in sys.argv
    dry_run = "--run" not in sys.argv  # 기본 = dry-run
    subj_filter = None
    single = None

    if "--subject" in sys.argv:
        idx = sys.argv.index("--subject")
        if idx + 1 < len(sys.argv):
            subj_filter = sys.argv[idx + 1]

    if "--single" in sys.argv:
        idx = sys.argv.index("--single")
        if idx + 1 < len(sys.argv):
            single = sys.argv[idx + 1]

    run_pipeline(do_ocr=do_ocr, dry_run=dry_run,
                 subject_filter=subj_filter, single_path=single)
