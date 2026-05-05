"""PDF (publisher, exam_type) 식별기.

분할 알고리즘에는 사용 안 함 — 결과 표/명명 라벨용.
LR-007: 시험 본질 토큰만 사용 (학원 슬로건/마케팅 문구 의존 금지).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

import fitz  # PyMuPDF


Publisher = Literal["ITPE", "KPC", "인포레버", "동기회", "unknown"]
ExamType = Literal["mock", "actual", "unknown"]


# 시험 종별 (시험 자체의 라벨 — 안정)
ACTUAL_RE = re.compile(r"기출\s*(문제|풀이|해설)|국가기술자격기술사시험문제")
MOCK_RE = re.compile(r"실전\s*(명품\s*)?모의고사|IMPACT\s*실전\s*모의고사")

# 학원 식별 (시험 브랜드 — 학원 명 + 출판물 종류 동시 등장 토큰)
ITPE_RE = re.compile(r"ITPE\s*\(?\s*Information Technology Professional Engineer|ITPE\s*실전\s*명품")
KPC_RE = re.compile(
    r"KPC\s*기술사.*?IMPACT|Korea\s*Productivity\s*Center|한국생산성본부"
)
INFOREVER_RE = re.compile(r"인포레버컨설팅|Big&Up\s*기술사회|인포레버")
DGH_RE = re.compile(r"기출풀이집|여울동기회|동기회")  # 동기회 시그널


def _read_head_text(pdf_path: Path, n_pages: int = 5) -> str:
    """첫 N페이지 텍스트를 합쳐 반환 (분류기용 헤드 검사)."""
    try:
        doc = fitz.open(pdf_path)
        head = ""
        for i in range(min(doc.page_count, n_pages)):
            head += doc.load_page(i).get_text() + "\n"
        doc.close()
        return head
    except Exception:
        return ""


def detect_publisher(head_text: str, filename: str) -> Publisher:
    """본문 헤드 + 파일명으로 학원 식별.

    본문 시그널이 더 신뢰도 높으나, 본문에 학원명이 없는 경우 파일명으로 fallback.
    """
    # 본문 시그널 우선
    if KPC_RE.search(head_text):
        return "KPC"
    if ITPE_RE.search(head_text):
        return "ITPE"
    if INFOREVER_RE.search(head_text):
        return "인포레버"
    if DGH_RE.search(head_text):
        return "동기회"

    # 파일명 fallback
    name_lower = filename.lower()
    if "kpc" in name_lower:
        return "KPC"
    if "itpe" in name_lower:
        return "ITPE"
    if "인포레버" in filename or "inforever" in name_lower:
        return "인포레버"
    if "동기회" in filename:
        return "동기회"
    return "unknown"


def detect_exam_type(head_text: str, filename: str) -> ExamType:
    """본문 헤드 + 파일명으로 시험 종별 식별 (모의고사 / 본시험)."""
    if ACTUAL_RE.search(head_text):
        return "actual"
    if MOCK_RE.search(head_text):
        return "mock"

    # 파일명 힌트
    if "모의" in filename:
        return "mock"
    if "기출" in filename:
        return "actual"
    return "unknown"


def detect_publisher_and_type(pdf_path: Path) -> tuple[Publisher, ExamType]:
    """PDF의 (publisher, exam_type) 식별. 표시 라벨용."""
    head = _read_head_text(pdf_path)
    publisher = detect_publisher(head, pdf_path.name)
    exam_type = detect_exam_type(head, pdf_path.name)
    return publisher, exam_type
