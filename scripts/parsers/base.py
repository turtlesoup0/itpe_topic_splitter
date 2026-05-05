"""공통 헬퍼 + 표준 ParseResult 인터페이스.

모든 파서(현재 결정론적 파서, 향후 PureTopicSegmenter)가 사용.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# ParseResult — 모든 파서의 표준 출력
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Topic:
    """단일 토픽 청크."""
    session: int  # 1~4 (0 = 미상)
    num: int  # 토픽 번호
    title: str
    page_start: int  # 1-indexed
    page_end: int
    pages: int

    @classmethod
    def from_range(cls, session: int, num: int, title: str, ps: int, pe: int) -> "Topic":
        """0-indexed 페이지 범위를 받아 1-indexed Topic 생성."""
        return cls(
            session=session, num=num, title=title,
            page_start=ps + 1, page_end=pe + 1, pages=pe - ps + 1,
        )


@dataclass
class ParseResult:
    """파서 표준 결과. ok=False 시 v2 폴백."""
    ok: bool
    engine: str  # "itpe_mock" | "kpc_mock" | "pts" | ...
    round_id: str = ""
    topics: list[Topic] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)  # [{"path": str, "filename": str}, ...]
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    reason: str = ""  # ok=False 시 사유

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "round_id": self.round_id,
            "topics": [
                {
                    "session": t.session, "num": t.num, "title": t.title,
                    "page_start": t.page_start, "page_end": t.page_end, "pages": t.pages,
                }
                for t in self.topics
            ],
            "files": self.files,
            "warnings": self.warnings,
            "summary": self.summary,
            "reason": self.reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 파일명 정규화 — macOS APFS NFD 호환
# ─────────────────────────────────────────────────────────────────────────────
_ALLOWED_FILENAME_RE = re.compile(
    r"[^ -~가-힯ㄱ-ㆎ‐-⁯ -ɏ]+"
)


def sanitize_filename(s: str, max_len: int = 80, max_bytes: int = 180) -> str:
    """파일명 안전 정규화 (화이트리스트 + char/byte 한도).

    macOS APFS는 NFD 변환 후 NAME_MAX(255 바이트) 한도가 있고 일부 비-BMP/
    깨진 유니코드 시퀀스를 거부하므로 두 한도 모두 적용.
    """
    s = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", " ", s)
    s = _ALLOWED_FILENAME_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    nfd_bytes = unicodedata.normalize("NFD", s).encode("utf-8")
    if len(nfd_bytes) > max_bytes:
        while len(s) > 1 and len(unicodedata.normalize("NFD", s).encode("utf-8")) > max_bytes:
            s = s[:-1]
        s = s.rstrip()
    return s if s else "untitled"


# ─────────────────────────────────────────────────────────────────────────────
# 라운드 ID 추출
# ─────────────────────────────────────────────────────────────────────────────
def derive_round_id(pdf_path: Path) -> str:
    """파일명에서 회차 식별자 추출.

    예시:
      '모의_ITPE41-2603-합.pdf' → 'ITPE41-2603'
      '모의_KPC129_2604_합.pdf' → 'KPC129-2604'
      'ITPE 138관-합.pdf' → 'ITPE-138관'
      '인포레버 138관.pdf' → '인포레버-138관'
    """
    stem = pdf_path.stem
    stem = re.sub(r"^모의[_\s-]+", "", stem)
    stem = re.sub(r"[_\s-]?합$", "", stem)
    stem = re.sub(r"\s+", "-", stem)
    stem = re.sub(r"_", "-", stem)
    return stem


# ─────────────────────────────────────────────────────────────────────────────
# 본문 앵커 기반 헤더 trim (LR-007 — 슬로건 의존 금지)
# ─────────────────────────────────────────────────────────────────────────────
def strip_header_by_anchor(
    lines: list[str],
    anchor_predicate,
    max_header_lines: int = 16,
) -> list[str]:
    """페이지 라인 리스트에서 헤더를 trim 한 본문 라인 리턴.

    Args:
        lines: 페이지 원본 라인 (이미 strip 된 상태 또는 빈 줄 포함)
        anchor_predicate: 라인 → bool. True면 그 라인부터 본문 시작
        max_header_lines: 헤더로 간주할 최대 라인 수 (그보다 깊으면 본문)
    """
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    if not cleaned:
        return []
    body_start = 0
    for i in range(min(len(cleaned), max_header_lines)):
        if anchor_predicate(cleaned[i]):
            body_start = i
            break
    return cleaned[body_start:]


# ─────────────────────────────────────────────────────────────────────────────
# 시험 메타데이터 — 분할 알고리즘에선 사용 X, 표시·검증용
# ─────────────────────────────────────────────────────────────────────────────
EXAM_META: dict[tuple[str, str], dict[int, int]] = {
    # (publisher, exam_type) → {session: expected_topic_count}
    ("ITPE", "actual"): {1: 13, 2: 6, 3: 6, 4: 6},
    ("KPC", "actual"): {1: 13, 2: 6, 3: 6, 4: 6},
    ("인포레버", "actual"): {1: 13, 2: 6, 3: 6, 4: 6},
    ("동기회", "actual"): {1: 13, 2: 6, 3: 6, 4: 6},
    ("ITPE", "mock"): {1: 14, 2: 7, 3: 7, 4: 7},
    ("KPC", "mock"): {1: 16, 2: 8, 3: 8, 4: 8},
}


def get_expected_counts(publisher: str, exam_type: str) -> Optional[dict[int, int]]:
    """식별된 (publisher, exam_type)의 교시별 기대 토픽 수 (검증/표시용)."""
    return EXAM_META.get((publisher, exam_type))
