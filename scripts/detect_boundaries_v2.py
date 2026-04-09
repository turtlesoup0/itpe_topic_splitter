"""
다중 신호 점수 기반 토픽 경계 탐지 (v2)

기존 detect_boundaries()의 한계:
  - 단일 마커("끝", "I.", "문 제 N.") 의존 → 특정 학원에서 실패
  - 포맷 감지 후 단일 규칙 적용 → 신호가 없으면 전체 실패

v2 접근:
  1. 교시 분리: 교시 표지/텍스트 기반으로 문서를 4개 교시 블록으로 나눔
  2. 다중 신호 점수: 끝/I./문제N./소제목리셋/밀도변화 등 복수 신호의 가중합
  3. 자기 교정: 문서 내 신호 분포를 보고 가중치를 자동 조정
  4. 시험 구조 검증: 13+6+6+6=31 기대와 대조하여 이상 탐지
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

# ─── 공통 패턴 ────────────────────────────────────────────────────
_끝_PAT = re.compile(r'^[\u201c\u201d"\']?끝[\u201c\u201d"\']?\s*$')
_ROMAN_I_PAT = re.compile(r'^I\.\s+.{3,}')
_MENTI_PAT = re.compile(r'^문\s*제\s+(\d{1,2})\.\s+(.+)', re.DOTALL)
_STD_NUM_PAT = re.compile(r'^(\d{1,2})\.\s+(.{5,})')
_IGNORE_PAT = re.compile(
    r'^\d+$|Copyright|FB\d{2}|주간모의|^※|^다음 문제|문제를 선택'
    r'|누구나 ICT|cafe\.naver|All rights|기출풀이 의견'
)
_COVER_KEYWORDS = re.compile(r'국가기술자격|기술사\s*시험문제')
# 토픽 종료 마커 (학원별로 다름: "끝", "기출풀이 의견" 등)
_TOPIC_END_PAT = re.compile(r'^기출풀이\s*의견$')
_SESSION_PAT = re.compile(r'제?\s*(\d)\s*교시')
# 비토픽 페이지 패턴: 통계/도입/참고자료 — 토픽이 아닌 보조 페이지
# 주의: "기출문제", "기출해설집"은 KPC 등 일반 헤더에도 있으므로 제외
_NOISE_PAGE_PAT = re.compile(
    r'출제\s*빈도|출제\s*비율|도메인\s*별\s*출제|^\[참고\]|교시형\s*출제'
)
_Q_KEYWORDS = re.compile(
    r"설명하시오|논하시오|서술하시오|비교하시오|구분하시오|기술하시오"
    r"|설명하고|논하고|비교하고|이다\.|하시오\.|있다\."
)
# 마커 헤딩: □/■/◇/◆ + 텍스트 (일부 학원에서 토픽 시작 표시)
_MARKER_HEADING_PAT = re.compile(r'^[□■◇◆●○▶►▷]\s*.{3,}')
# KPC: ★ 난이도 마커가 포함된 토픽 시작 paragraph
_STAR_RATING_PAT = re.compile(r'★[★☆]{1,4}')
# 라이지움: "N교시 M번" heading = 토픽 시작
_SESSION_TOPIC_PAT = re.compile(r'(\d)\s*교시\s*(\d+)\s*번')
# 아이리포: 반복 헤더의 "IT trends" 뒤 키워드 추출
_HEADER_KW_PAT = re.compile(r'IT\s*trends\s+(.+?)(?:\s*\|\s*PM|\s+PM\s)', re.DOTALL)
# 합숙: "N일차" 기반 블록 분리
_DAY_PAT = re.compile(r'(\d+)\s*일차')

# ─── 한글 균등배분 공백 복원 (kordoc 포팅) ────────────────────────
_KR_CHAR_RE = re.compile(r'[\uAC00-\uD7AF\u3131-\u318E]')


_STRUCT_PREFIX_RE = re.compile(r'^[\dIVXa-zA-Z]+\.|^[가-아]\.')


def _collapse_even_spacing(text: str) -> str:
    """한글 균등배분 공백 제거: '기 출 풀 이 의 견' → '기출풀이의견'

    PDF OCR/추출 시 균등배분 레이아웃의 글자 간 공백을 제거.
    토큰의 70% 이상이 한글 1글자이면 균등배분으로 판단.
    구조적 접두사("1.", "I.", "가." 등)로 시작하면 결합하지 않음.
    (kordoc collapseEvenSpacing 알고리즘 포팅)
    """
    tokens = text.split(" ")
    if len(tokens) >= 3:
        # 구조적 접두사 보호: "1. 프 로 젝 트" 등 결합 방지
        if tokens[0] and _STRUCT_PREFIX_RE.match(tokens[0]):
            return text
        kr_single = sum(1 for t in tokens
                        if len(t) == 1 and _KR_CHAR_RE.match(t))
        if kr_single / len(tokens) >= 0.7:
            return "".join(tokens)
    return text


def _norm(raw: str) -> str:
    """element content 정규화: strip + 균등배분 공백 제거"""
    return _collapse_even_spacing(raw.strip())


# ─── 데이터 클래스 ────────────────────────────────────────────────

@dataclass
class SessionBlock:
    """교시 블록"""
    session_num: int          # 1, 2, 3, 4
    page_start: int
    page_end: int
    expected_topics: int      # 1교시=13, 2~4교시=6


@dataclass
class SignalWeights:
    """신호별 가중치 (자기 교정으로 조정됨)"""
    끝_marker: float = 0.0
    topic_end: float = 0.0      # "기출풀이 의견" 등 토픽 종료 마커
    roman_i: float = 0.0
    menti: float = 0.0
    std_num_restart: float = 0.0
    cover_page: float = 0.0
    density_drop: float = 0.0
    font_heading: float = 0.0    # 폰트 크기 기반 헤딩 탐지 (kordoc 포팅)
    marker_heading: float = 0.0  # □/■ 마커 헤딩 기반 토픽 시작
    star_rating: float = 0.0     # KPC: ★ 난이도 마커 기반 토픽 시작
    session_topic_num: float = 0.0  # 라이지움: "N교시 M번" 기반 토픽 시작
    header_kw_change: float = 0.0   # 아이리포: 반복 헤더 키워드 변화

    def dominant_signal(self) -> str:
        signals = {
            '끝': self.끝_marker, 'topic_end': self.topic_end,
            'I.': self.roman_i, '문제N': self.menti,
            'N.restart': self.std_num_restart,
            'font_heading': self.font_heading,
            'marker_heading': self.marker_heading,
            '★rating': self.star_rating,
            'N교시M번': self.session_topic_num,
            'header_kw': self.header_kw_change,
        }
        return max(signals, key=signals.get)


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
    fmt: str = "multi_signal"


# ─── 반복 헤더 탐지 ───────────────────────────────────────────────

def _detect_repeated_headers(elements: list, total_pages: int) -> set:
    """문서 전체에서 반복 등장하는 헤더/푸터를 탐지.
    kordoc --no-header-footer로 위치 기반 헤더는 제거되지만,
    본문 내 반복 헤딩(예: 학원명, 회차 표시)은 여전히 남으므로 이 함수 유지."""
    heading_counts = Counter(
        _norm(e["content"]) for e in elements if e["type"] == "heading"
    )
    threshold = max(3, total_pages * 0.15)
    return {c for c, n in heading_counts.items() if n >= threshold}


# ─── Phase 1: 교시 분리 ──────────────────────────────────────────

def detect_sessions(elements: list, total_pages: int) -> list[SessionBlock]:
    """
    문서를 교시(1~4교시) 블록으로 분리.

    전략 (우선순위):
    1. "국가기술자격 기술사 시험문제" 표지 페이지 → 교시 경계
    2. "N교시" 텍스트 멘션으로 교시 번호 확인
    3. 표지가 없으면 (인포레버): 토픽 수/페이지 분포 기반 추론 → 단일 블록 반환
    """
    # 1. 교시 표지 탐지
    cover_pages = _detect_cover_pages(elements, total_pages)

    if len(cover_pages) >= 2:
        # 표지가 충분하면 표지 기반 분리
        return _sessions_from_covers(cover_pages, total_pages, elements)

    # 2. 표지 부족 → heading/paragraph 내 "N교시" 전환점 기반 분리
    heading_sessions = _detect_sessions_from_headings(elements, total_pages)
    if heading_sessions:
        return heading_sessions

    # 3. 모든 방법 실패 → 단일 블록
    return [SessionBlock(
        session_num=0,  # 교시 미확정
        page_start=1,
        page_end=total_pages,
        expected_topics=31,  # 전체 31개 기대
    )]


def _detect_cover_pages(elements: list, total_pages: int) -> list[dict]:
    """
    교시 표지 페이지를 탐지.
    heading뿐 아니라 caption 타입도 검색 (ITPE 4교시 표지가 caption).
    Returns: [{'page': int, 'session_num': int or None}, ...]
    """
    candidates = []
    seen_pages = set()

    # 모든 타입에서 검색 (heading, caption, paragraph)
    for e in elements:
        c = _norm(e["content"])
        if _COVER_KEYWORDS.search(c) and e["page"] not in seen_pages:
            seen_pages.add(e["page"])
            session_num = _extract_session_num(elements, e["page"])
            candidates.append({
                "page": e["page"],
                "session_num": session_num,
            })

    # "N교시" + "시험시간/시험문제" 조합 (caption 타입에서 교시 표지를 놓치는 케이스)
    # 예: ITPE p77 caption "기술사 제 138 회 제 4 교시 (시험시간: 100 분)"
    _COVER_CAPTION_PAT = re.compile(r'기술사.*제\s*\d+\s*회.*제\s*(\d)\s*교시')
    for e in elements:
        if e["page"] in seen_pages:
            continue
        m = _COVER_CAPTION_PAT.search(e["content"])
        if m:
            seen_pages.add(e["page"])
            candidates.append({
                "page": e["page"],
                "session_num": int(m.group(1)),
            })

    if not candidates:
        return []

    candidates.sort(key=lambda c: c["page"])

    # 너무 가까운 표지 병합 (10페이지 이내는 같은 교시의 중복 표지)
    merged = [candidates[0]]
    for c in candidates[1:]:
        if c["page"] - merged[-1]["page"] > 10:
            merged.append(c)
        else:
            if c["session_num"] and not merged[-1]["session_num"]:
                merged[-1]["session_num"] = c["session_num"]

    return merged


def _extract_session_num(elements: list, cover_page: int) -> Optional[int]:
    """표지 페이지 ±1 범위에서 교시 번호를 추출"""
    for e in elements:
        if abs(e["page"] - cover_page) <= 1:
            m = _SESSION_PAT.search(e["content"])
            if m:
                return int(m.group(1))
    return None


def _sessions_from_covers(covers: list[dict], total_pages: int,
                           elements: list) -> list[SessionBlock]:
    """표지 목록으로부터 교시 블록 생성"""
    blocks = []

    for i, cover in enumerate(covers):
        page_start = cover["page"]
        page_end = covers[i + 1]["page"] - 1 if i + 1 < len(covers) else total_pages

        # 교시 번호 결정
        session_num = cover["session_num"]
        if not session_num:
            session_num = i + 1

        blocks.append(SessionBlock(
            session_num=session_num,
            page_start=page_start,
            page_end=page_end,
            expected_topics=0,  # 아래에서 설정
        ))

    # 교시 번호 순차 보정: 감지된 번호가 비순차적이면 순서 기반으로 재할당
    nums = [b.session_num for b in blocks]
    if nums != sorted(nums) or len(set(nums)) != len(nums):
        # 비순차적이거나 중복 → 순서 기반 재할당
        for i, b in enumerate(blocks):
            b.session_num = i + 1

    # 기대 토픽 수 설정
    for b in blocks:
        b.expected_topics = 13 if b.session_num == 1 else 6

    return blocks


def _detect_sessions_from_headings(elements: list,
                                    total_pages: int) -> list[SessionBlock]:
    """
    heading/paragraph에서 "N교시" 전환점을 찾아 교시 블록 생성.

    표지가 없는 라이지움/아이리포 등에서 교시 구분에 사용.
    "N교시 M번" 같은 토픽 번호는 무시하고, 교시 번호 전환만 감지.

    전략:
    1. 모든 heading/paragraph에서 "N교시" 패턴 추출
    2. 교시 번호가 바뀌는 첫 페이지 = 교시 시작
    3. 최소 2개 교시가 감지되어야 유효
    """
    page_session: dict[int, int] = {}  # page → session_num (첫 등장)

    for e in elements:
        if e.get("type") not in ("heading", "paragraph"):
            continue
        m = _SESSION_PAT.search(e.get("content", ""))
        if m:
            pg = e.get("page", 0)
            sn = int(m.group(1))
            if sn < 1 or sn > 4:
                continue
            if pg not in page_session:
                page_session[pg] = sn

    if not page_session:
        return []

    # 교시 전환점 찾기: 교시 번호가 바뀌는 첫 페이지
    transitions: list[tuple[int, int]] = []  # (page, session_num)
    prev_sn = 0
    for pg in sorted(page_session.keys()):
        sn = page_session[pg]
        if sn != prev_sn:
            transitions.append((pg, sn))
            prev_sn = sn

    if len(transitions) < 2:
        return _detect_sessions_from_days(elements, total_pages)

    # 교시 번호가 반복되면 (예: 1,2,3,4,1,2,3,4) 마지막 사이클을 사용
    # 문제 페이지에서 1,2,3,4교시가 먼저 나오고, 해설에서 다시 나오는 패턴
    session_nums = [sn for _, sn in transitions]
    last_start_idx = 0
    for i, sn in enumerate(session_nums):
        if sn == 1 and i > 0:
            # 교시 번호가 1로 리셋 → 새 사이클 시작
            last_start_idx = i
    transitions = transitions[last_start_idx:]

    if len(transitions) < 2:
        # 교시 전환 실패 → "N일차" 기반 블록 분리 시도 (합숙 문서)
        return _detect_sessions_from_days(elements, total_pages)

    # SessionBlock 생성
    blocks = []
    for i, (pg, sn) in enumerate(transitions):
        page_end = transitions[i + 1][0] - 1 if i + 1 < len(transitions) else total_pages
        blocks.append(SessionBlock(
            session_num=sn,
            page_start=pg,
            page_end=page_end,
            expected_topics=13 if sn == 1 else 6,
        ))

    return blocks


def _detect_sessions_from_days(elements: list,
                                total_pages: int) -> list[SessionBlock]:
    """
    반복 헤더의 "N일차" 전환으로 블록 분리 (합숙 문서용).

    합숙 PDF는 5일간 자료가 하나로 합쳐져 있고, 모든 페이지에
    "해설집 (N일차)" 반복 헤더가 존재. 일차 번호 변화 = 블록 경계.

    expected_topics는 블록 내 "끝" 마커 수로 실측 설정.
    """
    # 페이지별 일차 번호 추출 (반복 헤더에서)
    page_day: dict[int, int] = {}
    for e in elements:
        if e.get("type") not in ("heading", "paragraph"):
            continue
        m = _DAY_PAT.search(e.get("content", ""))
        if m:
            pg = e.get("page", 0)
            day = int(m.group(1))
            if day < 1 or day > 10:
                continue
            if pg not in page_day:
                page_day[pg] = day

    if len(page_day) < 5:
        return []

    # 일차 전환점 찾기
    transitions: list[tuple[int, int]] = []  # (page, day_num)
    prev_day = 0
    for pg in sorted(page_day.keys()):
        day = page_day[pg]
        if day != prev_day:
            transitions.append((pg, day))
            prev_day = day

    if len(transitions) < 2:
        return []

    # 블록 내 "끝" 마커 수 계산 → expected_topics
    end_pages_all = set()
    for e in elements:
        if _끝_PAT.match(_norm(e.get("content", ""))):
            end_pages_all.add(e.get("page", 0))

    blocks = []
    for i, (pg, day) in enumerate(transitions):
        page_end = transitions[i + 1][0] - 1 if i + 1 < len(transitions) else total_pages
        # 블록 내 "끝" 마커 수 = 토픽 수 추정
        end_count = sum(1 for ep in end_pages_all if pg <= ep <= page_end)
        # 최소 1, 끝 마커 없으면 블록 크기 기반 추정
        expected = end_count if end_count >= 2 else max((page_end - pg + 1) // 3, 3)
        blocks.append(SessionBlock(
            session_num=day,
            page_start=pg,
            page_end=page_end,
            expected_topics=expected,
        ))

    return blocks


# ─── Phase 2: 자기 교정 (가중치 조정) ────────────────────────────

def calibrate_weights(elements: list, total_pages: int) -> SignalWeights:
    """
    문서 전체를 스캔하여 각 신호의 출현 빈도를 파악하고
    가중치를 자동 조정.

    원칙: 많이 나타나는 신호 = 이 문서에서 신뢰할 수 있는 신호
    """
    w = SignalWeights()

    # "끝" 마커 수 (OCR 제외)
    끝_count = sum(
        1 for e in elements
        if e.get("source") != "ocr" and _끝_PAT.match(_norm(e["content"]))
    )

    # "I." 패턴 수
    roman_i_count = sum(
        1 for e in elements
        if _ROMAN_I_PAT.match(_norm(e["content"]))
    )

    # "문 제 N." 패턴 수
    menti_count = sum(
        1 for e in elements
        if _MENTI_PAT.match(_norm(e["content"]))
    )

    # 소제목 번호 (N.) — heading 타입에서만
    std_nums = []
    for e in elements:
        if e["type"] != "heading":
            continue
        m = _STD_NUM_PAT.match(_norm(e["content"]))
        if m:
            std_nums.append((e["page"], int(m.group(1))))

    # "1."의 등장 횟수 = 토픽 시작 가능 횟수 (번호 리셋 신호)
    num_one_count = sum(1 for _, n in std_nums if n == 1)

    # "기출풀이 의견" 등 토픽 종료 마커 수
    topic_end_count = sum(
        1 for e in elements
        if _TOPIC_END_PAT.match(_norm(e["content"]))
    )

    # 가중치 산정: 신호 강도에 비례
    # 최소 임계값 이상일 때만 활성화
    if 끝_count >= 3:
        w.끝_marker = min(끝_count / 10, 1.0)
    if topic_end_count >= 3:
        w.topic_end = min(topic_end_count / 8, 1.0)
    if roman_i_count >= 3:
        w.roman_i = min(roman_i_count / 10, 1.0)
    if menti_count >= 3:
        w.menti = min(menti_count / 8, 1.0)
    if num_one_count >= 3:
        w.std_num_restart = min(num_one_count / 10, 1.0)

    # 폰트 크기 기반 헤딩 수 (font_ratio >= 1.3 = H2 이상, kordoc 기준)
    # font_ratio가 있는 element에서만 카운트
    font_heading_count = sum(
        1 for e in elements
        if e.get("font_ratio", 1.0) >= 1.3
        and len(_norm(e["content"])) > 5
        and not _IGNORE_PAT.search(_norm(e["content"]))
    )
    if font_heading_count >= 3:
        w.font_heading = min(font_heading_count / 10, 1.0)

    # □/■ 마커 헤딩 수
    marker_heading_count = sum(
        1 for e in elements
        if _MARKER_HEADING_PAT.match(_norm(e["content"]))
        and not _IGNORE_PAT.search(_norm(e["content"]))
    )
    if marker_heading_count >= 3:
        w.marker_heading = min(marker_heading_count / 10, 1.0)

    # ★ 난이도 마커 수 (KPC: paragraph에 ★★ + "1." 패턴)
    star_count = sum(
        1 for e in elements
        if e.get("type") == "paragraph"
        and _STAR_RATING_PAT.search(e.get("content", ""))
        and len(e.get("content", "")) > 10
    )
    if star_count >= 3:
        w.star_rating = min(star_count / 10, 1.0)

    # "N교시 M번" heading 수 (라이지움)
    session_topic_count = sum(
        1 for e in elements
        if e.get("type") == "heading"
        and _SESSION_TOPIC_PAT.search(_norm(e.get("content", "")))
    )
    if session_topic_count >= 3:
        w.session_topic_num = min(session_topic_count / 10, 1.0)

    # 반복 헤더 키워드 변화 수 (아이리포)
    header_kw_changes = _count_header_kw_changes(elements)
    if header_kw_changes >= 3:
        w.header_kw_change = min(header_kw_changes / 10, 1.0)

    # 정규화: 가장 강한 신호를 1.0으로
    all_weights = [
        w.끝_marker, w.topic_end, w.roman_i, w.menti,
        w.std_num_restart, w.font_heading, w.marker_heading,
        w.star_rating, w.session_topic_num, w.header_kw_change,
    ]
    max_w = max(*all_weights, 0.01)
    w.끝_marker /= max_w
    w.topic_end /= max_w
    w.roman_i /= max_w
    w.menti /= max_w
    w.std_num_restart /= max_w
    w.font_heading /= max_w
    w.marker_heading /= max_w
    w.star_rating /= max_w
    w.session_topic_num /= max_w
    w.header_kw_change /= max_w

    return w


def _count_header_kw_changes(elements: list) -> int:
    """반복 헤더의 키워드 변화 횟수를 카운트 (아이리포 등).

    페이지별 첫 paragraph에서 'IT trends' 뒤 키워드를 추출하고
    연속 페이지 간 키워드가 바뀌는 횟수를 반환.
    """
    page_kw: dict[int, str] = {}
    seen = set()
    for e in sorted(elements, key=lambda x: x.get("page", 0)):
        pg = e.get("page", 0)
        if pg in seen or e.get("type") != "paragraph":
            continue
        m = _HEADER_KW_PAT.search(e.get("content", ""))
        if m:
            seen.add(pg)
            page_kw[pg] = m.group(1).strip()[:40]

    if len(page_kw) < 5:
        return 0

    changes = 0
    prev_kw = None
    for pg in sorted(page_kw.keys()):
        kw = page_kw[pg]
        if prev_kw is not None and kw != prev_kw:
            changes += 1
        prev_kw = kw
    return changes


# ─── Phase 3: 다중 신호 경계 탐지 ────────────────────────────────

def _is_cover_page(elements: list, page: int) -> bool:
    """해당 페이지가 교시 표지인지 확인"""
    for e in elements:
        if e["page"] == page and _COVER_KEYWORDS.search(e["content"]):
            return True
    return False


def _detect_noise_pages(block_elems: list, repeated_headers: set) -> set:
    """
    비토픽 페이지를 감지: 통계, 도입부, 목차, 참고자료, 시험문제 목록 등.

    감지 대상:
    1. 통계 페이지 (_NOISE_PAGE_PAT: "출제 빈도", "도메인 별 출제" 등)
    2. 프론트매터 (블록 초반 noise 클러스터 → 첫 토픽 신호까지 확장)
    3. 시험 문제 목록 페이지 (여러 번호+질문이 나열되고 I. 토픽이 없는 페이지)
       → 인포레버에서 교시 전환점 (p22: "1. 뉴로모픽...", "3. ISO...")
    """
    if not block_elems:
        return set()

    # Type 1: 통계/출제빈도 패턴 (프론트매터 확장 트리거 가능)
    stat_noise = set()
    for e in block_elems:
        c = _norm(e["content"])
        if c in repeated_headers:
            continue
        if _NOISE_PAGE_PAT.search(c):
            stat_noise.add(e["page"])

    # Type 2: 시험 문제 목록 페이지 (프론트매터 확장 트리거 안 함)
    # 동일 페이지에 2개 이상의 번호+질문키워드가 있고 "I." 토픽이 없으면
    qlist_noise = set()
    page_q_count: dict[int, int] = {}
    page_has_roman: dict[int, bool] = {}
    for e in block_elems:
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        pg = e["page"]
        m = _STD_NUM_PAT.match(c)
        if m and _Q_KEYWORDS.search(c):
            page_q_count[pg] = page_q_count.get(pg, 0) + 1
        if _ROMAN_I_PAT.match(c):
            page_has_roman[pg] = True

    for pg, qc in page_q_count.items():
        if qc >= 2 and not page_has_roman.get(pg, False):
            qlist_noise.add(pg)

    # Type 3: 테이블 밀집 페이지 (kordoc table_marker 활용)
    # kordoc가 구조적으로 감지한 table_marker 요소를 우선 활용하고,
    # table_marker가 없는 경우 기존 텍스트 길이 휴리스틱으로 폴백
    table_noise = set()
    page_table_count: dict[int, int] = {}   # table_marker 수
    page_non_table: dict[int, int] = {}     # table_marker가 아닌 서술 요소 수
    page_short_count: dict[int, int] = {}   # 짧은(≤10자) element 수
    page_long_count: dict[int, int] = {}    # 긴(>30자) 서술 element 수
    _NUM_HEAVY_PAT = re.compile(r'^[\d%.,\s\-~()]+$')
    for e in block_elems:
        c = _norm(e["content"])
        if c in repeated_headers:
            continue
        pg = e["page"]
        if e.get("type") == "table_marker":
            page_table_count[pg] = page_table_count.get(pg, 0) + 1
        elif len(c) > 30 and not _Q_KEYWORDS.search(c):
            page_non_table[pg] = page_non_table.get(pg, 0) + 1
            page_long_count[pg] = page_long_count.get(pg, 0) + 1
        if len(c) <= 10 or _NUM_HEAVY_PAT.match(c):
            page_short_count[pg] = page_short_count.get(pg, 0) + 1

    for pg in set(page_table_count) | set(page_short_count):
        tbl_cnt = page_table_count.get(pg, 0)
        non_tbl = page_non_table.get(pg, 0)
        short_cnt = page_short_count.get(pg, 0)
        long_cnt = page_long_count.get(pg, 0)
        # kordoc table_marker ≥2개 + 서술 요소 없음 = 테이블 전용 페이지
        if tbl_cnt >= 2 and non_tbl == 0:
            table_noise.add(pg)
        # 폴백: 짧은 element ≥8개 + 긴 element ≤1개
        elif short_cnt >= 8 and long_cnt <= 1:
            table_noise.add(pg)

    noise = stat_noise | qlist_noise | table_noise
    if not noise:
        return noise

    # ── 프론트매터 확장 (통계 noise만 트리거) ─────────────────────
    # 시험 문제 목록은 교시 시작부에 있을 수 있으므로 확장하지 않음
    block_start_page = min(e["page"] for e in block_elems)
    early_noise = {p for p in stat_noise if p - block_start_page < 10}

    if early_noise:
        first_signal_page = None
        for e in sorted(block_elems, key=lambda x: x["page"]):
            c = _norm(e["content"])
            if c in repeated_headers or _IGNORE_PAT.search(c) or len(c) < 5:
                continue
            if _ROMAN_I_PAT.match(c) or _MENTI_PAT.match(c):
                first_signal_page = e["page"]
                break

        if first_signal_page and first_signal_page > max(early_noise):
            noise.update(range(block_start_page, first_signal_page))

    return noise


def _find_content_start(elements: list, cover_page: int,
                         block_end: int, repeated: set) -> int:
    """
    교시 표지 이후 실제 콘텐츠가 시작되는 페이지를 찾음.
    표지 직후 1-2페이지는 문제 목록이나 빈 페이지일 수 있으므로
    실제 토픽 관련 element가 시작되는 페이지를 반환.
    """
    for e in sorted(
        [e for e in elements if cover_page < e["page"] <= block_end],
        key=lambda x: x["page"]
    ):
        c = _norm(e["content"])
        if c in repeated or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        if _COVER_KEYWORDS.search(c):
            continue
        # 실제 콘텐츠 발견
        return e["page"]
    return cover_page + 1


def score_boundaries(elements: list, session_block: SessionBlock,
                     weights: SignalWeights,
                     repeated_headers: set) -> list[BoundaryCandidate]:
    """
    교시 블록 내에서 다중 신호의 가중합으로 토픽 경계 후보를 탐지.

    각 페이지에 대해 "여기서 새 토픽이 시작되는가?"의 점수를 계산.
    """
    ps = session_block.page_start
    pe = session_block.page_end

    # 블록 내 elements만 필터
    block_elems = [e for e in elements if ps <= e["page"] <= pe]

    # 교시 표지 페이지 식별 → 해당 페이지에는 토픽 시작 점수를 주지 않음
    cover_pages_in_block = set()
    if _is_cover_page(elements, ps):
        cover_pages_in_block.add(ps)

    # 비토픽(noise) 페이지 식별: 통계, 도입부, 참고자료 등
    noise_pages = _detect_noise_pages(block_elems, repeated_headers)

    # 실제 콘텐츠 시작 페이지 (표지/qlist 다음)
    content_start = _find_content_start(
        elements, ps, pe, repeated_headers
    ) if ps in cover_pages_in_block else ps

    # 페이지별 신호 수집
    page_scores: dict[int, BoundaryCandidate] = {}
    for pg in range(ps, pe + 1):
        page_scores[pg] = BoundaryCandidate(page=pg, score=0.0, title="")

    # ── 신호 1: 토픽 종료 마커 → 구간 기반 분리 ─────────────────────
    # "끝" 마커와 "기출풀이 의견" 등을 통합하여 구간 분리
    # end_marker_pages는 가중치와 무관하게 항상 수집 (끝 게이트용)
    end_marker_pages = set()
    for e in block_elems:
        if _끝_PAT.match(_norm(e["content"])):
            end_marker_pages.add(e["page"])
        if _TOPIC_END_PAT.match(_norm(e["content"])):
            end_marker_pages.add(e["page"])

    end_weight = max(weights.끝_marker, weights.topic_end)
    if end_marker_pages and end_weight > 0:
        sorted_ends = sorted(end_marker_pages)
        # 각 구간의 시작 페이지에 점수: end1+1, end2+1, ...
        # content_start는 별도 block_start 신호로 처리 (낮은 가중치)
        # → content_start를 interval에 넣으면 실제 end marker 없이도
        #   높은 점수를 받아 noise boundary가 되는 문제 방지
        interval_starts = []
        for end_p in sorted_ends:
            next_pg = end_p + 1
            if next_pg <= pe:
                if _is_cover_page(elements, next_pg):
                    actual = _find_content_start(
                        elements, next_pg, pe, repeated_headers)
                    cover_pages_in_block.add(next_pg)
                else:
                    actual = next_pg
                interval_starts.append(actual)

        for start_pg in interval_starts:
            if start_pg in page_scores and start_pg not in cover_pages_in_block:
                page_scores[start_pg].score += end_weight * 10
                page_scores[start_pg].signals["end_marker"] = end_weight * 10

    # ── 신호 2: "I." 로마 숫자 토픽 시작 ─────────────────────────────
    if weights.roman_i > 0:
        for e in block_elems:
            c = _norm(e["content"])
            if c in repeated_headers or _IGNORE_PAT.search(c):
                continue
            if e["page"] in cover_pages_in_block:
                continue
            if _ROMAN_I_PAT.match(c):
                pg = e["page"]
                page_scores[pg].score += weights.roman_i * 10
                page_scores[pg].signals["I."] = weights.roman_i * 10
                title = c[3:].strip()[:70]
                if not page_scores[pg].title or len(title) > len(page_scores[pg].title):
                    page_scores[pg].title = title

    # ── 신호 3: "문 제 N." 토픽 제목 ─────────────────────────────────
    if weights.menti > 0:
        for e in block_elems:
            if e["page"] in cover_pages_in_block:
                continue
            m = _MENTI_PAT.match(_norm(e["content"]))
            if m:
                pg = e["page"]
                page_scores[pg].score += weights.menti * 10
                page_scores[pg].signals["문제N"] = weights.menti * 10
                title = m.group(2).strip().split("\n")[0][:70]
                if not page_scores[pg].title or len(title) > len(page_scores[pg].title):
                    page_scores[pg].title = title

    # ── 신호 4: 소제목 번호 리셋 ("1." 재등장) ───────────────────────
    if weights.std_num_restart > 0:
        _apply_num_restart_signal(block_elems, page_scores, weights,
                                  repeated_headers, ps, pe,
                                  cover_pages_in_block)

    # ── 신호 5: 시험 문제지 텍스트 기반 토픽 힌트 ─────────────────────
    # 교시 표지/문제지에 포함된 질문 텍스트가 본문에서 토픽 시작으로 등장
    _apply_exam_question_signal(block_elems, page_scores, weights,
                                repeated_headers, cover_pages_in_block,
                                content_start, pe)

    # ── 신호 6: 번호+질문키워드 = 시험 문제 텍스트 → 토픽 시작 ──────────
    # KPC 등에서 "N. ...설명하시오." 형태의 시험 문제가 직접 본문에 삽입됨
    _apply_question_text_signal(block_elems, page_scores, weights,
                                repeated_headers, cover_pages_in_block)

    # ── 신호 7: 한글 소제목 "가." 리셋 → 새 토픽 시작 ───────────────
    _apply_kr_restart_signal(block_elems, page_scores,
                             repeated_headers, cover_pages_in_block)

    # ── 신호 8: 폰트 크기 기반 헤딩 → 새 토픽 시작 (kordoc 포팅) ─────
    if weights.font_heading > 0:
        _apply_font_heading_signal(block_elems, page_scores, weights,
                                   repeated_headers, cover_pages_in_block)

    # ── 신호 9: □/■ 마커 헤딩 → 새 토픽 시작 ──────────────────────────
    if weights.marker_heading > 0:
        _apply_marker_heading_signal(block_elems, page_scores, weights,
                                     repeated_headers, cover_pages_in_block)

    # ── 신호 10: ★ 난이도 마커 → 토픽 시작 (KPC) ──────────────────────
    if weights.star_rating > 0:
        _apply_star_rating_signal(block_elems, page_scores, weights,
                                  repeated_headers, cover_pages_in_block)

    # ── 신호 11: "N교시 M번" heading → 토픽 시작 (라이지움) ─────────────
    if weights.session_topic_num > 0:
        _apply_session_topic_signal(block_elems, page_scores, weights,
                                    repeated_headers, cover_pages_in_block)

    # ── 신호 12: 반복 헤더 키워드 변화 → 토픽 전환 (아이리포) ──────────────
    if weights.header_kw_change > 0:
        _apply_header_keyword_signal(block_elems, page_scores, weights,
                                     cover_pages_in_block)

    # ── "끝" 게이트: 끝 마커가 일관된 문서에서 끝 없는 경계에 패널티 ──
    _apply_end_marker_gate(page_scores, end_marker_pages, content_start)

    # ── 블록 첫 콘텐츠 페이지는 항상 토픽 시작 후보 ──────────────────
    if content_start in page_scores and page_scores[content_start].score == 0:
        page_scores[content_start].score += 5.0
        page_scores[content_start].signals["block_start"] = 5.0

    # ── 제목 보완 ─────────────────────────────────────────────────────
    for pg, cand in page_scores.items():
        if cand.score > 0 and not cand.title:
            cand.title = _extract_title(block_elems, pg, repeated_headers)

    # 토픽 종료 마커 페이지 = 토픽 끝이므로 시작 후보에서 제외
    # (예: "기출풀이 의견" 페이지에 "1." 패턴이 있어도 토픽 시작이 아님)
    topic_end_pages = set()
    for e in block_elems:
        c = _norm(e["content"])
        if _TOPIC_END_PAT.match(c):
            topic_end_pages.add(e["page"])

    # 표지 + noise + 토픽종료 페이지 제거 + 점수 양수인 후보만 반환
    skip_pages = cover_pages_in_block | noise_pages | topic_end_pages
    candidates = [
        c for c in page_scores.values()
        if c.score > 0 and c.page not in skip_pages
    ]
    candidates.sort(key=lambda c: c.page)

    return candidates


def _apply_num_restart_signal(block_elems: list, page_scores: dict,
                               weights: SignalWeights,
                               repeated_headers: set,
                               ps: int, pe: int,
                               cover_pages: set = None):
    """
    소제목 번호 "1."이 새로 등장 = 새 토픽 시작 가능성.

    로직:
    1. heading 타입의 번호를 우선 추적 (소제목 = 답안 구조).
       KPC에서 paragraph "2. 질문..." 뒤에 heading "1. 소제목"이 오는 경우,
       heading "1."을 기준으로 리셋 판정.
    2. paragraph + 문제 키워드("설명하시오" 등) + heading "1." 조합은
       독립적인 토픽 시작 신호.
    """
    cover_pages = cover_pages or set()

    # 페이지별 번호 수집 (타입별 분리)
    page_heading_first_num: dict[int, int] = {}
    page_any_first_num: dict[int, int] = {}
    page_has_question_elem: dict[int, bool] = {}

    for e in block_elems:
        if e["type"] not in ("heading", "paragraph"):
            continue
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        pg = e["page"]
        if pg in cover_pages:
            continue

        m = _STD_NUM_PAT.match(c)
        has_q_kw = bool(_Q_KEYWORDS.search(c))

        if m:
            num = int(m.group(1))
            if pg not in page_any_first_num:
                page_any_first_num[pg] = num
            # heading 번호: 시험 문제 텍스트(Q keyword)는 소제목이 아님 → 제외
            # 예: heading "4. Benchmark Test...비교하고...제시하시오." = 시험 문제
            #     heading "1. 프로젝트 리스크 최소화..." = 소제목 ✓
            if (e["type"] == "heading" and not has_q_kw
                    and pg not in page_heading_first_num):
                page_heading_first_num[pg] = num

        # 시험 문제 키워드 포함 element (heading이든 paragraph이든)
        if has_q_kw:
            page_has_question_elem[pg] = True

    # restart 탐지용: heading 번호 우선, 없으면 전체 타입
    # heading 우선이 KPC 같은 문서에서 paragraph 질문번호(N.)와
    # heading 소제목번호(1.)를 올바르게 구분
    page_first_num: dict[int, int] = {}
    for pg in range(ps, pe + 1):
        if pg in page_heading_first_num:
            page_first_num[pg] = page_heading_first_num[pg]
        elif pg in page_any_first_num:
            page_first_num[pg] = page_any_first_num[pg]

    # 번호 리셋 탐지
    prev_num = None
    for pg in range(ps, pe + 1):
        if pg in cover_pages:
            continue
        if pg not in page_first_num:
            continue
        cur_num = page_first_num[pg]
        if cur_num == 1 and prev_num is not None and prev_num > 1:
            # 번호 리셋! → 새 토픽 시작
            score = weights.std_num_restart * 8
            if page_has_question_elem.get(pg, False):
                score += weights.std_num_restart * 3
            page_scores[pg].score += score
            page_scores[pg].signals["num_restart"] = score
        prev_num = cur_num

    # 보충 신호: 시험 문제 element + heading "1." = 토픽 시작
    # num_restart가 못 잡은 페이지 (예: 블록 첫 토픽, 연속 1→1)
    for pg, has_q in page_has_question_elem.items():
        if not has_q or pg in cover_pages:
            continue
        if page_scores.get(pg) and page_scores[pg].signals.get("num_restart"):
            continue  # 이미 감지됨
        if page_heading_first_num.get(pg) == 1:
            score = weights.std_num_restart * 7
            page_scores[pg].score += score
            page_scores[pg].signals["question_para"] = score


def _apply_exam_question_signal(block_elems: list, page_scores: dict,
                                 weights: SignalWeights,
                                 repeated_headers: set,
                                 cover_pages: set,
                                 content_start: int, block_end: int):
    """
    시험 문제지 텍스트에서 추출한 토픽 힌트를 본문과 매칭.

    교시 표지 근처에 "1. ISMS-P..." 같은 시험 문제 목록이 있으면
    해당 키워드가 본문에서 처음 등장하는 페이지를 토픽 시작 후보로 추가.
    """
    if not cover_pages:
        return

    # 교시 표지 + 그 다음 1페이지에서 질문 키워드 추출
    question_keywords = []
    for e in block_elems:
        if e["page"] not in cover_pages:
            continue
        c = _norm(e["content"])
        if _Q_KEYWORDS.search(c) and len(c) > 20:
            # 긴 질문 텍스트에서 핵심 명사 추출 (첫 20자)
            question_keywords.append(c[:30])

    if not question_keywords:
        return

    # 본문에서 해당 키워드가 처음 등장하는 페이지 찾기
    for kw in question_keywords:
        for e in sorted(block_elems, key=lambda x: x["page"]):
            if e["page"] in cover_pages or e["page"] < content_start:
                continue
            if kw[:15] in e["content"]:  # 부분 매칭
                pg = e["page"]
                if pg in page_scores and page_scores[pg].score == 0:
                    score = 3.0  # 약한 보조 신호
                    page_scores[pg].score += score
                    page_scores[pg].signals["exam_q_hint"] = score
                break


def _apply_question_text_signal(block_elems: list, page_scores: dict,
                                 weights: SignalWeights,
                                 repeated_headers: set,
                                 cover_pages: set):
    """
    본문 내 시험 문제 텍스트(번호 + 질문 키워드)를 토픽 시작 신호로 사용.

    KPC 4교시 등에서 heading/paragraph "N. ...설명하시오." 형태의 시험 문제가
    본문에 직접 삽입됨. 이때 N은 교시 내 질문 번호(1~6).
    3개 이상의 번호+질문 패턴이 있으면 각각을 토픽 시작으로 인식.

    기존 num_restart/question_para 신호와 중복 시 가산하지 않음.
    """
    if weights.std_num_restart <= 0:
        return

    # 번호 + 질문 키워드 매칭 element 수집
    question_pages: list[tuple[int, int, str]] = []  # (page, num, title)
    for e in block_elems:
        if e["type"] not in ("heading", "paragraph"):
            continue
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        pg = e["page"]
        if pg in cover_pages:
            continue
        m = _STD_NUM_PAT.match(c)
        if m and _Q_KEYWORDS.search(c):
            question_pages.append((pg, int(m.group(1)), c[:70]))

    # 페이지별 중복 제거 (같은 페이지에 여러 질문이면 첫 것만)
    seen = set()
    unique = []
    for pg, num, title in question_pages:
        if pg not in seen:
            seen.add(pg)
            unique.append((pg, num, title))

    # 3개 이상이어야 패턴으로 인정 (1-2개는 우연)
    if len(unique) < 3:
        return

    for pg, num, title in unique:
        if pg not in page_scores:
            continue
        # 이미 강한 신호로 감지된 페이지는 스킵
        existing = page_scores[pg].signals
        if existing.get("num_restart") or existing.get("question_para"):
            continue
        if existing.get("end_marker"):
            continue
        score = weights.std_num_restart * 6
        page_scores[pg].score += score
        page_scores[pg].signals["question_text"] = score
        # 제목 보완
        if not page_scores[pg].title:
            page_scores[pg].title = title


def _apply_font_heading_signal(block_elems: list, page_scores: dict,
                                weights: SignalWeights,
                                repeated_headers: set,
                                cover_pages: set):
    """
    폰트 크기 기반 헤딩 탐지 신호 (kordoc detectHeadings 포팅).

    font_ratio >= 1.3 (H2 이상)인 element가 페이지의 첫 의미 있는 element이면
    새 토픽 시작 가능성. KPC/인포레버처럼 "I." 마커 없이 폰트 크기만
    커지는 토픽 시작을 탐지.

    kordoc 기준: H1=1.5×, H2=1.3×, H3=1.15× median font size.
    여기서는 H2(1.3×) 이상을 토픽 시작 신호로 사용.
    """
    # 페이지별 첫 큰 폰트 element 수집
    page_first_heading: dict[int, dict] = {}
    for e in sorted(block_elems, key=lambda x: x["page"]):
        pg = e["page"]
        if pg in cover_pages or pg in page_first_heading:
            continue
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        ratio = e.get("font_ratio", 1.0)
        if ratio >= 1.3:
            page_first_heading[pg] = e

    if not page_first_heading:
        return

    for pg, e in page_first_heading.items():
        if pg not in page_scores:
            continue
        # 이미 강한 신호가 있으면 보조 점수만 추가
        existing = page_scores[pg].signals
        if existing.get("I.") or existing.get("문제N") or existing.get("end_marker"):
            # 기존 신호 보강 (가산하지 않고 제목만 보완)
            if not page_scores[pg].title:
                page_scores[pg].title = _norm(e["content"])[:70]
            continue

        ratio = e.get("font_ratio", 1.3)
        # H1(1.5×) → 강한 신호, H2(1.3×) → 보통 신호
        strength = 8.0 if ratio >= 1.5 else 6.0
        score = weights.font_heading * strength
        page_scores[pg].score += score
        page_scores[pg].signals["font_heading"] = score
        if not page_scores[pg].title:
            page_scores[pg].title = _norm(e["content"])[:70]


def _apply_end_marker_gate(page_scores: dict[int, BoundaryCandidate],
                           end_marker_pages: set[int],
                           block_start_page: int) -> None:
    """
    "끝" 마커가 일관된 문서에서 끝 없는 경계에 점수 패널티.

    모든 기술사 해설지는 토픽이 "끝"으로 종료됨. 이 구조적 규칙을 활용:
    - 문서에 끝 마커가 3개 이상이면 게이트 활성화
    - 이전 경계~현재 후보 사이에 "끝"이 없으면 score × 0.3 패널티
    - end_marker 신호가 있거나 복합 신호(3+)는 면제
    """
    if len(end_marker_pages) < 3:
        return  # 끝 마커 불충분 → 게이트 비활성화

    sorted_ends = sorted(end_marker_pages)

    for pg, cand in page_scores.items():
        if cand.score <= 0:
            continue
        # end_marker 신호가 있으면 면제 (끝 다음 페이지는 정당한 경계)
        if cand.signals.get("end_marker"):
            continue
        # 강한 명시적 신호(N교시M번, ★rating, header_kw)는 끝 게이트 면제
        if (cand.signals.get("session_topic")
                or cand.signals.get("star_rating")
                or cand.signals.get("header_kw")):
            continue
        # 복합 신호(3종 이상)는 면제
        active_signals = sum(1 for v in cand.signals.values() if v > 0)
        if active_signals >= 3:
            continue
        # block_start 신호만 있으면 면제 (블록 첫 페이지)
        if pg == block_start_page:
            continue

        # 이 후보 페이지 직전에 "끝" 마커가 있는지 확인
        has_preceding_end = any(ep < pg for ep in sorted_ends)
        if has_preceding_end:
            # 가장 가까운 이전 끝 마커 찾기
            nearest_end = max(ep for ep in sorted_ends if ep < pg)
            # 끝 마커와 현재 후보 사이 간격이 3페이지 이내면 정당
            if pg - nearest_end <= 3:
                continue

        # "끝" 없이 등장한 경계 → 강한 패널티
        # 단일 신호(I. 또는 font_heading만)는 거의 확실히 소제목이므로 0.1
        # 복합 신호(2개)는 약간의 증거가 있으므로 0.3
        penalty = 0.1 if active_signals <= 1 else 0.3
        cand.score *= penalty
        cand.signals["end_gate_penalty"] = -1  # 디버깅용 마커


def _apply_marker_heading_signal(block_elems: list, page_scores: dict,
                                  weights: SignalWeights,
                                  repeated_headers: set,
                                  cover_pages: set):
    """
    □/■/◇/◆ 등 마커 헤딩 기반 토픽 시작 탐지.

    일부 학원 PDF는 "□ 클라우드 컴퓨팅" 등 도형 마커로 토픽을 구분.
    페이지의 첫 의미 있는 element가 마커 헤딩이면 새 토픽 시작 후보.
    """
    page_first_marker: dict[int, dict] = {}
    for e in sorted(block_elems, key=lambda x: x["page"]):
        pg = e["page"]
        if pg in cover_pages or pg in page_first_marker:
            continue
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        if _MARKER_HEADING_PAT.match(c):
            page_first_marker[pg] = e

    for pg, e in page_first_marker.items():
        if pg not in page_scores:
            continue
        existing = page_scores[pg].signals
        if existing.get("I.") or existing.get("문제N") or existing.get("end_marker"):
            if not page_scores[pg].title:
                c = _norm(e["content"])
                # 마커 문자 제거 후 제목 추출
                page_scores[pg].title = c[1:].strip()[:70]
            continue

        score = weights.marker_heading * 7.0
        page_scores[pg].score += score
        page_scores[pg].signals["marker_heading"] = score
        if not page_scores[pg].title:
            c = _norm(e["content"])
            page_scores[pg].title = c[1:].strip()[:70]


def _apply_star_rating_signal(block_elems: list, page_scores: dict,
                               weights: SignalWeights,
                               repeated_headers: set,
                               cover_pages: set):
    """
    KPC ★ 난이도 마커 기반 토픽 시작 탐지.

    KPC 해설지는 각 토픽이 "ICT의 가치를 이끄는 사람 ★★★☆☆ ... 1. 토픽 제목"
    형태의 paragraph로 시작. ★ 마커가 포함된 긴 paragraph = 토픽 시작.

    "1." + 키워드("개요", "개념", "정의" 등)가 함께 있으면 제목 추출.
    """
    # 페이지별 첫 ★ paragraph 수집
    page_star: dict[int, dict] = {}
    for e in sorted(block_elems, key=lambda x: x["page"]):
        pg = e["page"]
        if pg in cover_pages or pg in page_star:
            continue
        if e.get("type") != "paragraph":
            continue
        c = e.get("content", "")
        if _STAR_RATING_PAT.search(c) and len(c) > 10:
            page_star[pg] = e

    for pg, e in page_star.items():
        if pg not in page_scores:
            continue
        # 이미 강한 신호가 있으면 스킵
        existing = page_scores[pg].signals
        if existing.get("end_marker") or existing.get("I."):
            continue

        score = weights.star_rating * 10
        page_scores[pg].score += score
        page_scores[pg].signals["star_rating"] = score

        # 제목 추출: "1." 뒤의 텍스트
        c = e.get("content", "")
        m = re.search(r'1\.\s*(.{5,60}?)(?:\s*[가나다라]\.|\s*-|\s*$)', c)
        if m and not page_scores[pg].title:
            page_scores[pg].title = m.group(1).strip()[:70]


def _apply_session_topic_signal(block_elems: list, page_scores: dict,
                                 weights: SignalWeights,
                                 repeated_headers: set,
                                 cover_pages: set):
    """
    라이지움 "N교시 M번" heading 기반 토픽 시작 탐지.

    라이지움은 각 토픽을 "1교시 1 번", "2교시3번" 등 heading으로 구분.
    _SESSION_TOPIC_PAT으로 매칭하여 각 발견 위치를 토픽 시작으로 마킹.
    """
    for e in sorted(block_elems, key=lambda x: x["page"]):
        if e.get("type") != "heading":
            continue
        c = _norm(e.get("content", ""))
        m = _SESSION_TOPIC_PAT.search(c)
        if not m:
            continue
        pg = e["page"]
        if pg in cover_pages or pg not in page_scores:
            continue
        # 이미 강한 신호가 있으면 보강만
        existing = page_scores[pg].signals
        if existing.get("session_topic"):
            continue

        score = weights.session_topic_num * 10
        page_scores[pg].score += score
        page_scores[pg].signals["session_topic"] = score


def _apply_header_keyword_signal(block_elems: list, page_scores: dict,
                                  weights: SignalWeights,
                                  cover_pages: set):
    """
    아이리포 반복 헤더 키워드 변화 기반 토픽 전환 탐지.

    각 페이지 첫 paragraph에서 "IT trends [키워드]" 패턴을 추출.
    연속 페이지 간 키워드가 변하면 새 토픽 시작.
    """
    # 페이지별 키워드 추출
    page_kw: dict[int, str] = {}
    seen = set()
    for e in sorted(block_elems, key=lambda x: x["page"]):
        pg = e["page"]
        if pg in seen or pg in cover_pages:
            continue
        if e.get("type") != "paragraph":
            continue
        m = _HEADER_KW_PAT.search(e.get("content", ""))
        if m:
            seen.add(pg)
            page_kw[pg] = m.group(1).strip()[:40]

    if len(page_kw) < 5:
        return

    # 키워드 변화점 = 새 토픽 시작
    prev_kw = None
    for pg in sorted(page_kw.keys()):
        kw = page_kw[pg]
        if prev_kw is not None and kw != prev_kw:
            if pg in page_scores:
                score = weights.header_kw_change * 10
                page_scores[pg].score += score
                page_scores[pg].signals["header_kw"] = score
        prev_kw = kw


def _apply_kr_restart_signal(block_elems: list, page_scores: dict,
                              repeated_headers: set,
                              cover_pages: set):
    """
    한글 소제목 "가." 리셋 신호.

    기술사 답안에서 각 토픽은 "가. 정의 → 나. 특징 → 다. 비교" 순서로 진행.
    "나." 이상이 나타난 후 "가."가 다시 등장하면 새 토픽 시작 가능성.

    인포레버 138응처럼 "I." 없이 "가./나." 소제목만으로 시작하는 토픽에서
    경계를 감지하는 보조 신호.

    단, 같은 페이지 또는 인접 페이지에 "II./III." 등 로마 숫자가 있으면
    동일 토픽 내 섹션 전환이므로 신호를 무시.
    """
    KR_ORDER = {'가': 0, '나': 1, '다': 2, '라': 3, '마': 4,
                '바': 5, '사': 6, '아': 7}
    _ROMAN_GE2 = re.compile(r'^(II|III|IV|V)\.\s+')

    # 페이지별 첫 한글 소제목 + 로마 숫자 II+ 존재 여부 수집
    page_first_kr: dict[int, tuple[int, str]] = {}
    page_has_roman_ge2: dict[int, bool] = {}

    for e in sorted(block_elems, key=lambda x: x["page"]):
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        pg = e["page"]
        if pg in cover_pages:
            continue

        if _ROMAN_GE2.match(c):
            page_has_roman_ge2[pg] = True

        if pg in page_first_kr:
            continue
        m = _KR_SUB_PAT.match(c)
        if m and m.group(1) in KR_ORDER:
            page_first_kr[pg] = (KR_ORDER[m.group(1)], m.group(2).strip()[:70])

    if not page_first_kr:
        return

    # "가." 리셋 탐지
    prev_order = -1
    for pg in sorted(page_first_kr.keys()):
        order, title = page_first_kr[pg]
        if order == 0 and prev_order >= 1:
            # 같은 페이지 또는 인접 페이지에 II./III. 있으면 무시
            if (page_has_roman_ge2.get(pg, False) or
                    page_has_roman_ge2.get(pg - 1, False)):
                prev_order = max(prev_order, order)
                continue
            score = 6.0
            if pg in page_scores:
                page_scores[pg].score += score
                page_scores[pg].signals["kr_restart"] = score
                if not page_scores[pg].title or len(title) > len(page_scores[pg].title):
                    page_scores[pg].title = title
        prev_order = max(prev_order, order)


def _extract_title(elements: list, page: int, repeated_headers: set) -> str:
    """페이지에서 토픽 제목 후보를 추출"""
    for e in elements:
        if e["page"] != page:
            continue
        c = _norm(e["content"])
        if c in repeated_headers or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        if _끝_PAT.match(c):
            continue

        # "문 제 N." 형식
        m = _MENTI_PAT.match(c)
        if m:
            return m.group(2).strip().split("\n")[0][:70]

        # "I. 제목" 형식
        if _ROMAN_I_PAT.match(c):
            return c[3:].strip()[:70]

        # "N. 제목" 형식
        m = _STD_NUM_PAT.match(c)
        if m and int(m.group(1)) == 1:
            return m.group(2).strip()[:70]

        # heading 타입이면 그냥 사용
        if e["type"] == "heading" and len(c) > 10:
            return c[:70]

    return f"토픽_p{page}"


# ─── Phase 4: 후보 선택 및 검증 ──────────────────────────────────

def select_boundaries(candidates: list[BoundaryCandidate],
                      session_block: SessionBlock,
                      weights: SignalWeights) -> list[BoundaryCandidate]:
    """
    후보 중에서 최종 토픽 경계를 선택.

    전략:
    1. 강한 신호(score >= threshold)는 무조건 포함
    2. 기대 토픽 수와 비교하여 부족하면 threshold를 낮춤
    3. 초과하면 낮은 점수 후보를 제거
    """
    if not candidates:
        return []

    expected = session_block.expected_topics
    scores = sorted([c.score for c in candidates], reverse=True)

    # 동적 임계값: 상위 expected개의 점수 중 최소값의 50%
    if len(scores) >= expected:
        top_n_min = scores[expected - 1]
        threshold = top_n_min * 0.5
    else:
        # 후보가 기대보다 적으면 모든 양수 점수 후보를 포함
        threshold = min(scores) * 0.5 if scores else 0

    selected = [c for c in candidates if c.score >= threshold]

    # 단일 신호 경계는 cutoff 시 우선 제거 (과분할 방지)
    # 단, 강한 명시적 신호(N교시M번, ★rating, header_kw)는 면제
    for c in selected:
        active = sum(1 for v in c.signals.values() if v > 0)
        if active == 1:
            has_explicit = (c.signals.get("session_topic")
                           or c.signals.get("star_rating")
                           or c.signals.get("header_kw"))
            if not has_explicit:
                c.score *= 0.6

    # 기대보다 많으면 점수 상위 expected개만 유지
    # block_start(5.0) 같은 약한 noise boundary가 정확히 제거됨
    max_count = expected
    if len(selected) > max_count:
        selected.sort(key=lambda c: c.score, reverse=True)
        selected = selected[:max_count]
        selected.sort(key=lambda c: c.page)

    return selected


def validate_results(boundaries: list[TopicBoundary],
                     sessions: list[SessionBlock]) -> list[str]:
    """
    최종 결과를 시험 구조와 대조하여 경고 목록을 반환.

    검증 항목:
    1. 교시별 토픽 수가 기대와 일치하는지
    2. 비정상적으로 긴 섹션(10페이지 이상)이 있는지
    3. 비정상적으로 짧은 섹션(0페이지)이 있는지
    """
    warnings = []

    # 교시별 토픽 수 검증
    for sess in sessions:
        if sess.session_num == 0:
            # 교시 미확정 → 전체 수만 확인
            if len(boundaries) != 31:
                warnings.append(
                    f"전체 토픽 수 {len(boundaries)}개 (기대: 31개)"
                )
            continue

        sess_topics = [b for b in boundaries if b.session == sess.session_num]
        if len(sess_topics) != sess.expected_topics:
            warnings.append(
                f"{sess.session_num}교시: {len(sess_topics)}개 탐지 "
                f"(기대: {sess.expected_topics}개)"
            )

    # 섹션 길이 이상 탐지
    for b in boundaries:
        pages = b.page_end - b.page_start + 1
        if pages >= 10:
            warnings.append(
                f"Q{b.num:02d} p{b.page_start}-{b.page_end} "
                f"({pages}p) 비정상적으로 긴 섹션 — 미분할 가능성"
            )
        if pages == 0:
            warnings.append(
                f"Q{b.num:02d} p{b.page_start} 빈 섹션"
            )
        if pages == 1 and b.session in (2, 3, 4) and b.fmt != "question_pages":
            warnings.append(
                f"Q{b.num:02d} p{b.page_start} "
                f"1페이지 섹션 — 과분할 가능성"
            )

    return warnings


def analyze_quality(boundaries: list[TopicBoundary],
                    sessions: list[SessionBlock],
                    elements: list,
                    total_pages: int,
                    warnings: list[str]) -> str:
    """
    분할 결과의 예상 완성률을 분석하여 사용자 안내 텍스트를 반환.

    PDF 분할 직후, 사용자가 결과 품질을 판단할 수 있도록
    교시 구분·신호 탐지·토픽 수 일치도 등을 종합한 리포트.
    """
    lines = []

    # ── 1. 문서 기본 정보 ──
    topic_bs = [b for b in boundaries if b.fmt != "question_pages"]
    q_bs = [b for b in boundaries if b.fmt == "question_pages"]
    total_topics = len(topic_bs)

    sess_label = "교시" if sessions and sessions[0].session_num <= 4 else "일차"
    if len(sessions) == 1 and sessions[0].session_num == 0:
        lines.append(f"총 {total_pages}p → {total_topics}개 토픽 분할")
        lines.append(f"교시/일차 구분: 미확인 (단일 블록)")
    else:
        sess_str = ", ".join(
            f"{s.session_num}{sess_label}(p{s.page_start}-{s.page_end})"
            for s in sessions
        )
        lines.append(f"총 {total_pages}p → {total_topics}개 토픽 분할")
        lines.append(f"블록 구분: {sess_str}")

    if q_bs:
        lines.append(f"문제지: {len(q_bs)}건 분리")

    # ── 2. 교시별 완성도 ──
    total_expected = 0
    total_detected = 0
    sess_details = []

    for sess in sessions:
        detected = len([b for b in topic_bs if b.session == sess.session_num])
        expected = sess.expected_topics
        total_expected += expected
        total_detected += detected

        if expected > 0:
            pct = min(detected / expected * 100, 100)
            status = "OK" if detected == expected else (
                f"+{detected - expected}" if detected > expected else
                f"-{expected - detected}"
            )
            sess_details.append(
                f"  {sess.session_num}{sess_label}: "
                f"{detected}/{expected}개 ({pct:.0f}%) {status}"
            )

    if sess_details:
        lines.append("교시별 탐지:")
        lines.extend(sess_details)

    # ── 3. 신호 품질 ──
    weights = calibrate_weights(elements, total_pages)
    active_signals = []
    for attr, label in [
        ('끝_marker', '"끝" 종료 마커'),
        ('roman_i', '"I." 소제목'),
        ('star_rating', '★ 난이도'),
        ('session_topic_num', '"N교시 M번"'),
        ('header_kw_change', '헤더 키워드 변화'),
        ('menti', '"문제 N."'),
        ('font_heading', '폰트 크기 헤딩'),
        ('marker_heading', '□/■ 마커'),
    ]:
        v = getattr(weights, attr, 0)
        if v > 0.1:
            active_signals.append(f"{label}({v:.0%})")

    if active_signals:
        lines.append(f"탐지 신호: {', '.join(active_signals)}")
    else:
        lines.append("탐지 신호: 없음 (신뢰도 낮음)")

    # ── 4. 이미지/OCR 비율 ──
    ocr_count = sum(1 for e in elements if e.get("source") == "ocr")
    if ocr_count > 0:
        ocr_pct = ocr_count / len(elements) * 100
        lines.append(f"OCR 사용: {ocr_pct:.0f}% ({ocr_count}/{len(elements)} elements)")
        if ocr_pct > 80:
            lines.append("  ⚠ 대부분 이미지 기반 — OCR 품질에 따라 정확도 저하 가능")

    # ── 5. 전체 완성률 ──
    if total_expected > 0:
        completeness = min(total_detected / total_expected, 1.0)
    else:
        completeness = 0.5  # 기대값 불명

    # 경고 수에 따른 감점
    penalty = len(warnings) * 0.02
    quality = max(completeness - penalty, 0.1)

    # 최종 등급
    if quality >= 0.95:
        grade = "우수"
    elif quality >= 0.80:
        grade = "양호"
    elif quality >= 0.60:
        grade = "보통"
    else:
        grade = "낮음"

    lines.append(f"예상 완성률: {quality:.0%} ({grade})")

    # ── 6. 경고 요약 ──
    if warnings:
        lines.append(f"경고 {len(warnings)}건:")
        for w in warnings[:5]:
            lines.append(f"  - {w}")
        if len(warnings) > 5:
            lines.append(f"  ... 외 {len(warnings) - 5}건")

    return "\n".join(lines)


# ─── 메인 진입점 ─────────────────────────────────────────────────

def detect_boundaries_v2(elements: list, total_pages: int,
                          session: str = "") -> tuple[list[TopicBoundary], list[str]]:
    """
    다중 신호 점수 기반 토픽 경계 탐지.

    Args:
        elements: ODL 파싱된 element 목록
        total_pages: 전체 페이지 수
        session: 세션 힌트 (예: "기출", "1교시" 등)

    Returns:
        (boundaries, warnings) 튜플
        - boundaries: TopicBoundary 목록
        - warnings: 검증 경고 메시지 목록
    """
    # 0. 반복 헤더 탐지
    repeated = _detect_repeated_headers(elements, total_pages)

    # 1. 교시 분리
    sessions = detect_sessions(elements, total_pages)

    # 2. 가중치 자기 교정
    weights = calibrate_weights(elements, total_pages)

    # 2.5 교시별 문제 페이지 탐지 → "문제지" 경계 생성
    question_boundaries = _build_question_boundaries(elements, sessions, repeated)

    # 3. 교시별 토픽 탐지
    all_boundaries: list[TopicBoundary] = []
    num_counter = 1

    for sess in sessions:
        # 후보 탐지
        candidates = score_boundaries(elements, sess, weights, repeated)

        # 후보 선택
        selected = select_boundaries(candidates, sess, weights)

        # TopicBoundary 변환 + 긴 구간 sub-split
        # 현재 탐지 수 vs 기대 수 → sub-split 필요 여부 결정
        current_count = len(selected)
        need_more = current_count < sess.expected_topics

        for i, cand in enumerate(selected):
            page_end = (selected[i + 1].page - 1
                        if i + 1 < len(selected)
                        else sess.page_end)
            if page_end < cand.page:
                page_end = cand.page

            max_score = max(c.score for c in selected) if selected else 1.0
            confidence = min(cand.score / max_score, 1.0) if max_score > 0 else 0.0

            span = page_end - cand.page + 1
            # 1교시 단답형(기대 ~2p)에서 3p 이상이면 sub-split 시도
            # 2-4교시 서술형(기대 ~5p)에서 8p 이상이면 sub-split 시도
            max_span = 3 if sess.expected_topics >= 10 else 6
            sub_splits = []
            if span > max_span:
                sub_splits = _sub_split_long_section(
                    elements, cand.page, page_end, repeated, weights,
                    try_kr_heading=need_more,
                )

            if len(sub_splits) > 1:
                for j, sub in enumerate(sub_splits):
                    sub_end = (sub_splits[j + 1]["page"] - 1
                               if j + 1 < len(sub_splits) else page_end)
                    all_boundaries.append(TopicBoundary(
                        num=num_counter,
                        title=sub["title"],
                        page_start=sub["page"],
                        page_end=sub_end,
                        session=sess.session_num,
                        confidence=confidence * 0.8,
                    ))
                    num_counter += 1
            else:
                all_boundaries.append(TopicBoundary(
                    num=num_counter,
                    title=cand.title or f"토픽{num_counter}",
                    page_start=cand.page,
                    page_end=page_end,
                    session=sess.session_num,
                    confidence=confidence,
                ))
                num_counter += 1

    # 3.5 문제지 경계를 해설 경계 앞에 삽입
    if question_boundaries:
        combined = []
        q_idx = 0
        h_idx = 0
        while q_idx < len(question_boundaries) or h_idx < len(all_boundaries):
            if q_idx < len(question_boundaries) and (
                h_idx >= len(all_boundaries) or
                question_boundaries[q_idx].page_start < all_boundaries[h_idx].page_start
            ):
                combined.append(question_boundaries[q_idx])
                q_idx += 1
            else:
                combined.append(all_boundaries[h_idx])
                h_idx += 1
        all_boundaries = combined
        # 번호 재부여
        for i, b in enumerate(all_boundaries):
            b.num = i + 1

    # 3.6 단일 페이지 토픽 병합 (과분할 방지)
    all_boundaries = _merge_short_topics(all_boundaries, sessions)

    # 4. 검증
    warnings = validate_results(all_boundaries, sessions)

    return all_boundaries, warnings


def _build_question_boundaries(elements: list,
                                sessions: list[SessionBlock],
                                repeated_headers: set) -> list[TopicBoundary]:
    """
    교시별 문제 페이지를 탐지하여 "문제지" TopicBoundary로 생성.

    문제 페이지 판별 기준:
    - 같은 페이지에 2개 이상의 번호+질문 키워드("설명하시오" 등)
    - 또는 교시 표지와 같은 페이지에 여러 `list` 번호 항목
    - 교시 시작 ~ 첫 해설 페이지 사이의 페이지

    연속 문제 페이지를 교시별로 묶어 "N교시 문제지" 경계를 생성.
    """
    question_pages: dict[int, set] = {}  # session_num → {page, ...}

    for sess in sessions:
        ps, pe = sess.page_start, sess.page_end
        block_elems = [e for e in elements if ps <= e.get("page", 0) <= pe]

        # 방법 1: 한 페이지에 질문 키워드 2개 이상
        page_q: dict[int, int] = {}
        page_has_content: dict[int, bool] = {}  # "I.", 끝, 해설 내용

        for e in block_elems:
            c = _norm(e.get("content", ""))
            pg = e.get("page", 0)
            if c in repeated_headers or _IGNORE_PAT.search(c):
                continue

            # 질문 패턴 카운트
            m = _STD_NUM_PAT.match(c)
            if m and _Q_KEYWORDS.search(c):
                page_q[pg] = page_q.get(pg, 0) + 1

            # 해설 콘텐츠 마커 (이 페이지에 해설이 있으면 문제지가 아님)
            if _ROMAN_I_PAT.match(c) or _끝_PAT.match(c):
                page_has_content[pg] = True
            if _STAR_RATING_PAT.search(c) and len(c) > 10:
                page_has_content[pg] = True

        qpages = set()
        for pg, qc in page_q.items():
            if qc >= 2 and not page_has_content.get(pg, False):
                qpages.add(pg)

        # 방법 2: 교시 시작 페이지 자체가 문제 목록 (list 항목이 5개 이상)
        page_list_count: dict[int, int] = {}
        for e in block_elems:
            # split_odl.py에서 list → paragraph로 변환되지만
            # 원본 heading에 "N교시" 있고 같은 페이지에 번호 항목 다수
            c = _norm(e.get("content", ""))
            pg = e.get("page", 0)
            if _STD_NUM_PAT.match(c) and len(c) > 10:
                page_list_count[pg] = page_list_count.get(pg, 0) + 1

        for pg, cnt in page_list_count.items():
            if cnt >= 5 and not page_has_content.get(pg, False):
                qpages.add(pg)

        if qpages:
            sn = sess.session_num if sess.session_num > 0 else 0
            question_pages.setdefault(sn, set()).update(qpages)

    # 교시별 연속 문제 페이지를 묶어 경계 생성
    result: list[TopicBoundary] = []
    for sn in sorted(question_pages.keys()):
        pages = sorted(question_pages[sn])
        if not pages:
            continue

        # 연속 페이지 그룹화
        groups: list[list[int]] = [[pages[0]]]
        for pg in pages[1:]:
            if pg - groups[-1][-1] <= 2:  # 2페이지 이내 간격 = 같은 그룹
                groups[-1].append(pg)
            else:
                groups.append([pg])

        for group in groups:
            label = f"{sn}교시 문제지" if sn > 0 else "문제지"
            result.append(TopicBoundary(
                num=0,  # 나중에 재번호
                title=label,
                page_start=min(group),
                page_end=max(group),
                session=sn,
                confidence=0.95,
                fmt="question_pages",
            ))

    return result


def _merge_short_topics(boundaries: list[TopicBoundary],
                        sessions: list[SessionBlock]) -> list[TopicBoundary]:
    """
    단일 페이지 토픽을 인접 토픽에 병합 (과분할 방지).

    병합 조건:
    - 토픽이 1페이지이고 confidence < 0.9이면 병합
    - 또는 토픽이 2페이지 이하이고 confidence < 0.5이면 병합 (끝 게이트 패널티 대상)
    - 해당 교시의 expected_topics >= 10 (1교시 단답형)이면 병합 안 함
    병합 방향: 이전 토픽에 흡수 (첫 토픽이면 다음에 흡수)
    세션 경계를 넘는 병합 금지.
    """
    if len(boundaries) <= 1:
        return boundaries

    # 세션별 expected_topics 맵
    sess_expected = {s.session_num: s.expected_topics for s in sessions}

    merged = []
    skip_next = False

    for i, b in enumerate(boundaries):
        if skip_next:
            skip_next = False
            continue

        pages = b.page_end - b.page_start + 1
        exp = sess_expected.get(b.session, 6)

        # 병합 대상이 아닌 경우 유지
        # 1교시 단답형(exp >= 10)은 높은 confidence만 보호
        is_short_format = exp >= 10
        should_merge = (
            (pages == 1 and b.confidence < 0.9 and not is_short_format) or
            (pages <= 2 and b.confidence < 0.4 and not is_short_format)
        )
        if not should_merge:
            merged.append(b)
            continue

        # 이전 토픽에 병합 시도
        if merged and merged[-1].session == b.session:
            merged[-1].page_end = b.page_end
        # 다음 토픽에 병합 시도
        elif i + 1 < len(boundaries) and boundaries[i + 1].session == b.session:
            boundaries[i + 1].page_start = b.page_start
        else:
            # 병합 불가 (세션 내 유일한 토픽) → 유지
            merged.append(b)

    # 번호 재부여
    for i, b in enumerate(merged):
        b.num = i + 1

    return merged


_KR_SUB_PAT = re.compile(r'^(가|나|다|라|마|바|사|아)\.\s+(.{5,})')


def _sub_split_long_section(elements: list, start: int, end: int,
                             repeated: set,
                             weights: SignalWeights,
                             try_kr_heading: bool = False) -> list[dict]:
    """
    비정상적으로 긴 섹션을 sub-split 시도.

    전략 (우선순위):
    1. "I." 재등장 → 새 토픽
    2. "문 제 N." 재등장 → 새 토픽
    3. 한글 소제목 "가." 재등장 (이전 페이지에 "나." 이상이 있었음) → 새 토픽
       인포레버 138응에서 I. 없이 "가." "나."로 시작하는 토픽 대응

    Returns: [{'page': int, 'title': str}, ...]  1개 이하이면 분할 없음
    """
    sec_elems = [e for e in elements if start <= e["page"] <= end]

    subs = []
    seen_pages = set()

    for e in sorted(sec_elems, key=lambda x: x["page"]):
        c = _norm(e["content"])
        if c in repeated or _IGNORE_PAT.search(c):
            continue
        pg = e["page"]
        if pg in seen_pages:
            continue

        # "I." 패턴
        if _ROMAN_I_PAT.match(c):
            seen_pages.add(pg)
            subs.append({"page": pg, "title": c[3:].strip()[:70]})
            continue

        # "문 제 N." 패턴
        m = _MENTI_PAT.match(c)
        if m:
            seen_pages.add(pg)
            subs.append({
                "page": pg,
                "title": m.group(2).strip().split("\n")[0][:70],
            })
            continue

    # sub 없거나 1개면 한글 소제목 리셋으로 재시도
    # → 기대보다 경계가 부족할 때만 (over-split 방지)
    if len(subs) <= 1 and try_kr_heading:
        kr_subs = _sub_split_by_kr_heading(sec_elems, start, repeated)
        if kr_subs:
            # kr_subs는 새 분할점만 포함 → 구간 시작을 앞에 추가
            subs = [{"page": start, "title": _extract_title(
                sec_elems, start, repeated)}] + kr_subs

    # sub 없거나 1개면 서술형 heading으로 최후 시도
    if len(subs) <= 1 and try_kr_heading:
        heading_subs = _sub_split_by_heading_title(sec_elems, start, end, repeated)
        if heading_subs:
            subs = [{"page": start, "title": _extract_title(
                sec_elems, start, repeated)}] + heading_subs

    # sub 없거나 1개면 분할하지 않음
    if len(subs) <= 1:
        return []

    # 첫 sub가 구간 시작보다 뒤에 있으면, 앞부분도 포함
    if subs[0]["page"] > start:
        subs.insert(0, {"page": start, "title": _extract_title(
            sec_elems, start, repeated)})

    return subs


def _sub_split_by_kr_heading(sec_elems: list, start: int,
                              repeated: set) -> list[dict]:
    """
    한글 소제목("가.", "나." ...) 패턴으로 토픽 경계를 감지.

    로직: 페이지별 가장 먼저 등장하는 한글 소제목 추적.
    "나." 이상이 나온 후 다시 "가."가 등장하면 새 토픽.

    인포레버 138응에서 I. 없이 "가. 개념", "나. 비교" 형태로
    시작하는 토픽을 감지하기 위한 보조 전략.
    """
    KR_ORDER = {'가': 0, '나': 1, '다': 2, '라': 3, '마': 4,
                '바': 5, '사': 6, '아': 7}

    # 페이지별 첫 한글 소제목 수집
    page_first_kr: dict[int, tuple[int, str]] = {}  # pg → (order, title)
    for e in sorted(sec_elems, key=lambda x: x["page"]):
        c = _norm(e["content"])
        if c in repeated or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        pg = e["page"]
        if pg in page_first_kr:
            continue
        m = _KR_SUB_PAT.match(c)
        if m:
            kr_char = m.group(1)
            if kr_char in KR_ORDER:
                page_first_kr[pg] = (KR_ORDER[kr_char], m.group(2).strip()[:70])

    if not page_first_kr:
        return []

    # "가." 리셋 탐지
    subs = []
    prev_order = -1
    for pg in sorted(page_first_kr.keys()):
        order, title = page_first_kr[pg]
        # "가."(order=0)가 등장하고, 이전에 "나."(order≥1) 이상이 있었다면 리셋
        if order == 0 and prev_order >= 1:
            subs.append({"page": pg, "title": title})
        prev_order = max(prev_order, order)

    return subs


def _sub_split_by_heading_title(sec_elems: list, start: int, end: int,
                                 repeated: set) -> list[dict]:
    """
    최후 수단: 긴 서술형 heading으로 토픽 경계를 감지.

    "I." 없이 시작하는 토픽에서 긴(25자+) 서술형 heading이
    페이지의 첫 의미 있는 element로 등장하면 새 토픽 시작으로 판단.

    소제목 형식(N., 가., I.)은 제외하여 과분할을 방지.
    """
    # 페이지별 첫 의미 있는 element 수집
    page_first_meaningful: dict[int, dict] = {}
    for e in sorted(sec_elems, key=lambda x: x["page"]):
        c = _norm(e["content"])
        if c in repeated or _IGNORE_PAT.search(c) or len(c) < 5:
            continue
        pg = e["page"]
        if pg not in page_first_meaningful:
            page_first_meaningful[pg] = e

    subs = []
    for pg in sorted(page_first_meaningful.keys()):
        if pg == start:
            continue
        e = page_first_meaningful[pg]
        if e["type"] != "heading":
            continue
        c = _norm(e["content"])
        if len(c) < 25:
            continue
        # 소제목/번호 형식 제외
        if _STD_NUM_PAT.match(c) or _KR_SUB_PAT.match(c) or _ROMAN_I_PAT.match(c):
            continue
        if _끝_PAT.match(c):
            continue
        subs.append({"page": pg, "title": c[:70]})

    return subs
