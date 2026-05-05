"""코퍼스 회귀 테스트 — 분류기 + A/B 엔진 정확도 측정.

사용법:
    python tests/corpus_test.py
    python tests/corpus_test.py --csv out.csv

출력:
    - 회차별 정확도 (분류 / A 엔진 카운트 / B 엔진 카운트)
    - 통계 요약 (몇 회차에서 A/B 둘 다 정확한지 등)
    - --csv 옵션 시 결과 CSV 저장 (PTS 강화 추적용)

PTS 정확도가 발전하면 그 추이를 CSV diff로 정량 검증 가능.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import unicodedata
from pathlib import Path

# 모듈 경로 — tests/ 에서 실행 시 scripts/ 임포트
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

from parsers.classifier import detect_publisher_and_type
from parsers.base import EXAM_META
from parsers.pts import parse_pts
from diagnose_itpe_mock import is_itpe_mock_pdf, split_itpe_mock
from diagnose_kpc_mock import is_kpc_mock_pdf, split_kpc_mock


CORPUS_ROOTS = [
    Path("/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/1_기출 해설"),
    Path("/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs/공부/2_모의고사"),
]

# 분할 대상 아닌 파일 패턴 (작성답안, 조각, 총평 등)
SKIP_FILE_TOKENS = ["작성답안", "총평", "멘토링"]
SKIP_PATH_TOKENS = ["bak", "_split"]
SKIP_NAME_PATTERNS = [
    "교시.pdf",  # 회차별 교시 분리본
    " 답.pdf",
    "-답1", "-답2", "-답3", "-답4",
]


def expected_total(pub: str, et: str) -> int:
    meta = EXAM_META.get((pub, et))
    if not meta:
        return 0
    return sum(meta.values())


def is_target_pdf(path: Path) -> bool:
    """분할 대상 PDF인지 (조각/답안 등 제외)."""
    name = path.name
    if any(t in name for t in SKIP_FILE_TOKENS):
        return False
    for p in SKIP_NAME_PATTERNS:
        if p in name:
            return False
    return True


def collect_pdfs() -> list[Path]:
    suffix = unicodedata.normalize("NFD", ".pdf")
    out = []
    for root in CORPUS_ROOTS:
        if not root.exists():
            continue
        for d, _, files in os.walk(root):
            if any(t in d for t in SKIP_PATH_TOKENS):
                continue
            for f in sorted(files):
                if not f.endswith(suffix):
                    continue
                p = Path(d) / f
                if not is_target_pdf(p):
                    continue
                out.append(p)
    return out


def run_a_engine(p: Path) -> dict | None:
    """A 엔진 결과 — 결정적 파서가 ok=True 반환한 경우만."""
    if is_itpe_mock_pdf(p):
        r = split_itpe_mock(p, Path(tempfile.mkdtemp()))
        if r.get("ok") and r.get("topics"):
            return r
    if is_kpc_mock_pdf(p):
        r = split_kpc_mock(p, Path(tempfile.mkdtemp()))
        if r.get("ok") and r.get("topics"):
            return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=str, help="결과 CSV 경로")
    args = ap.parse_args()

    pdfs = collect_pdfs()
    print(f"코퍼스: {len(pdfs)} 파일")

    rows = []
    for p in pdfs:
        try:
            pub, et = detect_publisher_and_type(p)
        except Exception:
            pub, et = "unknown", "unknown"
        exp = expected_total(pub, et)

        # B 엔진
        try:
            b_res = parse_pts(p)
            b_count = len(b_res.topics) if b_res.ok else 0
            b_ok = b_res.ok
        except Exception:
            b_count = 0
            b_ok = False

        # A 엔진 (모의고사 결정적만)
        try:
            a_res = run_a_engine(p)
            a_count = len(a_res["topics"]) if a_res else 0
            a_available = a_res is not None
        except Exception:
            a_count = 0
            a_available = False

        rows.append({
            "file": p.name,
            "publisher": pub,
            "exam_type": et,
            "expected": exp,
            "a_count": a_count,
            "a_match": (a_available and exp > 0 and a_count == exp),
            "b_count": b_count,
            "b_ok": b_ok,
            "b_match": (b_ok and exp > 0 and b_count == exp),
        })

    # 요약
    total = len(rows)
    a_match = sum(1 for r in rows if r["a_match"])
    b_match = sum(1 for r in rows if r["b_match"])
    both = sum(1 for r in rows if r["a_match"] and r["b_match"])
    a_only = sum(1 for r in rows if r["a_match"] and not r["b_match"])
    b_only = sum(1 for r in rows if r["b_match"] and not r["a_match"])
    classify_known = sum(
        1 for r in rows
        if r["publisher"] != "unknown" and r["exam_type"] != "unknown"
    )

    print()
    print("─" * 60)
    print(f"총 {total}건")
    print(f"  분류 식별: {classify_known}/{total} ({100*classify_known/total:.1f}%)")
    print(f"  A 엔진(결정적) 정확: {a_match}건")
    print(f"  B 엔진(PTS) 정확:   {b_match}건")
    print(f"  A·B 둘 다 정확: {both}건")
    print(f"  A만 정확: {a_only}건")
    print(f"  B만 정확: {b_only}건")
    print("─" * 60)

    # 분류별 분포
    from collections import Counter
    by_cls = Counter((r["publisher"], r["exam_type"]) for r in rows)
    print("\n분류별 분포:")
    for (pub, et), n in sorted(by_cls.items()):
        a = sum(1 for r in rows if r["publisher"] == pub and r["exam_type"] == et and r["a_match"])
        b = sum(1 for r in rows if r["publisher"] == pub and r["exam_type"] == et and r["b_match"])
        print(f"  ({pub}, {et}): {n}건 — A 정확 {a}, B 정확 {b}")

    # CSV 저장
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV 저장: {args.csv}")


if __name__ == "__main__":
    main()
