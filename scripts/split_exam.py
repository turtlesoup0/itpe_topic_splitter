#!/usr/bin/env python3
"""
기출 해설 PDF 분할 스크립트

지원 출처: KPC, ITPE, 동기회 (아이리포는 텍스트 추출 제한으로 제외)
지원 회차: 137회, 138회 (--exam 으로 지정)

사용법:
  python3 split_exam.py                      # 137회 dry-run (기본)
  python3 split_exam.py --exam 138            # 138회 dry-run
  python3 split_exam.py --exam 138 --run      # 138회 실제 분할
  python3 split_exam.py --exam 138 --run --ocr  # 138회 분할 + OCR
"""

import os, re, sys, json, unicodedata
import fitz
from typing import List, Tuple, Dict
from datetime import datetime
import subprocess, shutil

# ─── Configuration ─────────────────────────────────────────────
EXAM_BASE = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/1_기출 해설"
PROJECT_DIR = "/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter"
DATA_DIR = os.path.join(PROJECT_DIR, "data")
OCRMYPDF_CMD = "/Users/turtlesoup0-macmini/Library/Python/3.9/bin/ocrmypdf"
OCR_LANG = "kor+eng"


# ─── Helpers ───────────────────────────────────────────────────
def nfc(s):
    return unicodedata.normalize('NFC', s)


def safe_filename(s, max_len=80):
    s = nfc(s)
    s = re.sub(r'[/\\:*?"<>|]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:max_len].rstrip() if len(s) > max_len else s


def detect_source(fn):
    fn = nfc(fn)
    if 'KPC' in fn.upper():
        return 'KPC'
    if 'ITPE' in fn.upper():
        return 'ITPE'
    if '\ub3d9\uae30\ud68c' in fn:
        return '\ub3d9\uae30\ud68c'
    if '\uc544\uc774\ub9ac\ud3ec' in fn:
        return '\uc544\uc774\ub9ac\ud3ec'
    return 'UNKNOWN'


def detect_exam(fn):
    fn = nfc(fn)
    if '\uad00' in fn:
        return '\uad00'
    if '\uc751' in fn:
        return '\uc751'
    return '?'


# ─── Session Detection ─────────────────────────────────────────
def find_sessions(doc, source, exam_type):
    if source in ('KPC', 'ITPE'):
        return _sessions_by_header(doc, exam_type)
    elif source == '\ub3d9\uae30\ud68c':
        return _sessions_by_footer(doc, exam_type)
    return []


def _sessions_by_header(doc, exam_type):
    """KPC/ITPE: session start pages have 교시 + 문제 중/선택"""
    sessions = []
    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ""
        m = re.search(r'(?:\uc81c\s*)?(\d)\s*\uad50\uc2dc', text)
        if m and ('\ubb38\uc81c \uc911' in text or '\uc120\ud0dd' in text):
            sess = int(m.group(1))
            if not any(s['session'] == sess for s in sessions):
                sessions.append({'session': sess, 'start': pi, 'exam': exam_type})
    _assign_ends(sessions, doc.page_count)
    return sessions


def _sessions_by_footer(doc, exam_type):
    """동기회: blank page separators, 교시 on every content page"""
    sessions = []
    prev = None
    for pi in range(doc.page_count):
        text = (doc[pi].get_text() or "").strip()
        if len(text) < 50:
            prev = None
            continue
        m = re.search(r'(\d)\s*\uad50\uc2dc', text)
        if m:
            sess = int(m.group(1))
            if sess != prev:
                prev = sess
                if not any(s['session'] == sess for s in sessions):
                    sessions.append({'session': sess, 'start': pi, 'exam': exam_type})
    _assign_ends(sessions, doc.page_count)
    for s in sessions:
        while s['end'] > s['start'] and len((doc[s['end']].get_text() or "").strip()) < 50:
            s['end'] -= 1
    return sessions


def _assign_ends(sessions, total):
    # Sort by session number to handle out-of-order PDFs
    sessions.sort(key=lambda s: s['session'])
    for i, s in enumerate(sessions):
        s['end'] = sessions[i + 1]['start'] - 1 if i + 1 < len(sessions) else total - 1


# ─── Problem List Extraction ──────────────────────────────────
def extract_problem_list(doc, sess):
    """Extract problem titles from session start page"""
    text = doc[sess['start']].get_text() or ""
    problems = []
    lines = text.split('\n')
    in_list = False
    for line in lines:
        ls = line.strip()
        if '\ubb38\uc81c \uc911' in ls or '\uc120\ud0dd' in ls:
            in_list = True
            continue
        if in_list:
            m = re.match(r'^(\d{1,2})\.\s+(.+)', ls)
            if m:
                num = int(m.group(1))
                title = m.group(2).strip()
                if title and len(title) > 1:
                    problems.append((num, title))
    return problems


# ─── Boundary Detection ──────────────────────────────────────
def find_boundaries(doc, problems, sess, source):
    # Always include full expected range so boundary detection doesn't miss problems
    expected = list(range(1, 14)) if sess['session'] == 1 else list(range(1, 7))
    existing_nums = set(p[0] for p in problems)
    for n in expected:
        if n not in existing_nums:
            problems.append((n, 'Q%d' % n))
    problems.sort(key=lambda x: x[0])

    if source == 'KPC':
        return _bounds_kpc(doc, problems, sess)
    elif source == 'ITPE':
        return _bounds_itpe(doc, problems, sess)
    elif source == '\ub3d9\uae30\ud68c':
        return _bounds_dongkihoe(doc, problems, sess)
    return []


def _bounds_kpc(doc, problems, sess):
    """KPC: '제N.' or 'N.' with 문/제 in prev lines"""
    ptitles = {p[0]: p[1] for p in problems}
    pnums = set(p[0] for p in problems)
    boundaries = []

    for pi in range(sess['start'] + 1, sess['end'] + 1):
        text = doc[pi].get_text() or ""
        lines = text.split('\n')

        for li in range(min(20, len(lines))):
            ls = lines[li].strip()
            num, title = None, None

            # Pattern 1: "제N. title" or "제 N. title"
            m = re.match(r'^\uc81c\s*(\d{1,2})\.\s+(.+)', ls)
            if m:
                num = int(m.group(1))
                title = m.group(2).strip()

            # Pattern 2: "N. title" with "문/제" in prev lines
            if num is None:
                m2 = re.match(r'^(\d{1,2})\.\s+(.+)', ls)
                if m2:
                    prev = '\n'.join(l.strip() for l in lines[max(0, li - 5):li])
                    if '\ubb38' in prev and '\uc81c' in prev:
                        num = int(m2.group(1))
                        title = m2.group(2).strip()

            if num is None or num not in pnums:
                continue
            if any(b['num'] == num for b in boundaries):
                continue

            boundaries.append({
                'num': num,
                'title': ptitles.get(num, title or ''),
                'page_idx': pi,
                'score': 15,
            })

    boundaries.sort(key=lambda x: x['page_idx'])
    _assign_boundary_ends(boundaries, sess['end'])
    return boundaries


def _bounds_itpe(doc, problems, sess):
    """ITPE: '01'/'02' on own line, followed by title then 문제 keyword"""
    ptitles = {p[0]: p[1] for p in problems}
    pnums = set(p[0] for p in problems)
    boundaries = []

    for pi in range(sess['start'] + 1, sess['end'] + 1):
        text = doc[pi].get_text() or ""
        lines = text.split('\n')

        # ITPE layout varies across sessions:
        #   1교시: L0=header, L1-L2=blank, L3=copyright, L4=page_num, L5=q_num
        #   2-4교시: L0=header, L1=copyright, L2=page_num, L3=q_num
        # Page numbers (10-13) can false-match as question numbers on 1교시.
        # Fix: collect all candidates per page, keep the LAST one (highest line idx)
        # since page numbers always appear before question numbers.
        best = None
        for li in range(min(10, len(lines))):
            ls = lines[li].strip()
            m = re.match(r'^(\d{2})$', ls)
            if not m:
                continue
            num = int(m.group(1))
            if num not in pnums:
                continue

            # Verify: "문제" must appear as a STANDALONE line within 4 lines
            after_lines = [l.strip() for l in lines[li + 1:li + 5]]
            if not any(al == '\ubb38\uc81c' for al in after_lines):
                continue

            # Keep the last valid match (question num comes after page num)
            best = (li, num)

        if best is None:
            continue
        li, num = best

        # Get title from next meaningful line
        title = ""
        for nl in lines[li + 1:li + 4]:
            nls = nl.strip()
            if nls and len(nls) > 3 and nls != '\ubb38\uc81c':
                title = nls
                break

        boundaries.append({
            'num': num,
            'title': title or '',
            'page_idx': pi,
            'score': 15,
        })

    boundaries.sort(key=lambda x: x['page_idx'])

    # Post-process: fix wrong/duplicate question numbers from PDF template errors
    expected_count = 13 if sess['session'] == 1 else 6
    if len(boundaries) == expected_count:
        # Count matches expected → renumber sequentially by page position
        for i, b in enumerate(boundaries):
            correct_num = i + 1
            if correct_num in ptitles:
                b['title'] = ptitles[correct_num]
            b['num'] = correct_num
    else:
        # Fewer than expected → remove duplicates (keep first occurrence)
        seen = set()
        deduped = []
        for b in boundaries:
            if b['num'] not in seen:
                seen.add(b['num'])
                deduped.append(b)
            elif b['num'] in ptitles:
                b['title'] = ptitles[b['num']]
        boundaries = deduped

    _assign_boundary_ends(boundaries, sess['end'])
    return boundaries


def _bounds_dongkihoe(doc, problems, sess):
    """동기회: Redirect to all-page scan filtered by session"""
    # This is a stub — the actual work is done by scan_dongkihoe_all
    return []


def scan_dongkihoe_all(doc):
    """동기회: Scan ALL pages for 'N교시 N번' markers (session-independent)"""
    MAX_Q = {1: 13, 2: 6, 3: 6, 4: 6}
    boundaries = []

    for pi in range(doc.page_count):
        text = doc[pi].get_text() or ""
        m = re.search(r'(\d)\s*\uad50\uc2dc\s*\n\s*(\d{1,2})\s*\ubc88', text[:500])
        if not m:
            continue
        sess_num, qnum = int(m.group(1)), int(m.group(2))

        # Validate session+question combination (1교시: Q1-13, 2-4교시: Q1-6)
        if qnum > MAX_Q.get(sess_num, 6):
            # Invalid combo — likely a template typo; fall back to header's 교시
            header_m = re.search(r'(\d)\s*\uad50\uc2dc', text[:80])
            if header_m:
                alt_sess = int(header_m.group(1))
                if qnum <= MAX_Q.get(alt_sess, 13):
                    sess_num = alt_sess
        key = (sess_num, qnum)
        if any((b['detected_session'], b['num']) == key for b in boundaries):
            continue

        # Extract title from "문제" label
        tm = re.search(
            r'\ubb38\uc81c\s*\n(.+?)(?:\n\ub3c4\uba54\uc778|\n\ub09c\uc774\ub3c4|\n\ucd9c\uc81c)',
            text[:1200], re.DOTALL
        )
        title = tm.group(1).strip().split('\n')[0][:80] if tm else 'Q%d' % qnum

        boundaries.append({
            'num': qnum,
            'title': title,
            'page_idx': pi,
            'detected_session': sess_num,
            'score': 20,
        })

    boundaries.sort(key=lambda x: x['page_idx'])

    # Assign page ends based on physical page order
    for i, b in enumerate(boundaries):
        if i + 1 < len(boundaries):
            b['page_end'] = boundaries[i + 1]['page_idx'] - 1
        else:
            b['page_end'] = doc.page_count - 1

    # Trim trailing blank pages
    for b in boundaries:
        while b['page_end'] > b['page_idx']:
            if len((doc[b['page_end']].get_text() or "").strip()) < 50:
                b['page_end'] -= 1
            else:
                break

    # Remove invalid entries
    boundaries[:] = [b for b in boundaries if b['page_end'] >= b['page_idx']]
    return boundaries


def _assign_boundary_ends(boundaries, session_end):
    for i, b in enumerate(boundaries):
        b['page_end'] = boundaries[i + 1]['page_idx'] - 1 if i + 1 < len(boundaries) else session_end
    # Remove invalid entries where page_end < page_idx
    boundaries[:] = [b for b in boundaries if b['page_end'] >= b['page_idx']]


# ─── PDF Splitting ──────────────────────────────────────────────
def split_pdf(source_path, boundaries, output_dir, source, exam, session_num):
    doc = fitz.open(source_path)
    results = []

    for b in boundaries:
        sp, ep = b['page_idx'], min(b['page_end'], doc.page_count - 1)
        if sp > ep:
            continue

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=sp, to_page=ep)

        topic = safe_filename(b['title'], 50)
        fname = '%s_%s_%d\uad50\uc2dc_Q%02d_%s.pdf' % (source, exam, session_num, b['num'], topic)
        out_path = os.path.join(output_dir, fname)
        new_doc.save(out_path)
        new_doc.close()

        img = sum(1 for p in range(sp, ep + 1)
                  if len((doc[p].get_text() or "").strip()) < 50)

        results.append({
            'filename': fname, 'path': out_path,
            'source': source, 'exam': exam,
            'session': session_num, 'q_num': b['num'], 'q_title': b['title'],
            'pages': ep - sp + 1, 'image_pages': img,
            'needs_ocr': img > 0,
        })

    doc.close()
    return results


# ─── OCR ─────────────────────────────────────────────────────────
def apply_ocr(pdf_path):
    tmp = pdf_path + ".ocr_tmp.pdf"
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


# ─── Verification ──────────────────────────────────────────────
def verify_splits(results):
    """분할 결과 검증: 각 파일을 열어서 내용 확인"""
    issues = []
    for r in results:
        path = r['path']
        if not os.path.exists(path):
            issues.append((r['filename'], 'FILE_MISSING'))
            continue

        try:
            doc = fitz.open(path)
            if doc.page_count == 0:
                issues.append((r['filename'], 'EMPTY_PDF'))
                doc.close()
                continue

            # Check first page has content
            text = (doc[0].get_text() or "").strip()
            if len(text) < 30:
                issues.append((r['filename'],
                               'NO_TEXT (image-only: %d chars, %d pages)' % (len(text), doc.page_count)))

            # Check title presence (loose match)
            title_short = r['q_title'][:10] if r['q_title'] else ''
            if title_short and len(text) > 50:
                found = False
                for pi in range(doc.page_count):
                    pt = doc[pi].get_text() or ""
                    if title_short in pt:
                        found = True
                        break
                if not found:
                    issues.append((r['filename'],
                                   'TITLE_MISMATCH (expected: %s...)' % title_short))

            # Check file size
            fsize = os.path.getsize(path)
            if fsize < 1000:
                issues.append((r['filename'], 'TINY_FILE (%d bytes)' % fsize))

            doc.close()
        except Exception as e:
            issues.append((r['filename'], 'OPEN_ERROR: %s' % str(e)))

    return issues


# ─── Pipeline ──────────────────────────────────────────────────
def find_exam_pdfs(exam_dir):
    # Collect files: bak dir first (lower priority), then main dir (overwrites)
    file_map = {}  # dedup_key -> entry

    dirs_to_scan = []
    bak = os.path.join(exam_dir, "bak")
    if os.path.isdir(bak):
        dirs_to_scan.append(bak)
    dirs_to_scan.append(exam_dir)

    for d in dirs_to_scan:
        for f in os.listdir(d):
            fn = nfc(f)
            full = os.path.join(d, f)
            if not fn.endswith('.pdf') or not os.path.isfile(full):
                continue
            src = detect_source(fn)
            if src in ('UNKNOWN', '\uc544\uc774\ub9ac\ud3ec'):
                continue
            exam = detect_exam(fn)
            # Detect per-교시 session from filename (e.g., "ITPE 138관-2교시_v1.0.pdf")
            sess_m = re.search(r'(\d)\uad50\uc2dc', fn)
            sess = int(sess_m.group(1)) if sess_m else None
            # Dedup key: prefer main dir over bak, prefer latest version
            dedup_key = (src, exam, sess or 0)
            file_map[dedup_key] = {
                'path': full, 'filename': fn, 'source': src,
                'exam': exam, 'file_session': sess,
            }

    pdfs = list(file_map.values())
    return sorted(pdfs, key=lambda x: (x['source'], x['exam'], x.get('file_session') or 0))


def run_pipeline(exam_num=137, do_split=False, do_ocr=False):
    exam_dir = os.path.join(EXAM_BASE, str(exam_num))
    split_dir = os.path.join(PROJECT_DIR, "split_pdfs", "%d\ud68c" % exam_num)
    os.makedirs(split_dir, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    pdfs = find_exam_pdfs(exam_dir)

    mode = 'DRY-RUN' if not do_split else ('SPLIT+OCR' if do_ocr else 'SPLIT')
    print('\n' + '=' * 70)
    print(' %d\ud68c \uae30\ucd9c \ud574\uc124 PDF \ubd84\ud560 [%s]' % (exam_num, mode))
    print(' \ub300\uc0c1: %d\uac1c | \ucd9c\ub825: %s' % (len(pdfs), split_dir))
    print('=' * 70)

    all_results, failed = [], []
    total = 0

    for i, pdf in enumerate(pdfs):
        print('\n[%d/%d] %s %s \u2014 %s' % (i + 1, len(pdfs), pdf['source'], pdf['exam'], pdf['filename']))

        try:
            doc = fitz.open(pdf['path'])
        except Exception as e:
            print('  \u2717 %s' % e)
            failed.append({'pdf': pdf['filename'], 'error': str(e)})
            continue

        # ── 동기회: full-page scan (session labels unreliable) ──
        if pdf['source'] == '\ub3d9\uae30\ud68c':
            all_bounds = scan_dongkihoe_all(doc)
            if not all_bounds:
                print('  \u2717 \ubb38\uc81c \uacbd\uacc4 \ubbf8\ud0d0\uc9c0')
                failed.append({'pdf': pdf['filename'], 'error': 'no bounds'})
                doc.close()
                continue

            # Group by detected session
            by_sess = {}
            for b in all_bounds:
                by_sess.setdefault(b['detected_session'], []).append(b)

            for sess_num in sorted(by_sess):
                bounds = by_sess[sess_num]
                b_str = ', '.join(
                    'Q%d(p%d-%d)' % (b['num'], b['page_idx'] + 1, b['page_end'] + 1)
                    for b in bounds
                )
                print('\n  \u2500\u2500 %d\uad50\uc2dc: %d\uac1c \u2192 %s' % (sess_num, len(bounds), b_str))
                total += len(bounds)

                if not do_split:
                    continue

                results = split_pdf(
                    pdf['path'], bounds, split_dir,
                    pdf['source'], pdf['exam'], sess_num
                )
                if do_ocr:
                    for r in results:
                        if r['needs_ocr']:
                            ok = apply_ocr(r['path'])
                            r['ocr_applied'] = ok
                            if ok:
                                print('    OCR: %s' % r['filename'])
                all_results.extend(results)
                print('    \u2192 %d\uac1c PDF \uc0dd\uc131' % len(results))

            doc.close()
            continue

        # ── KPC/ITPE: session-based processing ──
        # Per-교시 files (e.g., 138회): session already known from filename
        if pdf.get('file_session'):
            sessions = _sessions_by_header(doc, pdf['exam'])
            if not sessions:
                # Fallback: create session from filename info
                # Find first content page (skip blank/cover pages)
                start_idx = 0
                for pi in range(doc.page_count):
                    text = doc[pi].get_text() or ""
                    if '\uad50\uc2dc' in text and ('\ubb38\uc81c' in text or '\uc120\ud0dd' in text):
                        start_idx = pi
                        break
                sessions = [{
                    'session': pdf['file_session'],
                    'start': start_idx,
                    'end': doc.page_count - 1,
                    'exam': pdf['exam'],
                }]
        else:
            sessions = find_sessions(doc, pdf['source'], pdf['exam'])
        if not sessions:
            print('  \u2717 \uad50\uc2dc \ubbf8\ud0d0\uc9c0')
            failed.append({'pdf': pdf['filename'], 'error': 'no sessions'})
            doc.close()
            continue

        sess_str = ', '.join(
            '%d\uad50\uc2dc(p%d-%d)' % (s['session'], s['start'] + 1, s['end'] + 1)
            for s in sessions
        )
        print('  \uad50\uc2dc: %d\uac1c \u2192 %s' % (len(sessions), sess_str))

        for sess in sessions:
            print('\n  \u2500\u2500 %d\uad50\uc2dc \u2500\u2500' % sess['session'])

            problems = extract_problem_list(doc, sess)
            if problems:
                print('    \ubaa9\ub85d: %d\uac1c [%s]' % (
                    len(problems), ', '.join(str(p[0]) for p in problems)))
            else:
                expected = list(range(1, 14)) if sess['session'] == 1 else list(range(1, 7))
                problems = [(n, 'Q%d' % n) for n in expected]
                print('    \ubaa9\ub85d: \ubbf8\ud0d0\uc9c0 \u2192 \uae30\ubcf8\uac12 %d\uac1c' % len(problems))

            bounds = find_boundaries(doc, problems, sess, pdf['source'])
            if not bounds:
                print('    \u2717 \uacbd\uacc4 \ubbf8\ud0d0\uc9c0')
                failed.append({
                    'pdf': pdf['filename'],
                    'error': '%d\uad50\uc2dc no bounds' % sess['session']
                })
                continue

            b_str = ', '.join(
                'Q%d(p%d-%d)' % (b['num'], b['page_idx'] + 1, b['page_end'] + 1)
                for b in bounds
            )
            print('    \uacbd\uacc4: %d\uac1c \u2192 %s' % (len(bounds), b_str))

            found = set(b['num'] for b in bounds)
            expected_nums = set(p[0] for p in problems)
            missing = expected_nums - found
            if missing:
                print('    \u26a0 \ubbf8\ud0d0\uc9c0: %s' % sorted(missing))

            total += len(bounds)

            if not do_split:
                continue

            results = split_pdf(
                pdf['path'], bounds, split_dir,
                pdf['source'], sess.get('exam', pdf['exam']), sess['session']
            )

            if do_ocr:
                for r in results:
                    if r['needs_ocr']:
                        ok = apply_ocr(r['path'])
                        r['ocr_applied'] = ok
                        if ok:
                            print('    OCR: %s' % r['filename'])

            all_results.extend(results)
            print('    \u2192 %d\uac1c PDF \uc0dd\uc131' % len(results))

        doc.close()

    # ─── Report ─────────────────────────────────────────────────
    print('\n' + '=' * 70)
    action = '\ud0d0\uc9c0' if not do_split else '\uc0dd\uc131'
    print(' \uc644\ub8cc: %d\uac1c \ud1a0\ud53d %s' % (total, action))
    if failed:
        print(' \uc2e4\ud328: %d\uac1c' % len(failed))
        for f_item in failed:
            print('   - %s: %s' % (f_item['pdf'], f_item['error']))

    if do_split and all_results:
        print('\n \uac80\uc99d \uc911...')
        issues = verify_splits(all_results)
        if issues:
            print(' \u26a0 \uac80\uc99d \uc774\uc288 %d\uac1c:' % len(issues))
            for fname, issue in issues:
                print('   - %s: %s' % (fname, issue))
        else:
            print(' \u2713 \ubaa8\ub4e0 %d\uac1c \ud30c\uc77c \uac80\uc99d \ud1b5\uacfc' % len(all_results))

        # Per-source summary
        sources = {}
        for r in all_results:
            key = '%s %s' % (r['source'], r['exam'])
            sources[key] = sources.get(key, 0) + 1
        print('\n \ucd9c\ucc98\ubcc4:')
        for k, v in sorted(sources.items()):
            print('   %s: %d\uac1c' % (k, v))

        report = {
            'timestamp': datetime.now().isoformat(),
            'exam': '%d\ud68c' % exam_num, 'total': total,
            'results': all_results, 'failed': failed,
            'verification_issues': [{'file': f, 'issue': i} for f, i in issues],
        }
        rp = os.path.join(DATA_DIR, "exam%d_report.json" % exam_num)
        with open(rp, 'w', encoding='utf-8') as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
        print('\n \ub9ac\ud3ec\ud2b8: %s' % rp)

    print('=' * 70)
    return all_results


if __name__ == "__main__":
    do_split = "--run" in sys.argv
    do_ocr = "--ocr" in sys.argv
    exam_num = 137
    for i, a in enumerate(sys.argv):
        if a == '--exam' and i + 1 < len(sys.argv):
            exam_num = int(sys.argv[i + 1])
    run_pipeline(exam_num=exam_num, do_split=do_split, do_ocr=do_ocr)
