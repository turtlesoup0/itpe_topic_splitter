#!/usr/bin/env python3
"""
모의고사 + 합숙 PDF 분할 스크립트

iCloud/공부/2_모의고사 및 3_합숙 폴더의 PDF를 문제별 개별 PDF로 분할.

지원 출처: ITPE, KPC
제외 출처: 아이리포, 라이지움, 회사 양성반 (텍스트 추출 불가)

사용법:
  python3 split_materials.py                    # 전체 dry-run
  python3 split_materials.py --run              # 실제 분할
  python3 split_materials.py --type 모의고사    # 모의고사만 dry-run
  python3 split_materials.py --type 합숙        # 합숙만 dry-run
  python3 split_materials.py --type 합숙 --run  # 합숙만 실제 분할
  python3 split_materials.py --run --ocr        # 분할 + OCR

내부 포맷:
  01              ← 2자리 독립 숫자 (PRIMARY MARKER, 01-16)
  [토픽명]       ← 다음 비어있지 않은 줄
  문제: ...       ← 또는 예상문제: ...
  도메인: 보안   ← 검증 마커 (8줄 이내)
  난이도: 중
  키워드: ...
  끝             ← 종료 마커

출력:
  split_pdfs/모의고사/{출처명}/  (예: ITPE40-2601_합/)
  split_pdfs/합숙/{출처명}/      (예: ITPE138_1일_1/)
"""

import os
import re
import sys
import json
import unicodedata
import fitz  # PyMuPDF
from datetime import datetime
import subprocess
import shutil

# ─── Configuration ─────────────────────────────────────────────────
STUDY_BASE = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부"
MOUI_DIR   = os.path.join(STUDY_BASE, "2_모의고사")
HABSUK_DIR = os.path.join(STUDY_BASE, "3_합숙")

PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
SPLIT_DIR   = os.path.join(PROJECT_DIR, "split_pdfs")
DATA_DIR    = os.path.join(PROJECT_DIR, "data")

OCRMYPDF_CMD = "/Users/turtlesoup0-macmini/Library/Python/3.9/bin/ocrmypdf"
OCR_LANG     = "kor+eng"

# 포함할 서브폴더 (소문자 비교)
INCLUDE_SUBDIRS = {'itpe', 'kpc'}

# 제외할 키워드 (폴더명/파일명 포함 여부로 판단)
EXCLUDE_KEYWORDS = {'아이리포', '라이지움', '회사', 'bak'}


# ─── Helpers ───────────────────────────────────────────────────────
def nfc(s):
    return unicodedata.normalize('NFC', s)


def safe_filename(s, max_len=80):
    s = nfc(s)
    s = re.sub(r'[/\\:*?"<>|]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:max_len].rstrip() if len(s) > max_len else s


def get_source_name(filename, material_type):
    """
    파일명에서 출처 이름 추출 (출력 폴더명 및 파일 접두사로 사용)

    모의고사: 모의_ITPE40-2601_합.pdf  →  ITPE40-2601_합
    합숙:     합숙_ITPE138_1일_1.pdf   →  ITPE138_1일_1
    """
    fn = nfc(os.path.basename(filename))
    fn = fn.replace('.pdf', '')
    if material_type == '모의고사' and fn.startswith('모의_'):
        return fn[len('모의_'):]
    elif material_type == '합숙' and fn.startswith('합숙_'):
        return fn[len('합숙_'):]
    return fn  # fallback: 파일명 전체


# ─── File Discovery ────────────────────────────────────────────────
def find_pdfs(base_dir, material_type):
    """
    모의고사 또는 합숙 폴더에서 처리할 PDF 목록 반환.

    대상 서브폴더: ITPE/, KPC/
    제외: 아이리포, 라이지움, 회사 양성반, bak
    """
    results = []

    if not os.path.isdir(base_dir):
        print('  ⚠ 폴더 없음: %s' % base_dir)
        return results

    for subdir in sorted(os.listdir(base_dir)):
        sub_path = os.path.join(base_dir, subdir)
        sub_nfc  = nfc(subdir)

        if not os.path.isdir(sub_path):
            continue

        # 제외 키워드 체크
        skip = any(kw in sub_nfc for kw in EXCLUDE_KEYWORDS)
        if skip:
            continue

        # 포함 서브폴더만 (ITPE, KPC)
        if sub_nfc.lower() not in INCLUDE_SUBDIRS:
            continue

        # PDF 수집 (단일 레벨, 재귀 X)
        for fn in sorted(os.listdir(sub_path)):
            fn_nfc = nfc(fn)
            full   = os.path.join(sub_path, fn)

            if not fn_nfc.lower().endswith('.pdf') or not os.path.isfile(full):
                continue

            # bak 파일 제외
            if 'bak' in fn_nfc.lower():
                continue

            results.append({
                'path':          full,
                'filename':      fn_nfc,
                'subdir':        sub_nfc,
                'material_type': material_type,
                'source_name':   get_source_name(fn_nfc, material_type),
            })

    return sorted(results, key=lambda x: (x['subdir'], x['filename']))


# ─── Question Detection ────────────────────────────────────────────
def find_questions(doc):
    """
    모의고사/합숙 PDF에서 문제 경계 탐지.

    알고리즘:
      1. 각 페이지의 첫 25줄 스캔
      2. 독립 2자리 숫자(01-16)가 PRIMARY MARKER
      3. 그 이후 8줄 이내 '도메인' 또는 '예상문제' 포함 시 유효
      4. 토픽명: 숫자 다음 비어있지 않은 줄
      5. 페이지당 첫 번째 유효 마커만 사용 (페이지 번호 오탐 방지)
    """
    questions = []

    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ''
        if len(text.strip()) < 30:
            continue

        lines = text.strip().split('\n')

        for li, line in enumerate(lines[:25]):
            ls = line.strip()

            # Primary marker: 독립 2자리 숫자
            m = re.match(r'^(\d{2})$', ls)
            if not m:
                continue
            num = int(m.group(1))
            if not (1 <= num <= 16):
                continue

            # 검증: 다음 8줄 이내 '도메인' 또는 '예상문제' 포함
            ctx_lines = lines[li:min(li + 8, len(lines))]
            ctx = '\n'.join(ctx_lines)
            if '도메인' not in ctx and '예상문제' not in ctx:
                continue

            # 토픽명: 다음 비어있지 않은 줄
            topic = ''
            for nl in lines[li + 1:li + 5]:
                nls = nl.strip()
                if nls and len(nls) > 1 and not re.match(r'^\d+$', nls):
                    topic = nls
                    break

            if not topic:
                continue

            # 도메인 추출
            domain = ''
            dm = re.search(r'도메인\s*[:\n]\s*(.+)', ctx)
            if dm:
                domain = dm.group(1).strip()

            questions.append({
                'q_num':    num,
                'topic':    topic,
                'domain':   domain,
                'page_idx': pi,
            })
            break  # 페이지당 첫 번째 유효 마커만 사용

    # page_end 계산: 다음 문제 시작 페이지의 전 페이지까지
    for i, q in enumerate(questions):
        if i + 1 < len(questions):
            q['page_end'] = max(questions[i + 1]['page_idx'] - 1, q['page_idx'])
        else:
            q['page_end'] = doc.page_count - 1

    return questions


# ─── PDF Splitting ─────────────────────────────────────────────────
def split_pdf(doc_path, questions, output_dir, source_name):
    """문제 경계에 따라 PDF를 개별 파일로 분할"""
    doc     = fitz.open(doc_path)
    results = []

    for q in questions:
        sp = q['page_idx']
        ep = min(q['page_end'], doc.page_count - 1)
        if sp > ep:
            continue

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=sp, to_page=ep)

        topic  = safe_filename(q['topic'], max_len=50)
        fname  = '%s_Q%02d_%s.pdf' % (safe_filename(source_name, 40), q['q_num'], topic)
        out_path = os.path.join(output_dir, fname)

        new_doc.save(out_path)
        new_doc.close()

        img_pages = sum(
            1 for pi in range(sp, ep + 1)
            if len((doc[pi].get_text() or '').strip()) < 50
        )

        results.append({
            'filename':    fname,
            'path':        out_path,
            'source_name': source_name,
            'q_num':       q['q_num'],
            'q_title':     q['topic'],
            'domain':      q['domain'],
            'pages':       ep - sp + 1,
            'image_pages': img_pages,
            'needs_ocr':   img_pages > 0,
        })

    doc.close()
    return results


# ─── OCR ──────────────────────────────────────────────────────────
def apply_ocr(pdf_path):
    tmp = pdf_path + '.ocr_tmp.pdf'
    try:
        r = subprocess.run(
            [OCRMYPDF_CMD, '--language', OCR_LANG, '--skip-text',
             '--optimize', '1', '--output-type', 'pdf',
             '--tesseract-timeout', '60', pdf_path, tmp],
            capture_output=True, text=True, timeout=180
        )
        if r.returncode == 0 and os.path.exists(tmp):
            shutil.move(tmp, pdf_path)
            return True
        elif r.returncode == 6:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return True
        else:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return False
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False


# ─── Pipeline ─────────────────────────────────────────────────────
def run_pipeline(material_type=None, do_split=False, do_ocr=False):
    """
    material_type: '모의고사', '합숙', 또는 None (전체)
    """
    os.makedirs(SPLIT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR,  exist_ok=True)

    # 처리 대상 결정
    targets = []
    if material_type is None or material_type == '모의고사':
        targets.append(('모의고사', MOUI_DIR))
    if material_type is None or material_type == '합숙':
        targets.append(('합숙', HABSUK_DIR))

    mode = 'DRY-RUN' if not do_split else ('SPLIT+OCR' if do_ocr else 'SPLIT')

    all_results = []
    all_failed  = []

    for mat_type, base_dir in targets:
        pdfs     = find_pdfs(base_dir, mat_type)
        out_base = os.path.join(SPLIT_DIR, mat_type)

        print('\n' + '=' * 70)
        print(' %s PDF 분할 [%s]' % (mat_type, mode))
        print(' 대상: %d개 PDF | 출력: %s' % (len(pdfs), out_base))
        print('=' * 70)

        total       = 0
        failed      = []
        results_all = []

        for i, pdf in enumerate(pdfs):
            print('\n[%d/%d] %s / %s' % (i + 1, len(pdfs), pdf['subdir'], pdf['filename']))

            try:
                doc = fitz.open(pdf['path'])
            except Exception as e:
                print('  ✗ %s' % e)
                failed.append({'pdf': pdf['filename'], 'error': str(e)})
                continue

            questions = find_questions(doc)
            doc.close()

            if not questions:
                print('  ✗ 문제 미탐지')
                failed.append({'pdf': pdf['filename'], 'error': 'no questions found'})
                continue

            # 탐지 결과 출력
            q_preview = ', '.join(
                'Q%02d(%s)' % (q['q_num'], q['topic'][:12])
                for q in questions[:4]
            )
            if len(questions) > 4:
                q_preview += ', +%d개' % (len(questions) - 4)
            print('  탐지: %d개 → %s' % (len(questions), q_preview))
            total += len(questions)

            if not do_split:
                continue

            out_dir = os.path.join(out_base, safe_filename(pdf['source_name'], 60))
            os.makedirs(out_dir, exist_ok=True)

            results = split_pdf(pdf['path'], questions, out_dir, pdf['source_name'])

            if do_ocr:
                for r in results:
                    if r['needs_ocr']:
                        ok = apply_ocr(r['path'])
                        r['ocr_applied'] = ok
                        if ok:
                            print('    OCR: %s' % r['filename'])

            results_all.extend(results)
            print('  → %d개 PDF 생성' % len(results))

        # 소계
        print('\n' + '─' * 70)
        action = '탐지' if not do_split else '생성'
        print(' %s 완료: %d개 문제 %s' % (mat_type, total, action))
        if failed:
            print(' 실패: %d개' % len(failed))
            for f in failed:
                print('   - %s: %s' % (f['pdf'], f['error']))

        # 리포트 저장
        if do_split and results_all:
            report = {
                'timestamp':     datetime.now().isoformat(),
                'material_type': mat_type,
                'total':         total,
                'results':       results_all,
                'failed':        failed,
            }
            fname_map = {'모의고사': 'moui_report.json', '합숙': 'habsuk_report.json'}
            rp = os.path.join(DATA_DIR, fname_map.get(mat_type, '%s_report.json' % mat_type))
            with open(rp, 'w', encoding='utf-8') as fp:
                json.dump(report, fp, ensure_ascii=False, indent=2)
            print(' 리포트: %s' % rp)

        all_results.extend(results_all)
        all_failed.extend(failed)

    return all_results


# ─── CLI ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    do_split    = '--run'  in sys.argv
    do_ocr      = '--ocr'  in sys.argv
    mat_type    = None

    if '--type' in sys.argv:
        idx = sys.argv.index('--type')
        if idx + 1 < len(sys.argv):
            mat_type = sys.argv[idx + 1]

    run_pipeline(material_type=mat_type, do_split=do_split, do_ocr=do_ocr)
