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

    def dominant_signal(self) -> str:
        signals = {
            '끝': self.끝_marker, 'topic_end': self.topic_end,
            'I.': self.roman_i, '문제N': self.menti,
            'N.restart': self.std_num_restart,
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
    """문서 전체에서 반복 등장하는 헤더/푸터를 탐지"""
    heading_counts = Counter(
        e["content"].strip() for e in elements if e["type"] == "heading"
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

    # 2. 표지가 부족하면 단일 블록 (교시 내 토픽 탐지에서 처리)
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
        c = e["content"].strip()
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
        if e.get("source") != "ocr" and _끝_PAT.match(e["content"].strip())
    )

    # "I." 패턴 수
    roman_i_count = sum(
        1 for e in elements
        if _ROMAN_I_PAT.match(e["content"].strip())
    )

    # "문 제 N." 패턴 수
    menti_count = sum(
        1 for e in elements
        if _MENTI_PAT.match(e["content"].strip())
    )

    # 소제목 번호 (N.) — heading 타입에서만
    std_nums = []
    for e in elements:
        if e["type"] != "heading":
            continue
        m = _STD_NUM_PAT.match(e["content"].strip())
        if m:
            std_nums.append((e["page"], int(m.group(1))))

    # "1."의 등장 횟수 = 토픽 시작 가능 횟수 (번호 리셋 신호)
    num_one_count = sum(1 for _, n in std_nums if n == 1)

    # "기출풀이 의견" 등 토픽 종료 마커 수
    topic_end_count = sum(
        1 for e in elements
        if _TOPIC_END_PAT.match(e["content"].strip())
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

    # 정규화: 가장 강한 신호를 1.0으로
    max_w = max(w.끝_marker, w.topic_end, w.roman_i, w.menti,
                w.std_num_restart, 0.01)
    w.끝_marker /= max_w
    w.topic_end /= max_w
    w.roman_i /= max_w
    w.menti /= max_w
    w.std_num_restart /= max_w

    return w


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
        c = e["content"].strip()
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
        c = e["content"].strip()
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

    noise = stat_noise | qlist_noise
    if not noise:
        return noise

    # ── 프론트매터 확장 (통계 noise만 트리거) ─────────────────────
    # 시험 문제 목록은 교시 시작부에 있을 수 있으므로 확장하지 않음
    block_start_page = min(e["page"] for e in block_elems)
    early_noise = {p for p in stat_noise if p - block_start_page < 10}

    if early_noise:
        first_signal_page = None
        for e in sorted(block_elems, key=lambda x: x["page"]):
            c = e["content"].strip()
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
        c = e["content"].strip()
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
    end_marker_pages = set()
    if weights.끝_marker > 0:
        for e in block_elems:
            if _끝_PAT.match(e["content"].strip()):
                end_marker_pages.add(e["page"])
    if weights.topic_end > 0:
        for e in block_elems:
            if _TOPIC_END_PAT.match(e["content"].strip()):
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
            c = e["content"].strip()
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
            m = _MENTI_PAT.match(e["content"].strip())
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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

    return warnings


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

    # 4. 검증
    warnings = validate_results(all_boundaries, sessions)

    return all_boundaries, warnings


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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
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
        c = e["content"].strip()
        if len(c) < 25:
            continue
        # 소제목/번호 형식 제외
        if _STD_NUM_PAT.match(c) or _KR_SUB_PAT.match(c) or _ROMAN_I_PAT.match(c):
            continue
        if _끝_PAT.match(c):
            continue
        subs.append({"page": pg, "title": c[:70]})

    return subs
