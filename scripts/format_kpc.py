"""
KPC 포맷 전용 토픽 경계 탐지

KPC 포맷 특성:
  - 이미지 기반 PDF → OCR 파싱 (table cell 없음, 모든 element가 paragraph)
  - 토픽 종료: "기출풀이 의견" paragraph (모든 KPC PDF에서 안정적)
  - 토픽 시작 보조: ★ 난이도 마커 (OCR 품질에 따라 인식 안 될 수 있음)
  - 제목 패턴: "제 N. title" 또는 "N. title"
  - 세션 구분: "N교시" 텍스트 + 시험문제 표지 페이지
  - 반복 헤더: "누구나 ICT", "cafe.naver", 회차 번호 등

전략:
  "기출풀이 의견" 페이지 = 토픽 종료.
  ★ 마커 또는 이전 "기출풀이 의견"+1 = 토픽 시작.
  구간 내 "제 N." / "N. title" = 제목 추출.
"""

import re
from detect_boundaries_v2 import (
    TopicBoundary, SessionBlock,
    _norm, _IGNORE_PAT, _SESSION_PAT,
    _renumber_boundaries,
)

# ─── KPC 전용 패턴 ──────────────────────────────────────────────

# 토픽 종료: "기출풀이 의견" (공백 변형 허용)
_KPC_END_PAT = re.compile(r'기출\s*풀이\s*의견')

# ★ 난이도 마커: 정확히 ★★☆☆☆ 등 (토픽 시작 보조)
_STAR_EXACT_PAT = re.compile(r'^★[★☆]{1,4}$')

# 제목 패턴: "제 N. title" (1교시 해설)
_JE_NUM_PAT = re.compile(r'^제\s*(\d{1,2})\.\s*(.{3,})')

# 제목 패턴: "N. title" (2~4교시 해설)
_STD_NUM_PAT = re.compile(r'^(\d{1,2})\.\s+(.{5,})')

# 세션 커버/문제지 시그널
_COVER_PAT = re.compile(
    r'국가기술자격|기술사\s*시험문제|시험시간|100\s*분'
    r'|문제를?\s*선택하여|다음\s*문제\s*중'
)

# KPC 반복 헤더 (OCR에서 매 페이지 등장)
_KPC_HEADER_PAT = re.compile(
    r'누구나\s*ICT|cafe\.naver|정보처리기술사\s*기출풀이'
    r'|ICT의\s*가치를?\s*이끄는|All\s*rights'
)

# 노이즈 페이지 (출제빈도 통계 등)
_NOISE_PAT = re.compile(
    r'출제\s*빈도|출제\s*비율|도메인\s*별\s*출제|출제\s*경향'
    r'|감사의?\s*글|기출\s*풀이집'
)


# ─── 메인 함수 ───────────────────────────────────────────────────

def detect_kpc_boundaries(elements: list, sessions: list[SessionBlock],
                          repeated_headers: set,
                          total_pages: int) -> list[TopicBoundary]:
    """
    KPC 포맷 전용 토픽 경계 탐지.

    Returns:
        TopicBoundary 리스트
    """
    # 1. "기출풀이 의견" 페이지 수집 (토픽 종료)
    end_pages = _collect_end_pages(elements)

    # 2. ★ 마커 페이지 수집 (토픽 시작 보조)
    star_pages = _collect_star_pages(elements)

    # 3. 커버/문제지 페이지 탐지
    cover_pages = _detect_cover_pages(elements, total_pages)

    # 4. 노이즈 페이지 탐지
    noise_pages = _detect_noise_pages(elements, total_pages)

    # 5. 토픽 경계 생성
    boundaries = _build_boundaries(
        end_pages, star_pages, cover_pages, noise_pages,
        elements, sessions, repeated_headers, total_pages,
    )

    # 6. 번호 부여
    _renumber_boundaries(boundaries)

    return boundaries


# ─── 내부 함수 ───────────────────────────────────────────────────

def _collect_end_pages(elements: list) -> list[int]:
    """모든 "기출풀이 의견" 페이지를 수집 (정렬된 리스트)"""
    pages = []
    seen = set()
    for e in elements:
        c = re.sub(r'\s+', '', e.get("content", "").strip())
        if _KPC_END_PAT.search(c):
            pg = e["page"]
            if pg not in seen:
                pages.append(pg)
                seen.add(pg)
    return sorted(pages)


def _collect_star_pages(elements: list) -> list[int]:
    """★ 난이도 마커 페이지를 수집 (정렬된 리스트)"""
    pages = []
    seen = set()
    for e in elements:
        c = e.get("content", "").strip()
        if _STAR_EXACT_PAT.match(c):
            pg = e["page"]
            if pg not in seen:
                pages.append(pg)
                seen.add(pg)
    return sorted(pages)


def _detect_cover_pages(elements: list, total_pages: int) -> set[int]:
    """시험문제 표지/문제지 페이지 탐지"""
    cover = set()
    for e in elements:
        c = e.get("content", "").strip()
        c_collapsed = re.sub(r'\s+', '', c)
        if _COVER_PAT.search(c_collapsed):
            cover.add(e["page"])
    return cover


def _detect_noise_pages(elements: list, total_pages: int) -> set[int]:
    """출제빈도 통계 등 비토픽 페이지 탐지"""
    noise = set()
    for e in elements:
        c = re.sub(r'\s+', '', e.get("content", "").strip())
        if _NOISE_PAT.search(c):
            noise.add(e["page"])
    return noise


def _find_session(page: int, sessions: list[SessionBlock]) -> int:
    """페이지가 속한 세션 번호 반환"""
    for s in sessions:
        if s.page_start <= page <= s.page_end:
            return s.session_num
    if sessions:
        return sessions[-1].session_num
    return 1


def _extract_kpc_title(elements: list, start_page: int, end_page: int,
                       repeated_headers: set) -> str:
    """
    KPC 토픽 구간에서 제목을 추출.

    우선순위:
    1. "제 N. title" 패턴
    2. "N. title" 패턴 (첫 번째 것, N이 작은 숫자)
    3. 충분히 긴 비-헤더 paragraph
    """
    page_elems = [e for e in elements
                  if start_page <= e["page"] <= min(start_page + 1, end_page)]

    for e in page_elems:
        c = _norm(e["content"])
        if len(c) < 5:
            continue
        c_collapsed = re.sub(r'\s+', '', c)
        # 반복 헤더/노이즈 건너뛰기
        if _KPC_HEADER_PAT.search(c_collapsed):
            continue
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue

        # "제 N. title"
        m = _JE_NUM_PAT.match(c)
        if m:
            return m.group(2).strip().split("\n")[0][:70]

    # "제 N." 없으면 "N. title" 찾기
    for e in page_elems:
        c = _norm(e["content"])
        if len(c) < 5:
            continue
        c_collapsed = re.sub(r'\s+', '', c)
        if _KPC_HEADER_PAT.search(c_collapsed):
            continue
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue

        m = _STD_NUM_PAT.match(c)
        if m:
            num = int(m.group(1))
            if num <= 13:  # 토픽 번호 범위
                return m.group(2).strip().split("\n")[0][:70]

    # 폴백: 충분히 긴 첫 번째 비-헤더 paragraph
    for e in page_elems:
        c = _norm(e["content"])
        if len(c) < 10:
            continue
        c_collapsed = re.sub(r'\s+', '', c)
        if _KPC_HEADER_PAT.search(c_collapsed):
            continue
        if c in repeated_headers or _IGNORE_PAT.search(c):
            continue
        if _STAR_EXACT_PAT.match(c):
            continue
        # 메타 레이블 건너뛰기 (역, 난이도, 경, 도, 료 등)
        if re.match(r'^(역|난이도|경|도|료)\s', c):
            continue

        return c.split("\n")[0].strip()[:70]

    return f"토픽_p{start_page}"


def _build_boundaries(end_pages: list[int],
                      star_pages: list[int],
                      cover_pages: set[int],
                      noise_pages: set[int],
                      elements: list,
                      sessions: list[SessionBlock],
                      repeated_headers: set,
                      total_pages: int) -> list[TopicBoundary]:
    """
    "기출풀이 의견" 구간 기반으로 토픽 경계를 생성.

    알고리즘:
    1. 각 "기출풀이 의견" = 토픽 종료 페이지
    2. 토픽 시작 = ★ 마커 페이지 (있으면) 또는 이전 종료+1
    3. 커버/노이즈 페이지는 건너뜀
    """
    boundaries: list[TopicBoundary] = []

    # 커버+노이즈 통합
    skip_pages = cover_pages | noise_pages

    # 문제지 경계 생성 (세션 시작 ~ 첫 토픽 시작)
    question_boundaries = _build_question_page_boundaries(
        cover_pages, noise_pages, sessions, end_pages, star_pages)
    boundaries.extend(question_boundaries)

    # 토픽 경계 생성
    prev_end = 0  # 이전 "기출풀이 의견" 페이지

    for end_pg in end_pages:
        search_start = prev_end + 1 if prev_end > 0 else 1

        # ★ 마커가 이 구간에 있으면 토픽 시작으로 사용
        topic_start = None
        for sp in star_pages:
            if search_start <= sp <= end_pg:
                topic_start = sp
                break

        if topic_start is None:
            # ★ 없으면 이전 종료+1
            topic_start = search_start

        # 커버/노이즈 페이지 건너뛰기
        while topic_start in skip_pages and topic_start < end_pg:
            topic_start += 1

        if topic_start > end_pg:
            prev_end = end_pg
            continue

        # 세션 할당
        sess_num = _find_session(topic_start, sessions)

        # 제목 추출
        title = _extract_kpc_title(
            elements, topic_start, end_pg, repeated_headers)

        boundaries.append(TopicBoundary(
            num=0,
            title=title,
            page_start=topic_start,
            page_end=end_pg,
            session=sess_num,
            confidence=0.85,  # "기출풀이 의견" 기반은 높은 신뢰도
            fmt="kpc",
        ))

        prev_end = end_pg

    # 마지막 "기출풀이 의견" 이후 남은 페이지
    if end_pages and end_pages[-1] < total_pages:
        remaining_start = end_pages[-1] + 1
        # 남은 페이지가 커버/노이즈가 아니면 토픽으로 추가
        non_skip = [p for p in range(remaining_start, total_pages + 1)
                    if p not in skip_pages]
        if len(non_skip) >= 2:  # 최소 2페이지 이상이면 토픽 가능
            topic_start = non_skip[0]
            sess_num = _find_session(topic_start, sessions)
            title = _extract_kpc_title(
                elements, topic_start, total_pages, repeated_headers)
            boundaries.append(TopicBoundary(
                num=0,
                title=title,
                page_start=topic_start,
                page_end=total_pages,
                session=sess_num,
                confidence=0.60,
                fmt="kpc",
            ))

    # page_start 기준 정렬
    boundaries.sort(
        key=lambda b: (b.page_start, 0 if b.fmt == "question_pages" else 1))

    return boundaries


def _build_question_page_boundaries(
        cover_pages: set[int],
        noise_pages: set[int],
        sessions: list[SessionBlock],
        end_pages: list[int],
        star_pages: list[int]) -> list[TopicBoundary]:
    """세션 시작 ~ 첫 토픽 시작까지를 문제지로 묶음."""
    qbs: list[TopicBoundary] = []
    skip_pages = cover_pages | noise_pages

    for sess in sessions:
        # 이 세션의 첫 토픽 시작 페이지 찾기
        # ★ 마커가 있으면 그것이 첫 토픽
        first_star = None
        for sp in star_pages:
            if sess.page_start <= sp <= sess.page_end and sp not in skip_pages:
                first_star = sp
                break

        # "기출풀이 의견" 종료 역추적: 이 세션 내 첫 "기출풀이 의견" 페이지
        first_end = None
        for ep in end_pages:
            if sess.page_start <= ep <= sess.page_end:
                first_end = ep
                break

        # 첫 토픽 시작 = ★ 또는 (첫 기출풀이의견이 있는 구간의 시작)
        first_topic = first_star
        if first_topic is None and first_end is not None:
            # ★ 없으면 세션시작+1~2가 커버, 그 다음이 토픽 시작
            for p in range(sess.page_start, first_end + 1):
                if p not in skip_pages:
                    first_topic = p
                    break

        if first_topic is None or first_topic <= sess.page_start:
            continue

        # 세션 시작 ~ 첫 토픽 전까지 문제지
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
