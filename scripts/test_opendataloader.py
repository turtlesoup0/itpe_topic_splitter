"""
opendataloader-pdf 성능 테스트 스크립트
- 3개 샘플 PDF에 대해 markdown, json, text 포맷으로 추출 성능 측정
"""

import time
import os
import json
import tempfile
from pathlib import Path
from opendataloader_pdf import convert

BASE_DIR = Path("/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter/split_pdfs")

# 다양한 유형의 샘플 PDF 3개 선택
SAMPLES = [
    BASE_DIR / "137회/ITPE_관_4교시_Q01_BPF(Berkeley Packet Filter door) 악성코드와 관련하여 다음을 설명.pdf",
    BASE_DIR / "19기/1주차-SW/19기_1주차-SW_SW_1교시_Q01_스크럼(Scrum).pdf",
    BASE_DIR / "19기/16주차-4교시 시험/19기_16주차-4교시 시험_ETC_2교시_Q01_AI 디지털교과서 도입을 앞두고 에듀테크 기업들의 시장 선점.pdf",
]

FORMATS = ["markdown", "json", "text"]


def test_pdf(pdf_path: Path, fmt: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        start = time.perf_counter()
        try:
            convert(
                input_path=str(pdf_path),
                output_dir=tmpdir,
                format=fmt,
                quiet=True,
                reading_order="xycut",
            )
            elapsed = time.perf_counter() - start

            # 출력 파일 찾기 (디렉토리 제외)
            ext_map = {"markdown": "md", "json": "json", "text": "txt"}
            ext = ext_map.get(fmt, fmt)
            out_files = [f for f in Path(tmpdir).rglob(f"*.{ext}") if f.is_file()]
            if not out_files:
                out_files = [f for f in Path(tmpdir).rglob("*") if f.is_file()]

            if out_files:
                content = out_files[0].read_text(encoding="utf-8", errors="ignore")
                char_count = len(content)
                line_count = content.count("\n")

                # 텍스트 품질 샘플 (처음 300자)
                preview = content[:300].replace("\n", "↵")
            else:
                char_count = 0
                line_count = 0
                preview = "(출력 없음)"

            return {
                "status": "OK",
                "elapsed_sec": round(elapsed, 3),
                "char_count": char_count,
                "line_count": line_count,
                "preview": preview,
            }
        except Exception as e:
            elapsed = time.perf_counter() - start
            return {
                "status": f"ERROR: {e}",
                "elapsed_sec": round(elapsed, 3),
                "char_count": 0,
                "line_count": 0,
                "preview": "",
            }


def run_tests():
    print("=" * 70)
    print("opendataloader-pdf 성능 테스트")
    print("=" * 70)

    results = {}

    for pdf_path in SAMPLES:
        name = pdf_path.name[:50]
        print(f"\n📄 PDF: {name}...")
        results[name] = {}

        for fmt in FORMATS:
            r = test_pdf(pdf_path, fmt)
            results[name][fmt] = r
            status_icon = "✅" if r["status"] == "OK" else "❌"
            print(
                f"  {status_icon} [{fmt:8s}] {r['elapsed_sec']:.3f}s | "
                f"{r['char_count']:,} chars | {r['line_count']:,} lines | "
                f"상태: {r['status']}"
            )
            if r["status"] == "OK" and fmt == "markdown":
                print(f"     미리보기: {r['preview'][:120]}")

    # 요약
    print("\n" + "=" * 70)
    print("요약")
    print("=" * 70)
    for name, fmts in results.items():
        print(f"\n{name}")
        for fmt, r in fmts.items():
            print(f"  {fmt:8s}: {r['elapsed_sec']:.3f}s, {r['char_count']:,} chars")

    # 포맷별 평균 속도
    print("\n포맷별 평균 처리 시간:")
    for fmt in FORMATS:
        times = [results[n][fmt]["elapsed_sec"] for n in results if results[n][fmt]["status"] == "OK"]
        if times:
            avg = sum(times) / len(times)
            print(f"  {fmt:8s}: 평균 {avg:.3f}s")


if __name__ == "__main__":
    run_tests()
