"""
포맷 모듈 공통 데이터 클래스, 패턴, 유틸리티

모든 format_*.py 모듈과 detect_boundaries_v2.py가 공유하는 요소를
한 곳에 모아 중복을 제거한다.
"""

import re
from collections import Counter
from dataclasses import dataclass, field


# ─── 데이터 클래스 ────────────────────────────────────────────────

@dataclass
class SessionBlock:
    """교시 블록"""
    session_num: int          # 1, 2, 3, 4
    page_start: int
    page_end: int
    expected_topics: int      # 1교시=13, 2~4교시=6


@dataclass
class BoundaryCandidate:
    """경계 후보"""
    page: int
    score: float
    title: str
    signals: dict = field(default_factory=dict)  # {signal_name: contribution}


@dataclass
class TopicBoundary:
    """최종 토픽 경계"""
    num: int
    title: str
    page_start: int
    page_end: int
    session: int             # 교시 번호
    confidence: float        # 0.0~1.0
    session_q: int = 0       # 교시 내 번호 (문제지는 0)
    fmt: str = "multi_signal"


# ─── 공통 정규식 패턴 ─────────────────────────────────────────────

# "끝" 마커 (curly/straight quotes, 마침표 등 허용)
END_PAT = re.compile(
    r'^[\u201c\u201d"\'\u2018\u2019]*끝[\u201c\u201d"\'\u2018\u2019.]*\s*$'
)

# 구조적 패턴
ROMAN_I_PAT = re.compile(r'^I\.\s+.{3,}')
MENTI_PAT = re.compile(r'^문\s*제\s+(\d{1,2})\.\s+(.+)', re.DOTALL)
STD_NUM_PAT = re.compile(r'^(\d{1,2})\.\s+(.{5,})')
SESSION_PAT = re.compile(r'제?\s*(\d)\s*교시')

# 노이즈 필터링
IGNORE_PAT = re.compile(
    r'^\d+$|Copyright|FB\d{2}|주간모의|^※|^다음 문제|문제를 선택'
    r'|누구나 ICT|cafe\.naver|All rights|기출풀이 의견'
)
COVER_KEYWORDS = re.compile(r'국가기술자격|기술사\s*시험문제')
NOISE_PAGE_PAT = re.compile(
    r'출제\s*빈도|출제\s*비율|도메인\s*별\s*출제|^\[참고\]|교시형\s*출제'
    r'|출제\s*경향|감사의?\s*글|기출\s*풀이집'
)

# 토픽 시작/종료 보조 패턴
TOPIC_END_PAT = re.compile(r'^기출풀이\s*의견$')
STAR_RATING_PAT = re.compile(r'★[★☆]{1,4}')
MARKER_HEADING_PAT = re.compile(r'^[□■◇◆●○▶►▷]\s*.{3,}')
SESSION_TOPIC_PAT = re.compile(r'(\d)\s*교시\s*[:\s]*(\d+)\s*번')

# 문제 텍스트 키워드
Q_KEYWORDS = re.compile(
    r"설명하시오|논하시오|서술하시오|비교하시오|구분하시오|기술하시오"
    r"|설명하고|논하고|비교하고|이다\.|하시오\.|있다\."
)

# 아이리포/합숙 등 특수 패턴
HEADER_KW_PAT = re.compile(
    r'IT\s*trends\s+(.+?)(?:\s*\|\s*PM|\s+PM\s)', re.DOTALL)
DAY_PAT = re.compile(r'(\d+)\s*일차')


# ─── 한글 균등배분 공백 복원 ──────────────────────────────────────

_KR_CHAR_RE = re.compile(r'[\uAC00-\uD7AF\u3131-\u318E]')
_STRUCT_PREFIX_RE = re.compile(r'^[\dIVXa-zA-Z]+\.|^[가-아]\.')


def collapse_even_spacing(text: str) -> str:
    """한글 균등배분 공백 제거: '기 출 풀 이 의 견' → '기출풀이의견'

    PDF OCR/추출 시 균등배분 레이아웃의 글자 간 공백을 제거.
    토큰의 70% 이상이 한글 1글자이면 균등배분으로 판단.
    구조적 접두사("1.", "I.", "가." 등)로 시작하면 결합하지 않음.
    """
    tokens = text.split(" ")
    if len(tokens) >= 3:
        if tokens[0] and _STRUCT_PREFIX_RE.match(tokens[0]):
            return text
        kr_single = sum(1 for t in tokens
                        if len(t) == 1 and _KR_CHAR_RE.match(t))
        if kr_single / len(tokens) >= 0.7:
            return "".join(tokens)
    return text


def norm(raw: str) -> str:
    """element content 정규화: strip + 균등배분 공백 제거"""
    return collapse_even_spacing(raw.strip())


# ─── 공유 유틸리티 함수 ───────────────────────────────────────────

def find_session(page: int, sessions: list[SessionBlock]) -> int:
    """페이지가 속한 세션 번호 반환."""
    for s in sessions:
        if s.page_start <= page <= s.page_end:
            return s.session_num
    if sessions:
        return sessions[-1].session_num
    return 1


def collect_marked_pages(elements: list, pattern: re.Pattern,
                         *, use_match: bool = True,
                         collapse_ws: bool = False) -> list[int]:
    """패턴에 매칭되는 element가 있는 페이지를 수집 (정렬, 중복 제거).

    Args:
        elements: kordoc element 목록
        pattern: 매칭할 정규식
        use_match: True면 pattern.match(), False면 pattern.search()
        collapse_ws: True면 공백 제거 후 매칭 (OCR PDF용)
    """
    pages = []
    seen: set[int] = set()
    match_fn = pattern.match if use_match else pattern.search
    for e in elements:
        c = e.get("content", "").strip()
        if collapse_ws:
            c = re.sub(r'\s+', '', c)
        if match_fn(c):
            pg = e["page"]
            if pg not in seen:
                pages.append(pg)
                seen.add(pg)
    return sorted(pages)


def renumber_boundaries(boundaries: list):
    """토픽 번호 + 교시 내 번호 재부여. 문제지(question_pages)는 Q번호에서 제외."""
    topic_num = 0
    session_counters: dict[int, int] = {}
    for b in boundaries:
        if b.fmt == "question_pages":
            b.num = 0
            b.session_q = 0
        else:
            topic_num += 1
            b.num = topic_num
            sess = b.session
            session_counters[sess] = session_counters.get(sess, 0) + 1
            b.session_q = session_counters[sess]


def detect_repeated_headers(elements: list, total_pages: int) -> set:
    """문서 전체에서 반복 등장하는 헤더/푸터를 탐지."""
    heading_counts = Counter(
        norm(e["content"]) for e in elements if e["type"] == "heading"
    )
    short_para_counts = Counter(
        norm(e["content"]) for e in elements
        if e.get("type") == "paragraph"
        and len(norm(e["content"])) <= 15
    )
    threshold = max(3, total_pages * 0.15)
    result = {c for c, n in heading_counts.items() if n >= threshold}
    result |= {c for c, n in short_para_counts.items() if n >= threshold}
    return result
