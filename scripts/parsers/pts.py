"""PureTopicSegmenter (PTS) — 학원·시험 종별 무관 토픽 경계 분할기.

설계 원칙:
- 시험 본질 토큰만 사용 (학원 슬로건/디자인 의존 금지 — LR-007)
- 분할 알고리즘은 학원/시험 종별 메타 사용 안 함 (검증/표시는 별도)
- 다중 신호 동시 등장 = 강한 토픽 후보, 단일 신호 = 약한 후보
- Fail-loud: 자기검증 실패 시 ok=False → v2 폴백

파이프라인:
  1. strip_header (본문 앵커까지 trim)
  2. extract_candidates (페이지×라인 단위, 신호별 점수)
  3. cluster_into_topic_starts (강한/약한 후보 통합, 단조 증가 시퀀스 추출)
  4. build_chunks (Q_START 사이의 페이지 = 토픽 청크, 빈/시험지 페이지 제외)
  5. validate (페이지 누락 0, sanity 페이지 수)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from .base import (
    ParseResult,
    Topic,
    derive_round_id,
    sanitize_filename,
    get_expected_counts,
)
from .classifier import detect_publisher_and_type


# ─────────────────────────────────────────────────────────────────────────────
# 신호 패턴 (LR-007: 시험 형식 토큰만)
# ─────────────────────────────────────────────────────────────────────────────
QNUM_ONLY_RE = re.compile(r"^(\d{1,2})\.?$")  # '01' '01.' '11.' 단독
QNUM_TOPIC_RE = re.compile(r"^(?:제\s+)?(\d{1,2})\.\s+(.+)$")  # '1. 토픽' 또는 '제  1. 토픽' 인라인
QNUM_BUN_RE = re.compile(r"^(\d{1,2})\s*번\.?$")  # '6 번' '11번.'
SESSION_HDR_RE = re.compile(r"^제\s*([1-4])\s*교시")  # '제 1 교시'
SESSION_SHORT_RE = re.compile(r"^([1-4])\s*교시$")  # '1 교시' 단독
PROBLEM_ANCHOR = "문제"
PROBLEM_ANCHOR_LINES = {"문", "제", "문제"}  # 단독 라인이 problem 신호 (line_idx 0~3)
PROBLEM_INLINE_RE = re.compile(r"^문\s+제$")  # '문 제' 단일 라인
DOMAIN_LABEL_RE = re.compile(r"^(도메인|출제영역|난이도|키워드|출제배경|참고문헌|출제자)$")
DOMAIN_INLINE_RE = re.compile(r"^출제영역\s+(.+)$")
SELECT_HDR_RE = re.compile(r"\[\s*(관리|응용|정보관리|컴퓨터시스템응용)\s*(선택|기술사)\s*\]")
ROMAN_SECTION_RE = re.compile(r"^[ⅠⅡⅢⅣⅤIVX]+\.")
COPYRIGHT_RE = re.compile(r"Copyright\s*[ⓒ©c\*]", re.IGNORECASE)
PAGE_NUM_ONLY_RE = re.compile(r"^[\-\s]*\d{1,3}[\-\s]*$|^PAGE(\s*\d*)?$")

# 헤더 메타 패턴 — strip_header에서 'brand'와 'pagenum' 슬롯 인식
_BRAND_RE = re.compile(
    r"누구나\s*ICT|cafe\.naver|youtube|tistory|ITPE\s*\(|"
    r"Copyright\s*[ⓒ©*c]|"
    r"제\s*\d+\s*회.*?(해설집|해설|기출문제해설집|모의고사|기출풀이)|"
    r"^\d+\s*회$|"
    r"All\s*rights?\s*reserved|"
    r"KPC\s*기술사.*?IMPACT|ITPE.*?실전\s*명품|"
    r"기출\s*(문제|해설|풀이)\s*(집|해설집)?$|"
    r"인포레버컨설팅|Big&Up|여울동기회|두드림동기회|그루터기동기회|"
    r"ICT\s*의?\s*가치를?\s*이끄는|한국생산성본부|"
    r"^https?://|010-\d{4}-\d{4}|@[\w\.]+\.",
    re.IGNORECASE,
)
_BRAND_FRAGMENT_RE = re.compile(r"^(kpc|ICT의|가치를|이끄는|사람|한국생산성본부)$")
_PAGENUM_RE = re.compile(r"^[\-\s]*(\d{1,3}|PAGE(\s*\d*)?)[\-\s]*$")


def strip_header(lines: list[str], max_header: int = 14) -> list[str]:
    """헤더 인식 → 페이지번호 1개만 trim → 본문 시작.

    헤더 슬롯 (순서 가변, 모두 옵션):
      - 브랜드/저작권/학원명 라인들 (모두 noise — 매칭되면 skip)
      - 어절 분리 슬로건 ('kpc', 'ICT의', '가치를' 등) — 매칭되면 skip
      - 페이지 번호 단독 라인 — 1번만 trim (그 후 단독 숫자는 토픽 번호로 보존)
      - 헤더 깊이 max_header 한도

    본문 시작 앵커 (도달 시 즉시 stop):
      - QNUM_ONLY_RE 매칭 (1~30) — 토픽 번호 후보 (페이지번호 trim 후)
      - QNUM_TOPIC_RE 매칭 — 'N. 토픽'
      - PROBLEM_ANCHOR_LINES — '문', '제', '문제'
      - PROBLEM_INLINE_RE — '문 제'
      - SESSION_HDR_RE — '제 N 교시'
      - SELECT_HDR_RE — '[관리/응용 선택]'
    """
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    if not cleaned:
        return []

    pagenum_consumed = False
    out_start = 0
    for i in range(min(len(cleaned), max_header)):
        ln = cleaned[i]

        # 본문 앵커 도달 — stop
        m = QNUM_ONLY_RE.match(ln)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 30 and pagenum_consumed:
                out_start = i
                break
            # pagenum_consumed 안 됐으면 첫 숫자는 페이지번호로 trim
            if not pagenum_consumed:
                pagenum_consumed = True
                continue
            out_start = i
            break

        if (
            QNUM_TOPIC_RE.match(ln)
            or QNUM_BUN_RE.match(ln)
            or ln in PROBLEM_ANCHOR_LINES
            or PROBLEM_INLINE_RE.match(ln)
            or SESSION_HDR_RE.match(ln)
            or SELECT_HDR_RE.search(ln)
        ):
            out_start = i
            break

        # 헤더 노이즈 — skip
        if _BRAND_RE.search(ln) or _BRAND_FRAGMENT_RE.match(ln):
            continue
        if _PAGENUM_RE.match(ln) and not pagenum_consumed:
            pagenum_consumed = True
            continue

        # 알 수 없는 라인 — 본문으로 간주 (over-trim 방지)
        out_start = i
        break

    return cleaned[out_start:]


# ─────────────────────────────────────────────────────────────────────────────
# 페이지 후보 신호
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Signal:
    page_idx: int  # 0-indexed
    line_idx: int  # body line index (헤더 trim 후)
    type: str  # 'qnum_only' | 'qnum_topic' | 'qnum_bun' | 'session' | 'problem' | 'domain' | 'select' | 'roman' | 'copyright_only'
    num: Optional[int] = None
    session: Optional[int] = None
    text: str = ""


@dataclass
class TopicCandidate:
    """페이지 단위 토픽 시작 후보. 한 페이지에 여러 신호가 모이면 score↑."""
    page_idx: int
    score: float = 0.0
    num: Optional[int] = None
    session: Optional[int] = None
    title: str = ""
    signals: list[str] = field(default_factory=list)  # 디버그용

    def add(self, weight: float, signal: str, num: Optional[int] = None,
            session: Optional[int] = None, title: str = ""):
        self.score += weight
        self.signals.append(signal)
        if self.num is None and num is not None:
            self.num = num
        if self.session is None and session is not None:
            self.session = session
        if not self.title and title:
            self.title = title


def extract_signals_from_page(page_idx: int, body: list[str]) -> list[Signal]:
    """본문 라인에서 토픽 시작 신호 추출 (페이지 첫 12라인까지)."""
    signals: list[Signal] = []
    head = body[:14]
    for j, ln in enumerate(head):
        # qnum_only — 단독 숫자 라인 (대부분 모의고사 ITPE 시작)
        m = QNUM_ONLY_RE.match(ln)
        if m:
            n = int(m.group(1))
            if 0 <= n <= 30:  # 1~30 범위만 (페이지 번호 outlier 차단)
                signals.append(Signal(page_idx, j, "qnum_only", num=n, text=ln))
                continue
        # qnum_topic — 'N. 토픽'
        m = QNUM_TOPIC_RE.match(ln)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 30:
                signals.append(Signal(page_idx, j, "qnum_topic", num=n, text=m.group(2).strip()))
                continue
        # qnum_bun — 'N 번'
        m = QNUM_BUN_RE.match(ln)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 30:
                signals.append(Signal(page_idx, j, "qnum_bun", num=n, text=ln))
                continue
        # session
        m = SESSION_HDR_RE.match(ln) or SESSION_SHORT_RE.match(ln)
        if m:
            signals.append(Signal(page_idx, j, "session", session=int(m.group(1)), text=ln))
            continue
        # problem anchor — '문', '제', '문제', '문 제' 단독 라인 (line_idx 0~3에서만)
        if j <= 3 and (ln in PROBLEM_ANCHOR_LINES or PROBLEM_INLINE_RE.match(ln)):
            signals.append(Signal(page_idx, j, "problem", text=ln))
            continue
        # domain label
        if DOMAIN_LABEL_RE.match(ln) or DOMAIN_INLINE_RE.match(ln):
            signals.append(Signal(page_idx, j, "domain", text=ln))
            continue
        # select header
        if SELECT_HDR_RE.search(ln):
            signals.append(Signal(page_idx, j, "select", text=ln))
            continue
        # roman section
        if ROMAN_SECTION_RE.match(ln):
            signals.append(Signal(page_idx, j, "roman", text=ln))
            continue
    return signals


def is_empty_page(body: list[str]) -> bool:
    """본문이 거의 없거나 Copyright 한 줄만 있는 빈 페이지."""
    if len(body) <= 2 and any(COPYRIGHT_RE.search(ln) for ln in body):
        return True
    if len(body) == 0:
        return True
    return False


def is_session_paper(body: list[str]) -> bool:
    """시험지 표지 페이지 (해설 아님). [관리/응용 선택] 안내 페이지도 포함."""
    head = body[:8]
    for ln in head:
        if SESSION_HDR_RE.search(ln) and "시험시간" in ln:
            return True
        if SELECT_HDR_RE.search(ln):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 후보 통합 — 페이지 단위 점수 합산
# ─────────────────────────────────────────────────────────────────────────────
SIGNAL_WEIGHT = {
    "qnum_only": 1.0,    # 단독 숫자 라인 — ITPE 모의/본 5줄 슬롯의 첫 라인
    "qnum_topic": 1.5,   # 'N. 토픽' — sub-section으로 오인 위험. 단독 시 거부
    "qnum_bun": 1.5,     # 'N 번' — 동기회 등
    "problem": 1.0,      # '문제' / '문' / '문 제' 앵커 — 진짜 토픽 시작 결정 신호
    "domain": 0.6,       # '도메인'/'출제영역' 라벨 — 토픽 메타
    "select": 1.5,       # 시험지 표지 [관리/응용 선택]
    "roman": 0.3,        # 'I.' 보조 — 토픽 본문 첫 섹션
    "session": 0.0,
}

# 후보 임계 — 단일 강한 신호(qnum_topic 1.5, qnum_bun 1.5)는 sub-section 위험
# 다중 신호(problem+qnum_topic = 2.5, qnum_only+problem+domain = 2.6 등)만 통과
TOPIC_START_THRESHOLD = 2.0


SIGNAL_WINDOW = 10  # 헤더 직후 10라인 내 신호만 토픽 시작 후보


def cluster_into_candidates(
    pages: list[list[Signal]]
) -> list[TopicCandidate]:
    """페이지별 신호를 묶어 토픽 시작 후보 산출.

    임계 TOPIC_START_THRESHOLD 이상만 후보 — 단일 'qnum_topic' (1.5점) 같은
    sub-section 신호는 차단. 'problem'/'qnum_only'/'domain' 같은 시험 메타가
    동반될 때만 진짜 토픽 시작으로 인정.
    """
    candidates = []
    for pi, sigs in enumerate(pages):
        if not sigs:
            continue
        cand = TopicCandidate(page_idx=pi)
        head_sigs = [s for s in sigs if s.line_idx < SIGNAL_WINDOW]
        if not head_sigs:
            continue
        # 같은 type 신호 중복은 한 번만 카운트 (특히 qnum_topic — 본문에 'N.' 다중 등장 시)
        seen_types = set()
        for s in head_sigs:
            if s.type in seen_types and s.type in ("qnum_topic", "qnum_bun", "qnum_only"):
                continue
            seen_types.add(s.type)
            w = SIGNAL_WEIGHT.get(s.type, 0.1)
            cand.add(w, s.type, num=s.num, session=s.session, title=s.text)
        if cand.score >= TOPIC_START_THRESHOLD:
            candidates.append(cand)
    return candidates


def select_topic_starts(
    candidates: list[TopicCandidate]
) -> list[TopicCandidate]:
    """단조 증가 시퀀스 + 번호 리셋(N→1) 시 교시 +1 기반 선택.

    그리디: 점수가 높은 후보 우선, 같은 페이지 충돌 시 한 개만, 페이지 거리 sanity.
    """
    if not candidates:
        return []

    # 점수 기준 정렬 (높은 점수 우선) — 후 페이지 위치로 재정렬
    cands = sorted(candidates, key=lambda c: c.page_idx)

    # 번호 정합성 검사 — 단조 증가 또는 1로 리셋
    selected: list[TopicCandidate] = []
    current_session = 1
    last_num = 0

    for c in cands:
        if c.num is None:
            # 번호 없는 후보는 일단 제외 (약함)
            continue
        n = c.num
        # 번호 리셋 = 새 교시
        if last_num > 0 and n == 1 and last_num >= 3:
            current_session = min(current_session + 1, 4)
            c.session = current_session
            selected.append(c)
            last_num = 1
            continue
        # 단조 증가 (또는 같은 번호 — KPC 모의는 16번 다음 다시 1번 등)
        if n >= last_num and n - last_num <= 5:  # 점프 한도 (smell test)
            c.session = current_session
            selected.append(c)
            last_num = n
            continue
        # 단조성 위반 = 새 교시 가능성
        if n < last_num:
            current_session = min(current_session + 1, 4)
            c.session = current_session
            selected.append(c)
            last_num = n
            continue
        # 큰 점프 — 누락 또는 false positive. skip.
    return selected


# ─────────────────────────────────────────────────────────────────────────────
# 청크 산출 + 자기 검증
# ─────────────────────────────────────────────────────────────────────────────
def build_chunks(
    selected: list[TopicCandidate],
    page_kinds: list[str],
    total_pages: int,
) -> list[Topic]:
    """선택된 토픽 시작 사이의 페이지 = 청크. 빈/시험지 페이지는 청크 끝 직전에서 종료."""
    topics = []
    for idx, ts in enumerate(selected):
        ps = ts.page_idx
        next_start = selected[idx + 1].page_idx if idx + 1 < len(selected) else total_pages
        # 청크 끝 = 다음 시작 직전, 단 빈/시험지 페이지 만나면 그 직전까지
        boundary = next_start
        for k in range(ps + 1, next_start):
            if page_kinds[k] in ("EMPTY", "SESSION_PAPER"):
                boundary = k
                break
        topics.append(Topic.from_range(
            session=ts.session or 0,
            num=ts.num or 0,
            title=ts.title or "",
            ps=ps,
            pe=boundary - 1,
        ))
    return topics


# ─────────────────────────────────────────────────────────────────────────────
# Top-level 진입점
# ─────────────────────────────────────────────────────────────────────────────
def parse_pts(pdf_path: Path) -> ParseResult:
    """PureTopicSegmenter 진입점. ParseResult 반환."""
    if not pdf_path.exists():
        return ParseResult(ok=False, engine="pts", reason=f"파일 없음: {pdf_path}")

    doc = fitz.open(pdf_path)
    total_pages = doc.page_count

    # 1. 페이지별 본문 + 분류
    page_bodies = []
    page_kinds = []
    page_signals = []
    for i in range(total_pages):
        raw = doc.load_page(i).get_text()
        lines = raw.split("\n")
        body = strip_header(lines)
        page_bodies.append(body)
        if is_empty_page(body):
            page_kinds.append("EMPTY")
        elif is_session_paper(body):
            page_kinds.append("SESSION_PAPER")
        else:
            page_kinds.append("BODY")
        # SESSION_PAPER / EMPTY 페이지는 토픽 후보에서 제외 (시험지 표지가 토픽으로 오인되는 사고 방지)
        if page_kinds[-1] in ("SESSION_PAPER", "EMPTY"):
            page_signals.append([])
        else:
            page_signals.append(extract_signals_from_page(i, body))

    # 2. 후보 통합
    candidates = cluster_into_candidates(page_signals)
    selected = select_topic_starts(candidates)

    if not selected:
        doc.close()
        return ParseResult(
            ok=False, engine="pts",
            reason="토픽 후보 검출 실패",
        )

    # 3. 청크 산출
    topics = build_chunks(selected, page_kinds, total_pages)

    # 4. 자기 검증
    warnings = []
    if not topics:
        doc.close()
        return ParseResult(ok=False, engine="pts", reason="청크 0건")

    # 시험 메타와 매칭 — 카운트가 ±2 안에 들어와야 ok=True
    pub, et = detect_publisher_and_type(pdf_path)
    expected = get_expected_counts(pub, et)
    actual_counts = {s: sum(1 for t in topics if t.session == s) for s in [1, 2, 3, 4]}
    meta_ok = True
    if expected:
        diffs = []
        for s, exp_n in expected.items():
            act_n = actual_counts.get(s, 0)
            if abs(act_n - exp_n) > 2:
                meta_ok = False
                diffs.append(f"M{s}: {act_n} (기대 {exp_n})")
        if not meta_ok:
            doc.close()
            return ParseResult(
                ok=False, engine="pts",
                reason=f"시험 메타 카운트 불일치: {', '.join(diffs)}",
                topics=topics,  # 디버그용
            )

    # 페이지 sanity
    for t in topics:
        if t.pages > 30:
            warnings.append(f"M{t.session} Q{t.num}: {t.pages}p (분리 의심)")

    # 5. 분할 산출 (옵션 — 호출자가 분할 안 할 수도)
    round_id = derive_round_id(pdf_path)
    summary = f"{round_id}: {len(topics)}건 (M1={sum(1 for t in topics if t.session==1)}, M2={sum(1 for t in topics if t.session==2)}, M3={sum(1 for t in topics if t.session==3)}, M4={sum(1 for t in topics if t.session==4)})"

    doc.close()
    return ParseResult(
        ok=True,
        engine="pts",
        round_id=round_id,
        topics=topics,
        warnings=warnings,
        summary=summary,
    )


def split_pts(pdf_path: Path, out_dir: Path) -> ParseResult:
    """parse_pts + 분할 PDF 산출."""
    result = parse_pts(pdf_path)
    if not result.ok:
        return result

    src = fitz.open(pdf_path)
    target = out_dir / result.round_id
    target.mkdir(parents=True, exist_ok=True)

    files = []
    name_seen: dict[str, int] = {}
    for t in result.topics:
        title_safe = sanitize_filename(t.title or f"Q{t.num:02d}")
        sess_label = f"M{t.session}" if t.session else "M?"
        base = f"{result.round_id}_{sess_label}_Q{t.num:02d}_{title_safe}"
        if base in name_seen:
            name_seen[base] += 1
            base = f"{base}_{chr(ord('a') + name_seen[base] - 1)}"
        else:
            name_seen[base] = 1
        out_path = target / f"{base}.pdf"
        new_doc = fitz.open()
        new_doc.insert_pdf(src, from_page=t.page_start - 1, to_page=t.page_end - 1)
        new_doc.save(out_path)
        new_doc.close()
        files.append({"path": str(out_path), "filename": out_path.name})

    src.close()
    result.files = files
    return result
