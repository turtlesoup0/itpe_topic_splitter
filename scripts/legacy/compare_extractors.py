"""
기존 PyMuPDF 방식 vs opendataloader-pdf 비교 분석
- 텍스트 추출 품질
- 처리 속도
- 이미지 페이지(OCR 필요) 처리 능력
- 구조 보존(표, 제목) 품질
"""

import time
import tempfile
import re
from pathlib import Path

import fitz  # PyMuPDF
from opendataloader_pdf import convert

BASE = Path("/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter/split_pdfs")

# 다양한 유형 커버 (텍스트 기반, 이미지 포함, 표 포함)
SAMPLES = [
    ("텍스트 기반 (1교시)", BASE / "19기/1주차-SW/19기_1주차-SW_SW_1교시_Q01_스크럼(Scrum).pdf"),
    ("긴 해설 (복잡 레이아웃)", BASE / "137회/ITPE_관_4교시_Q01_BPF(Berkeley Packet Filter door) 악성코드와 관련하여 다음을 설명.pdf"),
    ("AI 주제 (표+텍스트)", BASE / "19기/16주차-4교시 시험/19기_16주차-4교시 시험_ETC_2교시_Q01_AI 디지털교과서 도입을 앞두고 에듀테크 기업들의 시장 선점.pdf"),
    ("DB 주제", BASE / "19기/3주차-DB/19기_3주차-DB_DB_1교시_Q01_쿼리 오프로딩(Query offloading)과 CDC (Change Data Capture).pdf"),
    ("AI 1교시", BASE / "19기/7주차-AI/19기_7주차-AI_AI_1교시_Q01_분산(모델 민감도) 증가, 오버피팅(Overfitting) 개념.pdf"),
]


# ──────────────────────────────────────────────
# 기존 방식: PyMuPDF
# ──────────────────────────────────────────────
def extract_pymupdf(pdf_path: Path) -> dict:
    start = time.perf_counter()
    try:
        doc = fitz.open(str(pdf_path))
        pages_text = []
        img_pages = 0
        for i in range(doc.page_count):
            t = doc[i].get_text() or ""
            pages_text.append(t)
            if len(t.strip()) < 50:
                img_pages += 1
        full_text = "\n\n".join(pages_text)
        doc.close()
        elapsed = time.perf_counter() - start
        return {
            "method": "PyMuPDF",
            "elapsed_sec": round(elapsed, 4),
            "total_pages": len(pages_text),
            "img_pages": img_pages,
            "char_count": len(full_text),
            "line_count": full_text.count("\n"),
            "text": full_text,
            "error": None,
        }
    except Exception as e:
        return {"method": "PyMuPDF", "elapsed_sec": round(time.perf_counter() - start, 4),
                "error": str(e), "char_count": 0, "line_count": 0, "text": ""}


# ──────────────────────────────────────────────
# opendataloader 방식
# ──────────────────────────────────────────────
def extract_odl(pdf_path: Path, fmt: str = "markdown") -> dict:
    start = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            convert(
                input_path=str(pdf_path),
                output_dir=tmpdir,
                format=fmt,
                quiet=True,
                reading_order="xycut",
            )
            ext_map = {"markdown": "md", "json": "json", "text": "txt"}
            ext = ext_map.get(fmt, fmt)
            out_files = [f for f in Path(tmpdir).rglob(f"*.{ext}") if f.is_file()]
            if not out_files:
                out_files = [f for f in Path(tmpdir).rglob("*") if f.is_file()]

            elapsed = time.perf_counter() - start
            if out_files:
                content = out_files[0].read_text(encoding="utf-8", errors="ignore")
                return {
                    "method": f"ODL-{fmt}",
                    "elapsed_sec": round(elapsed, 4),
                    "total_pages": None,
                    "img_pages": None,
                    "char_count": len(content),
                    "line_count": content.count("\n"),
                    "text": content,
                    "error": None,
                }
            return {"method": f"ODL-{fmt}", "elapsed_sec": round(elapsed, 4),
                    "error": "no output", "char_count": 0, "line_count": 0, "text": ""}
    except Exception as e:
        return {"method": f"ODL-{fmt}", "elapsed_sec": round(time.perf_counter() - start, 4),
                "error": str(e), "char_count": 0, "line_count": 0, "text": ""}


# ──────────────────────────────────────────────
# 텍스트 품질 분석
# ──────────────────────────────────────────────
def analyze_quality(text: str) -> dict:
    if not text:
        return {}
    has_table = bool(re.search(r'\|.*\|', text))
    has_heading = bool(re.search(r'^#{1,3}\s', text, re.MULTILINE))
    korean_chars = len(re.findall(r'[\uAC00-\uD7A3]', text))
    # 한글 비율
    korean_ratio = round(korean_chars / max(len(text), 1), 3)
    # 토픽 관련 키워드 포함 여부
    keywords = ["출제의도", "작성방안", "Keyword", "출제빈도", "개요", "상세설명"]
    kw_found = [kw for kw in keywords if kw in text]
    # 이상한 글자 (OCR 노이즈 지표)
    noise_chars = len(re.findall(r'[□■●○◎△▲※★☆◆◇]', text))
    return {
        "has_table": has_table,
        "has_heading": has_heading,
        "korean_chars": korean_chars,
        "korean_ratio": korean_ratio,
        "keywords_found": kw_found,
        "noise_chars": noise_chars,
    }


# ──────────────────────────────────────────────
# 메인 비교
# ──────────────────────────────────────────────
def run_comparison():
    print("=" * 75)
    print("PyMuPDF vs opendataloader-pdf 비교 분석")
    print("=" * 75)

    summary = []

    for label, pdf_path in SAMPLES:
        if not pdf_path.exists():
            print(f"\n⚠ 파일 없음: {pdf_path.name}")
            continue

        print(f"\n{'─'*75}")
        print(f"📄 [{label}]")
        print(f"   {pdf_path.name[:70]}")
        print(f"{'─'*75}")

        r_mupdf = extract_pymupdf(pdf_path)
        r_odl_md = extract_odl(pdf_path, "markdown")
        r_odl_txt = extract_odl(pdf_path, "text")

        for r in [r_mupdf, r_odl_md, r_odl_txt]:
            q = analyze_quality(r.get("text", ""))
            err = r.get("error")
            status = "❌ " + err if err else "✅"
            pages_info = f"{r.get('total_pages','?')}p (이미지:{r.get('img_pages','?')}p)" if r.get("total_pages") else "n/a"
            print(f"\n  [{r['method']:12s}] {status}")
            print(f"    속도     : {r['elapsed_sec']:.4f}s")
            print(f"    페이지   : {pages_info}")
            print(f"    글자수   : {r['char_count']:,} chars / {r['line_count']:,} lines")
            if q:
                print(f"    한글비율 : {q['korean_ratio']:.1%} ({q['korean_chars']:,}자)")
                print(f"    표 포함  : {'O' if q['has_table'] else 'X'}  |  제목 포함: {'O' if q['has_heading'] else 'X'}")
                print(f"    키워드   : {q['keywords_found'] if q['keywords_found'] else '없음'}")
                print(f"    노이즈   : {q['noise_chars']}개")

        # 텍스트 미리보기 비교 (처음 200자)
        print(f"\n  [텍스트 미리보기 비교 (처음 200자)]")
        for r in [r_mupdf, r_odl_md]:
            preview = r.get("text","")[:200].replace("\n", "↵")
            print(f"  {r['method']:12s}: {preview}")

        summary.append({
            "label": label,
            "mupdf_speed": r_mupdf["elapsed_sec"],
            "odl_md_speed": r_odl_md["elapsed_sec"],
            "mupdf_chars": r_mupdf["char_count"],
            "odl_md_chars": r_odl_md["char_count"],
            "mupdf_img_pages": r_mupdf.get("img_pages", 0),
        })

    # 종합 요약
    print(f"\n\n{'='*75}")
    print("종합 요약")
    print(f"{'='*75}")
    print(f"{'항목':<30} {'PyMuPDF':>10} {'ODL-md':>10} {'속도차이':>10} {'글자차이':>10}")
    print(f"{'─'*75}")
    for s in summary:
        speed_ratio = s["odl_md_speed"] / max(s["mupdf_speed"], 0.001)
        char_ratio = (s["odl_md_chars"] - s["mupdf_chars"]) / max(s["mupdf_chars"], 1)
        print(f"{s['label']:<30} {s['mupdf_speed']:>9.3f}s {s['odl_md_speed']:>9.3f}s "
              f"{speed_ratio:>8.0f}x  {char_ratio:>+9.1%}")

    if summary:
        avg_mupdf = sum(s["mupdf_speed"] for s in summary) / len(summary)
        avg_odl = sum(s["odl_md_speed"] for s in summary) / len(summary)
        print(f"{'─'*75}")
        print(f"{'평균':<30} {avg_mupdf:>9.3f}s {avg_odl:>9.3f}s {avg_odl/avg_mupdf:>8.0f}x")

    print(f"\n{'='*75}")
    print("핵심 결론")
    print(f"{'='*75}")
    print("""
이 프로젝트의 핵심 기능별 opendataloader 적용 가능성:

1. [PDF 분할]        PyMuPDF만 가능 → ODL 대체 불가
                     PDFDocument 조작, 페이지 추출은 fitz.open()이 필요

2. [텍스트 추출]     ODL-markdown이 구조 보존 우수 (표, 제목)
                     단, 속도는 PyMuPDF 대비 ~수백배 느림

3. [경계 탐지]       정규식 기반 로직은 ODL과 무관 → 기존 유지

4. [OCR 처리]        ODL hybrid 모드로 대체 가능하나, 현재 split_pdfs는
                     이미 텍스트 레이어가 있어 OCR 불필요 비율이 높음

5. [이미지 페이지]   ODL이 이점 있음 (hybrid OCR 내장)
""")


if __name__ == "__main__":
    run_comparison()
