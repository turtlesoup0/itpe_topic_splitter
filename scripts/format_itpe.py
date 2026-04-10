"""
ITPE 포맷 전용 토픽 경계 탐지

ITPE 포맷 특성:
  - 토픽 종료: "끝" 마커 (curly/straight quotes 포함)
  - 토픽 시작: 메타데이터 TC 클러스터 (도메인/난이도/키워드/출제배경/참고문헌/출제자)
  - 세션 구분: 긴 paragraph 내 "제 N 교시" 또는 heading
  - 적용 대상: ITPE 학원 본시험 + 모의고사, KPC OLD(120~122회)

전략:
  "끝" → "끝" 구간 내에서 메타TC 시작점 = 토픽 시작 페이지.
  "끝" 직후~다음 메타TC 직전 = 문제지/표지(noise).
"""

import re
from detect_boundaries_v2 import (
    TopicBoundary, SessionBlock, BoundaryCandidate,
    _norm, _끝_PAT, _IGNORE_PAT, _MENTI_PAT, _ROMAN_I_PAT,
    _STD_NUM_PAT, _extract_title, _renumber_boundaries,
)

# ─── ITPE 전용 패턴 ─────────────────────────────────────────────

# "끝" 마커: curly quotes, straight quotes, 마침표 등 허용
_ITPE_END_PAT = re.compile(
    r'^[\u201c\u201d"\'\u2018\u2019]*끝[\u201c\u201d"\'\u2018\u2019.]*\s*$'
)

# 메타데이터 TC 레이블 (공백 제거 후 매칭)
_META_LABELS = re.compile(
    r'^(도메인|난이도|키워드|출제배경|출제자|해설자|참고문헌)$'
)

# 세션 표지 시그널 (문제지/커버 페이지)
_SESSION_COVER_PAT = re.compile(
    r'국가기술자격|기술사\s*시험문제|시험시간\s*:\s*100\s*분'
    r'|문제를?\s*선택하여|다음\s*문제\s*중'
)

# 선택문제 마커 (모의고사)
_SELECT_PAT = re.compile(r'선택\s*문제')


# ─── 메인 함수 ───────────────────────────────────────────────────

def detect_itpe_boundaries(elements: list, sessions: list[SessionBlock],
                           repeated_headers: set,
                           total_pages: int) -> list[TopicBoundary]:
    """
    ITPE 포맷 전용 토픽 경계 탐지.

    Returns:
        TopicBoundary 리스트 (num, session_q는 _renumber_boundaries로 후처리)
    """
    # 1. "끝" 마커 페이지 수집
    end_pages = _collect_end_pages(elements)

    # 2. 메타TC 클러스터 시작 페이지 수집
    meta_start_pages = _collect_meta_start_pages(elements, end_pages)

    # 3. 문제지/표지 페이지 탐지
    cover_pages = _detect_cover_pages(elements, total_pages)

    # 4. 토픽 경계 생성
    boundaries = _build_boundaries(
        end_pages, meta_start_pages, cover_pages,
        elements, sessions, repeated_headers, total_pages,
    )

    # 5. 번호 부여
    _renumber_boundaries(boundaries)

    return boundaries


# ─── 내부 함수 ───────────────────────────────────────────────────

def _collect_end_pages(elements: list) -> list[int]:
    """모든 "끝" 마커 페이지를 수집 (정렬된 리스트)"""
    pages = []
    seen = set()
    for e in elements:
        c = e.get("content", "").strip()
        if _ITPE_END_PAT.match(c):
            pg = e["page"]
            if pg not in seen:
                pages.append(pg)
                seen.add(pg)
    return sorted(pages)


def _collect_meta_start_pages(elements: list,
                              end_pages: list[int]) -> list[int]:
    """
    메타데이터 TC 클러스터의 시작 페이지를 수집.

    "도메인", "난이도" 등 메타 레이블 TC가 처음 나타나는 페이지 중,
    해당 페이지에 메타 레이블이 2개 이상 있는 페이지만 선택.
    (단일 메타 레이블은 본문 내 우연 발생일 수 있음)
    """
    # 페이지별 메타 레이블 수 카운트
    page_meta_count: dict[int, int] = {}
    for e in elements:
        if not e.get("is_table_cell"):
            continue
        c_collapsed = re.sub(r'\s+', '', e.get("content", ""))
        if _META_LABELS.match(c_collapsed):
            pg = e["page"]
            page_meta_count[pg] = page_meta_count.get(pg, 0) + 1

    # 메타 레이블 2개 이상인 페이지만 선택
    meta_pages = sorted(pg for pg, cnt in page_meta_count.items() if cnt >= 2)
    return meta_pages


def _detect_cover_pages(elements: list, total_pages: int) -> set[int]:
    """문제지/표지/선택문제 페이지를 탐지"""
    cover = set()
    for e in elements:
        c = e.get("content", "")
        if _SESSION_COVER_PAT.search(c):
            cover.add(e["page"])
        if _SELECT_PAT.search(c) and e.get("type") == "heading":
            cover.add(e["page"])
    return cover


def _find_session(page: int, sessions: list[SessionBlock]) -> int:
    """페이지가 속한 세션 번호 반환"""
    for s in sessions:
        if s.page_start <= page <= s.page_end:
            return s.session_num
    # 세션 못 찾으면 가장 가까운 세션
    if sessions:
        return sessions[-1].session_num
    return 1


def _build_boundaries(end_pages: list[int],
                      meta_start_pages: list[int],
                      cover_pages: set[int],
                      elements: list,
                      sessions: list[SessionBlock],
                      repeated_headers: set,
                      total_pages: int) -> list[TopicBoundary]:
    """
    "끝" 구간 + 메타TC 시작점으로 토픽 경계를 생성.

    알고리즘:
    1. 이전 "끝" 페이지 이후, 다음 "끝" 페이지까지가 하나의 토픽 구간
    2. 각 구간 내에서 메타TC 시작 페이지 = 토픽 시작
    3. 메타TC가 없는 구간은 이전 "끝" + 1 페이지를 시작으로 사용
    4. 커버 페이지는 문제지로 분리
    """
    boundaries: list[TopicBoundary] = []

    # 문제지 경계 (세션 시작 전 커버 페이지들)
    question_boundaries = _build_question_page_boundaries(
        cover_pages, sessions, meta_start_pages, end_pages)
    boundaries.extend(question_boundaries)

    # 토픽 경계 생성
    prev_end = 0  # 이전 "끝" 페이지 (0 = 문서 시작 전)

    for i, end_pg in enumerate(end_pages):
        # 이 토픽의 시작 페이지 결정
        search_start = prev_end + 1 if prev_end > 0 else 1

        # 이 구간 내 메타TC 시작 페이지 찾기
        topic_start = None
        for mp in meta_start_pages:
            if search_start <= mp <= end_pg:
                topic_start = mp
                break

        if topic_start is None:
            # 메타TC 없으면 이전 끝 + 1
            topic_start = search_start

        # 커버 페이지 건너뛰기
        while topic_start in cover_pages and topic_start < end_pg:
            topic_start += 1

        if topic_start > end_pg:
            # 전체가 커버 페이지 → 스킵
            prev_end = end_pg
            continue

        # 세션 할당
        sess_num = _find_session(topic_start, sessions)

        # 제목 추출
        title = _extract_title(elements, topic_start, repeated_headers)

        boundaries.append(TopicBoundary(
            num=0,  # _renumber_boundaries에서 재부여
            title=title,
            page_start=topic_start,
            page_end=end_pg,
            session=sess_num,
            confidence=0.85,  # "끝" 기반은 높은 신뢰도
            fmt="itpe",
        ))

        prev_end = end_pg

    # 마지막 "끝" 이후 남은 페이지 처리
    if end_pages and end_pages[-1] < total_pages:
        remaining_start = end_pages[-1] + 1
        # 남은 구간에 메타TC가 있으면 토픽이 더 있을 수 있음
        remaining_meta = [mp for mp in meta_start_pages if mp >= remaining_start]
        if remaining_meta:
            topic_start = remaining_meta[0]
            if topic_start not in cover_pages:
                sess_num = _find_session(topic_start, sessions)
                title = _extract_title(elements, topic_start, repeated_headers)
                boundaries.append(TopicBoundary(
                    num=0,
                    title=title,
                    page_start=topic_start,
                    page_end=total_pages,
                    session=sess_num,
                    confidence=0.60,  # "끝" 없이 끝남 → 낮은 신뢰도
                    fmt="itpe",
                ))

    # page_start 기준 정렬
    boundaries.sort(key=lambda b: (b.page_start, 0 if b.fmt == "question_pages" else 1))

    return boundaries


def _build_question_page_boundaries(cover_pages: set[int],
                                     sessions: list[SessionBlock],
                                     meta_start_pages: list[int],
                                     end_pages: list[int]) -> list[TopicBoundary]:
    """
    커버/문제지 페이지를 question_pages 경계로 변환.

    세션 시작 페이지 ~ 첫 토픽 시작 전까지를 문제지로 묶음.
    """
    qbs: list[TopicBoundary] = []

    for sess in sessions:
        # 이 세션의 첫 토픽 시작 페이지 찾기
        first_topic = None
        for mp in meta_start_pages:
            if mp >= sess.page_start and mp <= sess.page_end and mp not in cover_pages:
                first_topic = mp
                break

        if first_topic is None:
            # 메타TC 없으면 첫 "끝" 페이지 기반으로 추정
            for ep in end_pages:
                if ep >= sess.page_start and ep <= sess.page_end:
                    # 끝 페이지 자체가 토픽의 일부 → 커버는 세션 시작~끝-1
                    first_topic = ep
                    break

        if first_topic is None or first_topic <= sess.page_start:
            continue

        # 세션 시작 ~ 첫 토픽 전까지 커버 페이지가 있으면 문제지
        cover_in_range = [p for p in cover_pages
                          if sess.page_start <= p < first_topic]
        if cover_in_range or first_topic > sess.page_start + 1:
            q_start = sess.page_start
            q_end = first_topic - 1
            if q_end >= q_start:
                qbs.append(TopicBoundary(
                    num=0,
                    title=f"문제지_{sess.session_num}교시",
                    page_start=q_start,
                    page_end=q_end,
                    session=sess.session_num,
                    confidence=0.95,
                    fmt="question_pages",
                ))

    return qbs
