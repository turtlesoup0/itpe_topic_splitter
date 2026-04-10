"""
포맷 자동 판별 + 포맷별 경계 ���지 디스패치

PDF의 element 패턴을 분석하여 학원 포맷을 식별하고,
해당 포맷에 맞는 전용 경계 탐지 함수를 호출한다.

포맷 판별 우선순위:
  1. KPC (NEW)  — TC "문 제" ≥ 5
  2. ITPE       — "끝" 마커 ≥ 5 + 메타 TC (도메인/출제자)
  3. 동기회/라이지움 — heading/paragraph "교시:번" ≥ 5
  4. 인포레버   — TC "관리N교시" 반복
  5. 아이리포   — TC "실전모의고사문제" 또는 "-뒷페이지에계속-"
  6. 동기회 125 — TC "[N-M]" 패턴 ≥ 5
  7. FALLBACK   — 기존 가중치 기반 로직
"""

import re
from enum import Enum, auto

# ─── 포맷 타입 ───────────────────────────────────────────────────

class FormatType(Enum):
    KPC = auto()
    ITPE = auto()
    DONGKIHOE = auto()      # 동기회 + 라이지움
    INFOLEVER = auto()       # 인포레버
    AIRIPO = auto()          # 아이리포
    FALLBACK = auto()        # 기존 가중치 기반

    def label(self) -> str:
        """사람이 읽을 수 있는 포맷 이름"""
        return {
            FormatType.KPC: "KPC",
            FormatType.ITPE: "ITPE",
            FormatType.DONGKIHOE: "동기회/라이지움",
            FormatType.INFOLEVER: "인포레버",
            FormatType.AIRIPO: "아이리포",
            FormatType.FALLBACK: "범용(fallback)",
        }[self]


# ─── 판별용 패턴 ─────────────────────────────────────────────────

_KPC_MUNJE_PAT = re.compile(r'^문\s*제$')
_KPC_END_PAT = re.compile(r'기출\s*풀이\s*의견')
_ITPE_END_PAT = re.compile(r'^[\u201c\u201d"\'"]?끝[\u201c\u201d"\'"]?\s*$')
_ITPE_META_PAT = re.compile(r'도메인|난이도|출제자|출제배경|참고문헌|키워드')
_DONGKI_PAT = re.compile(r'(\d)\s*교시\s*[:\s]*(\d+)\s*번')
_INFOLEVER_PAT = re.compile(r'관리\s*\d\s*교시')
_AIRIPO_MOCK_PAT = re.compile(r'실전\s*모의고사\s*문제')
_AIRIPO_CONT_PAT = re.compile(r'뒷\s*페이지\s*에?\s*계속')
_BRACKET_PAT = re.compile(r'^\[\d+-\d+\]')


# ─── 포맷 판별 ───────────────────────────────────────────────────

def detect_format(elements: list, total_pages: int) -> FormatType:
    """
    element 패턴을 분석하여 학원 포맷을 자동 판별.

    Args:
        elements: kordoc 파싱된 element 목록
        total_pages: 전체 페이지 수

    Returns:
        FormatType enum 값
    """
    # 카운터 초기화
    kpc_munje = 0
    kpc_opinion = 0       # "기출풀이 의견" (KPC 핵심 신호, OCR에서도 안정적)
    itpe_end = 0
    itpe_meta = 0
    dongki_session_topic = 0
    infolever_gwanri = 0
    airipo_mock = 0
    airipo_cont = 0
    bracket_pat = 0

    for e in elements:
        c = e.get("content", "").strip()
        c_collapsed = re.sub(r'\s+', '', c)  # 공백 제거 버전
        is_tc = e.get("is_table_cell", False)
        etype = e.get("type", "")

        # KPC: TC "문 제" (짧은 텍스트)
        if is_tc and len(c) < 10 and _KPC_MUNJE_PAT.search(c_collapsed):
            kpc_munje += 1

        # KPC: "기출풀이 의견" (OCR paragraph에서도 매칭)
        if _KPC_END_PAT.search(c_collapsed):
            kpc_opinion += 1

        # ITPE: "끝" 마커
        if _ITPE_END_PAT.match(c):
            itpe_end += 1

        # ITPE: 메타데이터 TC (도메인/난이도/출제자 등)
        if is_tc and _ITPE_META_PAT.search(c_collapsed):
            itpe_meta += 1

        # 동기회/라이지움: "N교시:M번" 패턴 (heading 또는 paragraph)
        if etype in ("heading", "paragraph") and _DONGKI_PAT.search(c):
            dongki_session_topic += 1

        # 인포레버: TC "관리N교시"
        if is_tc and _INFOLEVER_PAT.search(c_collapsed):
            infolever_gwanri += 1

        # 아이리포: "실전모의고사문제" 또는 "-뒷페이지에계속-"
        if _AIRIPO_MOCK_PAT.search(c_collapsed):
            airipo_mock += 1
        if _AIRIPO_CONT_PAT.search(c_collapsed):
            airipo_cont += 1

        # 동기회 125회: "[N-M]" 패턴
        if (is_tc or etype == "paragraph") and _BRACKET_PAT.match(c):
            bracket_pat += 1

    # ─── 우선순위 판별 ───────────────────────────────────────────
    # 고유 신호 우선: "교시:번"은 동기회/라이지움만 사용,
    # "끝"+메타TC는 ITPE와 동기회 공통이므로 교시:번을 먼저 체크

    # 1. KPC: "기출풀이 의견" ≥ 5 이면서 "끝"보다 많을 때 (KPC 고유)
    #    진짜 KPC는 의견≥끝 (의견이 모든 토픽에, 끝은 일부만)
    #    동기회(KPC해설자 참여)는 끝>의견 (끝이 모든 토픽에, 의견은 일부만)
    #    또는 TC "문 제" ≥ 5 (텍스트 기반 KPC)
    if kpc_munje >= 5:
        return FormatType.KPC
    if kpc_opinion >= 5 and kpc_opinion >= itpe_end:
        return FormatType.KPC

    # 2. 동기회/라이지움: "교시:번" 패턴 5개 이상 (동기회/라이지움 고유)
    if dongki_session_topic >= 5:
        return FormatType.DONGKIHOE

    # 3. ITPE: "끝" 마커 5개 이상 + 메타 TC 3개 이상
    if itpe_end >= 5 and itpe_meta >= 3:
        return FormatType.ITPE

    # 4. 인포레버: "관리N교시" TC 반복 (페이지 수의 30% 이상)
    if infolever_gwanri >= max(5, total_pages * 0.3):
        return FormatType.INFOLEVER

    # 5. 아이리포: "실전모의고사문제" 또는 연속 마커
    if airipo_mock >= 2 or airipo_cont >= 5:
        return FormatType.AIRIPO

    # 5.5 아이리포 본시험: "관리-N교���" TC가 있지만 인포레버 기준 미달
    #     아이리포 본시험은 "관리-1교시" 형태 (하이픈 포함)
    if infolever_gwanri >= 5 and airipo_cont >= 1:
        return FormatType.AIRIPO

    # 6. 동기회 125회: "[N-M]" 패���
    if bracket_pat >= 5:
        return FormatType.DONGKIHOE

    # 7. ITPE 관대한 조건: "끝" 마커만으로도 충분 (메타 TC 없는 경우)
    if itpe_end >= 10:
        return FormatType.ITPE

    # 8. Fallback
    return FormatType.FALLBACK


# ─── 디스패치 ────────────────────────────────────────────────────

def dispatch_boundaries(fmt: FormatType, elements: list,
                        sessions: list, repeated_headers: set,
                        total_pages: int):
    """
    포맷별 경계 탐지 함수를 호출.

    Returns:
        list[TopicBoundary] 또는 None (해당 포맷 모듈 미구현 시)
    """
    if fmt == FormatType.FALLBACK:
        return None

    # Lazy import로 순환 참조 방지
    try:
        if fmt == FormatType.ITPE:
            from format_itpe import detect_itpe_boundaries
            return detect_itpe_boundaries(elements, sessions,
                                          repeated_headers, total_pages)
        elif fmt == FormatType.KPC:
            from format_kpc import detect_kpc_boundaries
            return detect_kpc_boundaries(elements, sessions,
                                         repeated_headers, total_pages)
        elif fmt == FormatType.DONGKIHOE:
            from format_dongkihoe import detect_dongkihoe_boundaries
            return detect_dongkihoe_boundaries(elements, sessions,
                                               repeated_headers, total_pages)
        elif fmt == FormatType.INFOLEVER:
            from format_infolever import detect_infolever_boundaries
            return detect_infolever_boundaries(elements, sessions,
                                               repeated_headers, total_pages)
        elif fmt == FormatType.AIRIPO:
            from format_airipo import detect_airipo_boundaries
            return detect_airipo_boundaries(elements, sessions,
                                            repeated_headers, total_pages)
    except ImportError:
        # 포맷 모듈이 아직 구현되지 않음 → fallback
        return None

    return None
