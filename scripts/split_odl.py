#!/usr/bin/env python3
"""
kordoc + v2 경계 탐지 기반 FB반 리뷰 PDF 분할 스크립트

기존 ODL(Java) 기반 접근의 한계:
  - Java 런타임 필수 (OpenJDK)
  - 폰트 크기/스타일 정보 미제공 → PyMuPDF로 이중 파싱 필요
  - 테이블 구조 미제공 → 텍스트 패턴으로만 noise 감지

kordoc 기반 접근:
  1. kordoc CLI(Node.js)로 PDF를 파싱 → IRBlock[] (type, text, pageNumber, style, table)
  2. IRBlock → element 변환 (heading/paragraph/table 구조 + font_size + font_ratio)
  3. detect_boundaries_v2로 다중 신호 경계 탐지
  4. fitz(PyMuPDF)로 해당 페이지 범위를 PDF로 분할

사용법:
  python3 split_odl.py --single <path>        # 단일 PDF
  python3 split_odl.py --dry-run              # 전체 dry-run
  python3 split_odl.py                        # 전체 처리
"""

import os
import re
import sys
import json
import hashlib
import subprocess
import unicodedata
import tempfile
from pathlib import Path
from typing import List, Optional

# ─── OCR 결과 디스크 캐시 ──────────────────────────────────────
# 동일 PDF 재처리 시 kordoc+OCR(20-60s)를 생략하여 즉시 응답.
# 키: PDF 바이트 SHA256, 값: (elements, total_pages) JSON
# 스키마 변경 시 CACHE_SCHEMA 버전을 올리면 기존 캐시 자동 무효화.

CACHE_SCHEMA = "v1"


def _cache_dir() -> Path:
    """XDG_CACHE_HOME 존중 ($HOME/.cache가 기본)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "itpe-splitter" / "ocr"


def _pdf_content_hash(pdf_path: str) -> Optional[str]:
    """PDF 파일 바이트의 SHA256. 실패 시 None (캐시 skip)."""
    try:
        h = hashlib.sha256()
        with open(pdf_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _cache_path(pdf_hash: str) -> Path:
    return _cache_dir() / f"{pdf_hash}_{CACHE_SCHEMA}.json"


def _cache_load(pdf_path: str) -> Optional[tuple]:
    """캐시 히트 시 (elements, total_pages) 반환. 실패/미스 시 None."""
    pdf_hash = _pdf_content_hash(pdf_path)
    if not pdf_hash:
        return None
    try:
        path = _cache_path(pdf_hash)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        elements = data.get("elements")
        total_pages = data.get("total_pages")
        if isinstance(elements, list) and isinstance(total_pages, int):
            print(f"  [cache-hit] OCR 캐시 로드 → {total_pages}p, "
                  f"{len(elements)} elements ({path.name[:16]}…)")
            return elements, total_pages
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _cache_save(pdf_path: str, elements: list, total_pages: int) -> None:
    """캐시 저장. 실패는 무음 (원본 결과는 이미 유효)."""
    pdf_hash = _pdf_content_hash(pdf_path)
    if not pdf_hash:
        return
    try:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(pdf_hash)
        path.write_text(
            json.dumps({"elements": elements, "total_pages": total_pages},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  [cache-save] OCR 결과 캐시 저장 ({path.name[:16]}…)")
    except OSError:
        pass
from datetime import datetime

import fitz  # PyMuPDF (PDF 분할용)
from detect_boundaries_v2 import detect_boundaries_v2

# kordoc CLI 경로 (환경변수 또는 로컬 빌드)
KORDOC_CLI = os.environ.get("KORDOC_CLI", "/tmp/kordoc/dist/cli.js")

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


# ─── kordoc PDF 파싱 ─────────────────────────────────────────────

def parse_kordoc(pdf_path: str) -> tuple[list, int]:
    """
    kordoc CLI로 PDF를 파싱하고 detect_boundaries_v2 호환 elements를 반환.

    kordoc IRBlock → element 변환 규칙:
      - heading → heading (font_size/font_ratio 포함)
      - paragraph → paragraph
      - list → paragraph (텍스트 평탄화)
      - table → 셀 텍스트를 개별 paragraph로 추출 + is_table 마킹

    Returns: (elements, total_pages)
    """
    # 디스크 캐시 조회 — 동일 PDF는 kordoc+OCR 완전 생략
    cached = _cache_load(pdf_path)
    if cached is not None:
        return cached

    result = subprocess.run(
        ["node", KORDOC_CLI, pdf_path, "--format", "json",
         "--no-header-footer", "--silent"],
        capture_output=True, text=True, timeout=60,
    )
    # kordoc가 Warning을 stdout에 출력할 수 있으므로
    # JSON 시작 위치({)를 찾아서 파싱
    raw = result.stdout
    json_start = raw.find("{")

    # 이미지 기반 PDF: kordoc가 FAIL (returncode!=0) 또는 JSON 없음
    # → PyMuPDF OCR로 전체 폴백
    if result.returncode != 0 or json_start < 0:
        stderr_hint = result.stderr[:200] if result.stderr else ""
        is_image_fail = "이미지 기반" in stderr_hint or "0자" in stderr_hint
        if is_image_fail or json_start < 0:
            print(f"  [kordoc] 이미지 기반 PDF 감지 — OCR 폴백")
            # 페이지 수만 fitz로 취득 후 전체 OCR
            try:
                doc = fitz.open(pdf_path)
                total_pages = doc.page_count
                doc.close()
            except Exception:
                total_pages = 0
            if total_pages > 0:
                elements = _ocr_image_pdf(pdf_path, [], total_pages)
                _cache_save(pdf_path, elements, total_pages)
                return elements, total_pages
            raise RuntimeError(f"이미지 PDF 페이지 수 확인 실패")
        raise RuntimeError(f"kordoc 실패: {stderr_hint}")

    data = json.loads(raw[json_start:])
    if not data.get("success", True):
        raise RuntimeError(f"kordoc 파싱 실패: {data.get('error', '?')}")

    blocks = data.get("blocks", [])
    metadata = data.get("metadata", {})
    total_pages = metadata.get("pageCount") or data.get("totalPages") or 0
    if not total_pages and blocks:
        total_pages = max((b.get("pageNumber", 0) for b in blocks), default=0)
    is_image_based = data.get("isImageBased", False)

    if is_image_based:
        print(f"  [kordoc] 이미지 기반 PDF — OCR 보강")

    # median font size 계산 (font_ratio용)
    all_sizes = [
        b["style"]["fontSize"] for b in blocks
        if b.get("style", {}).get("fontSize")
    ]
    if all_sizes:
        sorted_sizes = sorted(all_sizes)
        mid = len(sorted_sizes) // 2
        median_size = ((sorted_sizes[mid - 1] + sorted_sizes[mid]) / 2
                       if len(sorted_sizes) % 2 == 0 else sorted_sizes[mid])
    else:
        median_size = 10.0  # fallback

    elements = []
    for block in blocks:
        pg = block.get("pageNumber", 0)
        if not pg:
            continue
        btype = block.get("type", "paragraph")
        text = (block.get("text") or "").strip()
        fs = block.get("style", {}).get("fontSize", 0)
        ratio = fs / median_size if fs and median_size > 0 else 1.0

        if btype == "table":
            # 테이블: 셀 텍스트를 개별 element로 추출
            table_data = block.get("table", {})
            cells = table_data.get("cells", [])
            for row in cells:
                for cell in row:
                    ct = (cell.get("text") or "").strip()
                    if ct and len(ct) > 2:
                        elements.append({
                            "type": "paragraph", "page": pg,
                            "content": ct,
                            "is_table_cell": True,
                        })
            # 테이블 자체도 noise 감지용으로 마킹
            elements.append({
                "type": "table_marker", "page": pg,
                "content": f"[TABLE {table_data.get('rows',0)}x{table_data.get('cols',0)}]",
                "table_rows": table_data.get("rows", 0),
                "table_cols": table_data.get("cols", 0),
            })
        elif btype == "list":
            # 리스트: 텍스트를 줄 단위로 분리하여 paragraph로 추가
            for line in text.splitlines():
                line = line.strip().lstrip("-•·○●◦▪▸►").strip()
                if line and len(line) > 2:
                    elements.append({
                        "type": "paragraph", "page": pg,
                        "content": line,
                        "font_size": fs, "font_ratio": ratio,
                    })
        elif btype == "heading":
            elements.append({
                "type": "heading", "page": pg,
                "content": text,
                "font_size": fs, "font_ratio": ratio,
                "heading_level": block.get("level", 0),
            })
        else:  # paragraph, separator, image, etc.
            if text and len(text) > 1:
                elements.append({
                    "type": "paragraph", "page": pg,
                    "content": text,
                    "font_size": fs, "font_ratio": ratio,
                })

    # 이미지 기반 PDF면 OCR 보강 시도
    if is_image_based and total_pages > 0:
        elements = _ocr_image_pdf(pdf_path, elements, total_pages)

    _cache_save(pdf_path, elements, total_pages)
    return elements, total_pages


def _ocr_one_page(pdf_path: str, page_idx: int) -> tuple:
    """OCR 워커 — 단일 페이지 처리.

    각 워커가 독립된 fitz.Document를 열어 PyMuPDF 스레드 안전 가이드 준수.
    (같은 Document 공유는 스레드 간 금지, 별도 open은 안전)

    Returns:
        (page_num_1based, lines_list)
    """
    lines: list[str] = []
    try:
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_idx]
            tp = page.get_textpage_ocr(
                language="kor+eng", dpi=200, full=False)
            text = page.get_text(textpage=tp)
            for ln in text.splitlines():
                ln = ln.strip()
                if ln and len(ln) > 2:
                    lines.append(ln)
        finally:
            doc.close()
    except Exception:
        pass
    return page_idx + 1, lines


def _ocr_image_pdf(pdf_path: str, elements: list, total_pages: int) -> list:
    """이미지 기반 PDF에 PyMuPDF Tesseract OCR 적용 (kordoc이 isImageBased 감지).

    페이지 수가 4 이상이면 ThreadPoolExecutor로 병렬 처리.
    Tesseract/PyMuPDF가 대부분의 작업에서 GIL을 해제하므로 스레드 병렬 효과 있음.
    각 워커는 독립된 fitz.open()으로 Document 공유를 피함.

    M4 Pro 8코어 기준 ~3~4배 단축 기대 (순차 19.5s → 병렬 ~5~7s).
    """
    if total_pages <= 0:
        return elements

    ocr_extras: list = []
    workers = 1

    # 소형 PDF: 프로세스 기동 오버헤드 회피하여 순차 처리
    if total_pages < 4:
        for pi in range(total_pages):
            _, lines = _ocr_one_page(pdf_path, pi)
            for ln in lines:
                ocr_extras.append({
                    "type": "paragraph", "page": pi + 1,
                    "content": ln, "source": "ocr",
                })
    else:
        # ProcessPool + fork mode:
        # - Tesseract/PyMuPDF는 스레드 안전 보장 없음 → ProcessPool 필수
        # - fork는 부모 메모리 공유해 기동 빠름, 자식은 split_odl 재-import 안 함
        # - macOS/Linux 둘 다 fork 지원 (Windows 미지원은 이 프로젝트 범위 밖)
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        cpu = os.cpu_count() or 4
        workers = min(total_pages, max(1, cpu - 2), 8)
        ctx = mp.get_context("fork")
        try:
            with ProcessPoolExecutor(max_workers=workers,
                                     mp_context=ctx) as pool:
                futures = [pool.submit(_ocr_one_page, pdf_path, i)
                           for i in range(total_pages)]
                for f in futures:
                    page_num, lines = f.result()
                    for ln in lines:
                        ocr_extras.append({
                            "type": "paragraph", "page": page_num,
                            "content": ln, "source": "ocr",
                        })
        except Exception as e:
            # 병렬 실패 시 순차 폴백 (graceful degradation)
            print(f"  [OCR] 병렬 실패 → 순차 폴백: {e}")
            ocr_extras.clear()
            for pi in range(total_pages):
                _, lines = _ocr_one_page(pdf_path, pi)
                for ln in lines:
                    ocr_extras.append({
                        "type": "paragraph", "page": pi + 1,
                        "content": ln, "source": "ocr",
                    })

    if ocr_extras:
        suffix = f" (병렬 {workers})" if total_pages >= 4 else ""
        print(f"  [OCR] {total_pages}개 이미지 페이지{suffix} "
              f"→ {len(ocr_extras)}개 element 추가")

    return elements + ocr_extras


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
        is_question_sheet = (b.get("fmt") == "question_pages")
        # 메타 정보가 있으면 기존 형식, 없으면 교시+번호 기반 간결 형식
        has_meta = any([gen.strip("_ "), week.strip("_ "),
                        subject.strip("_ "), session.strip("_ ")])
        if has_meta:
            if is_question_sheet:
                sess_num = b.get("session", 0)
                fname = (f"{gen}_{safe_filename(week, 20)}_{subject}"
                         f"_문제지_{sess_num}교시.pdf")
            else:
                fname = (f"{gen}_{safe_filename(week, 20)}_{subject}"
                         f"_{session}_Q{b['num']:02d}_{topic_name}.pdf")
        else:
            sess_num = b.get("session", 0)
            if is_question_sheet:
                fname = f"문제지_{sess_num}교시.pdf"
            elif sess_num:
                fname = (f"Q{b['num']:02d}_{sess_num}교시_{b.get('session_q', 0)}번"
                         f"_{topic_name}.pdf")
            else:
                fname = f"Q{b['num']:02d}_{topic_name}.pdf"
        out_path = os.path.join(output_dir, fname)

        # PDF 메타데이터에 제목 + 키워드 기록
        keywords = b.get("keywords", [])
        meta = {
            "title": b["title"],
            "subject": ", ".join(keywords) if keywords else b["title"],
            "keywords": ", ".join(keywords) if keywords else "",
        }
        new_doc.set_metadata(meta)
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
            "keywords": b.get("keywords", []),
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
    print(f" kordoc 기반 FB반 리뷰 PDF 분할 파이프라인")
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
            elements, total_pages = parse_kordoc(pdf["path"])
        except Exception as e:
            print(f"  ✗ kordoc 파싱 실패: {e}")
            failed.append({"pdf": pdf["filename"], "error": str(e)})
            continue

        if not elements:
            print(f"  ✗ kordoc 출력 없음")
            failed.append({"pdf": pdf["filename"], "error": "no elements"})
            continue

        boundaries_v2, warnings_v2 = detect_boundaries_v2(
            elements, total_pages, pdf.get("session", ""))

        # TopicBoundary dataclass → dict 변환 (split_pdf 호환)
        boundaries = [
            {
                "num": b.num,
                "title": b.title,
                "page": b.page_start,
                "page_start": b.page_start,
                "page_end": b.page_end,
                "fmt": b.fmt,
                "session": b.session,
                "confidence": b.confidence,
            }
            for b in boundaries_v2
        ]

        if not boundaries:
            print(f"  ✗ 경계 미탐지 (elements={len(elements)}, pages={total_pages})")
            failed.append({"pdf": pdf["filename"], "error": "no boundaries"})
            continue

        fmt = boundaries[0].get("fmt", "?")
        summary = [(b["num"], f"p{b['page_start']}-{b['page_end']}") for b in boundaries]
        print(f"  포맷: {fmt} | 페이지: {total_pages} | 경계: {len(boundaries)}개 → {summary}")
        for w in warnings_v2:
            print(f"  ⚠ {w}")

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
