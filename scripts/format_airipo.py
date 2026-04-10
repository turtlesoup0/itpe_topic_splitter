"""
아이리포 포맷 전용 토픽 경계 탐지

아이리포 포맷 특성:
  - 이미지 기반 PDF → OCR 파싱
  - 토픽 종료: "끝" 마커 (ITPE와 동일)
  - 토픽 연속: "-뒷페이지에계속-" → 이전 토픽 계속
  - 세션 구분: 매 페이지 헤더에 "관리-N교시" 표시
  - 페이지 구조: 해설(p1~) → 시험문제 원본(마지막 ~10p)
  - 1교시: 토픽당 1페이지, 2~4교시: 토픽당 2+ 페이지

전략:
  1. "관리-N교시" 헤더로 세션 범위 결정
  2. "끝" 마커로 토픽 종료 (ITPE 로직 재활용)
  3. "계속" 페이지를 이전 토픽에 병합
  4. 해설 영역 이후(시험문제 원본)는 커버로 분리
"""

import re
from detect_boundaries_v2 import (
    TopicBoundary, SessionBlock,
    _norm, _IGNORE_PAT, _STD_NUM_PAT,
    _renumber_boundaries,
)

# ─── 아이리포 전용 패턴 ──────────────────────────────────────────

# "끝" 마커
_END_PAT = re.compile(
    r'^[\u201c\u201d"\'\u2018\u2019]*끝[\u201c\u201d"\'\u2018\u2019.]*\s*$'
)

# "관리-N교시" 세션 헤더 (하이픈/대시 변형 허용)
_SESSION_HEADER_PAT = re.compile(r'관리\s*[-‒–—]?\s*(\d)\s*교시')

# 계속 마커 (뒷페이지에계속, 앞페이지에서계속)
_CONT_FWD_PAT = re.compile(r'뒷\s*페이지\s*에?\s*계속')
_CONT_BACK_PAT = re.compile(r'앞\s*페이지\s*에서?\s*계속')

# 아이리포 반복 헤더
_AIRIPO_HEADER_PAT = re.compile(
    r'Copyright.*아이리포|아이리포\s*HR|아이리포\s*기술사회'
    r'|SW\s+DB\s+CA|IT\s*trends|Big\s*&?\s*Up'
)

# 커버/시험문제 원본
_COVER_PAT = re.compile(
    r'국가기술자격|기술사\s*시험문제|시험시간|100\s*분'
    r'|문제를?\s*선택하여|다음\s*문제\s*중'
)


# ─── 메인 함수 ───────────────────────────────────────────────────

def detect_airipo_boundaries(elements: list, sessions: list[SessionBlock],
                             repeated_headers: set,
                             total_pages: int) -> list[TopicBoundary]:
    """아이리포 포맷 전용 토픽 경계 탐지."""

    # 1. "관리-N교시"로 세션 범위 결정 (기존 sessions 무시)
    airipo_sessions = _detect_airipo_sessions(elements, total_pages)

    # 2. "끝" 마커 페이지 수집
    end_pages = _collect_end_pages(elements)

    # 3. "계속" 페이지 수집
    cont_pages = _collect_cont_pages(elements)

    # 4. 커버 영역 탐지 (해설 이후 시험문제 원본)
    cover_start = _detect_cover_start(elements, airipo_sessions, total_pages)

    # 5. 토픽 경계 생성
    boundaries = _build_boundaries(
        end_pages, cont_pages, airipo_sessions,
        cover_start, elements, repeated_headers, total_pages,
    )

    # 6. 번호 부여
    _renumber_boundaries(boundaries)

    return boundaries


# ─── 내부 함수 ───────────────────────────────────────────────────

def _detect_airipo_sessions(elements: list,
                            total_pages: int) -> list[SessionBlock]:
    """'관리-N교시' 헤더로 세션 범위를 결정."""
    # 각 페이지의 세션 번호 수집
    page_session: dict[int, int] = {}
    for e in elements:
        cc = re.sub(r'\s+', '', e.get("content", ""))
        m = _SESSION_HEADER_PAT.search(cc)
        if m:
            page_session[e["page"]] = int(m.group(1))

    if not page_session:
        # 세션 헤더 없으면 단일 블록
        return [SessionBlock(
            session_num=0, page_start=1, page_end=total_pages,
            expected_topics=31)]

    # 세션별 페이지 범위 결정
    sessions_map: dict[int, list[int]] = {}
    for pg, sess in sorted(page_session.items()):
        sessions_map.setdefault(sess, []).append(pg)

    result = []
    for sess_num in sorted(sessions_map.keys()):
        pages = sessions_map[sess_num]
        expected = 13 if sess_num == 1 else 6
        result.append(SessionBlock(
            session_num=sess_num,
            page_start=min(pages),
            page_end=max(pages),
            expected_topics=expected,
        ))

    # 세션 사이 빈 페이지를 이전 세션에 포함
    for i in range(len(result) - 1):
        gap_end = result[i + 1].page_start - 1
        if gap_end > result[i].page_end:
            result[i].page_end = gap_end

    return result


def _collect_end_pages(elements: list) -> list[int]:
    """모든 "끝" 마커 페이지를 수집."""
    pages = []
    seen = set()
    for e in elements:
        c = e.get("content", "").strip()
        if _END_PAT.match(c):
            pg = e["page"]
            if pg not in seen:
                pages.append(pg)
                seen.add(pg)
    return sorted(pages)


def _collect_cont_pages(elements: list) -> set[int]:
    """'계속' 마커 페이지를 수집 (뒷페이지에계속 또는 앞페이지에서계속)."""
    pages = set()
    for e in elements:
        cc = re.sub(r'\s+', '', e.get("content", ""))
        if _CONT_FWD_PAT.search(cc) or _CONT_BACK_PAT.search(cc):
            pages.add(e["page"])
    return pages


def _detect_cover_start(elements: list,
                        sessions: list[SessionBlock],
                        total_pages: int) -> int:
    """해설 영역 이후 시험문제 원본 시작 페이지를 탐지."""
    # 마지막 세션 종료 이후 = 커버 영역
    if sessions:
        last_session_end = max(s.page_end for s in sessions)
        # 마지막 세션 종료 후 "끝"이나 "관리" 없는 페이지 = 커버
        return last_session_end + 1

    # 세션 없으면 커버 탐지 시도
    for e in elements:
        cc = re.sub(r'\s+', '', e.get("content", ""))
        if _COVER_PAT.search(cc) and e["page"] > total_pages * 0.7:
            return e["page"]

    return total_pages + 1  # 커버 없음


def _find_session(page: int, sessions: list[SessionBlock]) -> int:
    """페이지가 속한 세션 번호 반환."""
    for s in sessions:
        if s.page_start <= page <= s.page_end:
            return s.session_num
    if sessions:
        return sessions[-1].session_num
    return 0


def _extract_airipo_title(elements: list, start_page: int, end_page: int,
                          repeated_headers: set) -> str:
    """아이리포 토픽 구간에서 제목을 추출."""
    page_elems = [e for e in elements
                  if start_page <= e["page"] <= min(start_page + 1, end_page)]

    for e in page_elems:
        c = _norm(e["content"])
        if len(c) < 5:
            continue
        cc = re.sub(r'\s+', '', c)
        # 헤더/노이즈 건너뛰기
        if _AIRIPO_HEADER_PAT.search(cc):
            continue
        if _SESSION_HEADER_PAT.search(cc):
            continue
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        if _END_PAT.match(c):
            continue
        if _CONT_FWD_PAT.search(cc) or _CONT_BACK_PAT.search(cc):
            continue
        # 도메인 키워드 바 (매 페이지 반복)
        if re.search(r'SW\s+DB\s+CA|IT\s*trends|AI/Bigdata', c):
            continue
        if re.match(r'^(page|출제영역|핵심키워드|상/중/하)$', cc):
            continue

        # "N. title" 패턴
        m = _STD_NUM_PAT.match(c)
        if m:
            return m.group(2).strip().split("\n")[0][:70]

        # 충분히 긴 텍스트
        if len(c) > 10:
            return c.split("\n")[0].strip()[:70]

    return f"토픽_p{start_page}"


def _build_boundaries(end_pages: list[int],
                      cont_pages: set[int],
                      sessions: list[SessionBlock],
                      cover_start: int,
                      elements: list,
                      repeated_headers: set,
                      total_pages: int) -> list[TopicBoundary]:
    """
    "끝" 구간 기반으로 토픽 경계를 생성.

    알고리즘:
    1. "끝" 페이지 = 토픽 종료
    2. 이전 "끝"+1 ~ 현재 "끝" = 토픽 범위
    3. "계속" 페이지는 이전 토픽에 포함 (별도 처리 불필요 — 이미 범위에 포함)
    4. cover_start 이후는 question_pages
    """
    boundaries: list[TopicBoundary] = []

    # 표지 (p1 ~ 첫 세션 시작 전)
    if sessions:
        first_session_start = min(s.page_start for s in sessions)
        if first_session_start > 1:
            boundaries.append(TopicBoundary(
                num=0, title="표지",
                page_start=1, page_end=first_session_start - 1,
                session=0, confidence=0.95, fmt="question_pages",
            ))

    # 토픽 경계 생성
    prev_end = 0

    for end_pg in end_pages:
        # 커버 영역은 스킵
        if end_pg >= cover_start:
            break

        search_start = prev_end + 1 if prev_end > 0 else 1

        # 세션 시작 이전은 스킵
        if sessions and search_start < sessions[0].page_start:
            search_start = sessions[0].page_start

        topic_start = search_start

        if topic_start > end_pg:
            prev_end = end_pg
            continue

        # 세션 할당
        sess_num = _find_session(topic_start, sessions)

        # 제목 추출
        title = _extract_airipo_title(
            elements, topic_start, end_pg, repeated_headers)

        boundaries.append(TopicBoundary(
            num=0,
            title=title,
            page_start=topic_start,
            page_end=end_pg,
            session=sess_num,
            confidence=0.85,
            fmt="airipo",
        ))

        prev_end = end_pg

    # 마지막 "끝" 이후 ~ cover_start 전에 남은 페이지
    if end_pages:
        last_end = max(ep for ep in end_pages if ep < cover_start) \
            if any(ep < cover_start for ep in end_pages) else 0
        if last_end > 0 and last_end + 1 < cover_start:
            remaining_start = last_end + 1
            sess_num = _find_session(remaining_start, sessions)
            title = _extract_airipo_title(
                elements, remaining_start, cover_start - 1, repeated_headers)
            if title != f"토픽_p{remaining_start}":
                boundaries.append(TopicBoundary(
                    num=0, title=title,
                    page_start=remaining_start,
                    page_end=cover_start - 1,
                    session=sess_num, confidence=0.60, fmt="airipo",
                ))

    # 시험문제 원본 (커버)
    if cover_start <= total_pages:
        boundaries.append(TopicBoundary(
            num=0, title="시험문제_원본",
            page_start=cover_start, page_end=total_pages,
            session=0, confidence=0.95, fmt="question_pages",
        ))

    # 정렬
    boundaries.sort(
        key=lambda b: (b.page_start, 0 if b.fmt == "question_pages" else 1))

    return boundaries
