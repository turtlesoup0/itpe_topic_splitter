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
QNUM_TOPIC_RE = re.compile(r"^(\d{1,2})\.\s+(.+)$")  # '1. 토픽'
QNUM_BUN_RE = re.compile(r"^(\d{1,2})\s*번\.?$")  # '6 번' '11번.'
SESSION_HDR_RE = re.compile(r"^제\s*([1-4])\s*교시")  # '제 1 교시'
SESSION_SHORT_RE = re.compile(r"^([1-4])\s*교시$")  # '1 교시' 단독
PROBLEM_ANCHOR = "문제"
PROBLEM_INLINE_RE = re.compile(r"^문\s+제$")
DOMAIN_LABEL_RE = re.compile(r"^(도메인|출제영역|난이도|키워드|출제배경|참고문헌|출제자)$")
DOMAIN_INLINE_RE = re.compile(r"^출제영역\s+(.+)$")
SELECT_HDR_RE = re.compile(r"\[\s*(관리|응용|정보관리|컴퓨터시스템응용)\s*(선택|기술사)\s*\]")
ROMAN_SECTION_RE = re.compile(r"^[ⅠⅡⅢⅣⅤIVX]+\.")
COPYRIGHT_RE = re.compile(r"Copyright\s*[ⓒ©c\*]", re.IGNORECASE)
PAGE_NUM_ONLY_RE = re.compile(r"^[\-\s]*\d{1,3}[\-\s]*$|^PAGE(\s*\d*)?$")

# 헤더 노이즈 — 본문 시작 앵커 검출 시 무시
HEADER_NOISE_PATTERNS = [
    re.compile(r"^[\-\s]*\d{1,3}[\-\s]*$"),  # 페이지 번호
    re.compile(r"PAGE(\s*\d*)?$"),
    re.compile(r"누구나\s*ICT|cafe\.naver|youtube|tistory|ITPE\s*\("),  # URL/광고
    re.compile(r"Copyright\s*[ⓒ©*c]", re.IGNORECASE),
    re.compile(r"^\d+\s*회$"),  # 'NN 회'
    re.compile(r"제\s*\d+\s*회.*?(해설집|해설|기출문제해설집|모의고사)"),
    re.compile(r"All\s*rights?\s*reserved", re.IGNORECASE),
    re.compile(r"KPC\s*기술사.*?IMPACT|ITPE.*?실전\s*명품"),
    re.compile(r"기출\s*(문제|해설|풀이)\s*(집|해설집)?$"),
    re.compile(r"인포레버컨설팅|Big&Up|여울동기회|두드림동기회|그루터기동기회"),
    re.compile(r"ICT\s*의?\s*가치를?\s*이끄는|한국생산성본부"),  # 슬로건 — trim 대상
    re.compile(r"^[ICT의가치를이끄는사람한국생산성본부kpc]{1,6}$"),  # 어절 분리 슬로건
    re.compile(r"010-\d{4}-\d{4}|@[\w\.]+\."),  # 전화/이메일
    re.compile(r"^https?://"),
]


def is_header_noise(line: str) -> bool:
    """헤더 노이즈 라인인지. 본문 앵커 검출 시 skip."""
    if len(line) < 1:
        return True
    return any(p.search(line) for p in HEADER_NOISE_PATTERNS)


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


def strip_header(lines: list[str], max_header: int = 14) -> list[str]:
    """헤더 노이즈를 trim — 본문 의미 라인이 등장하는 위치까지."""
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    if not cleaned:
        return []
    body_start = 0
    for i in range(min(len(cleaned), max_header)):
        ln = cleaned[i]
        if is_header_noise(ln):
            continue
        # 학원/슬로건 어절 분리 (1~6글자 짧은 라인)이 연속 등장하는 경우는 헤더로 간주
        if len(ln) <= 6 and not (
            QNUM_ONLY_RE.match(ln) or QNUM_BUN_RE.match(ln)
            or SESSION_SHORT_RE.match(ln) or ln == PROBLEM_ANCHOR or ln == "문" or ln == "제"
        ):
            continue
        body_start = i
        break
    return cleaned[body_start:]


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
        # problem anchor
        if ln == PROBLEM_ANCHOR or ln == "문" or PROBLEM_INLINE_RE.match(ln):
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
    """시험지 표지 페이지 (해설 아님)."""
    head = body[:8]
    for ln in head:
        if SESSION_HDR_RE.search(ln) and "시험시간" in ln:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 후보 통합 — 페이지 단위 점수 합산
# ─────────────────────────────────────────────────────────────────────────────
SIGNAL_WEIGHT = {
    "qnum_only": 1.0,    # 모의 ITPE: 강력
    "qnum_topic": 1.5,   # 'N. 토픽' — 가장 강력
    "qnum_bun": 1.5,     # 'N 번' — 동기회 등
    "problem": 0.8,      # '문제' — 헤더에 있으면 약하지만 함께면 강
    "domain": 0.6,       # '도메인'/'출제영역' 라벨
    "select": 1.2,       # 시험지 표지 — 정확한 토픽 라벨 동반
    "roman": 0.3,        # 'I.' 보조 (토픽 시작 직후 첫 섹션)
    "session": 0.0,      # 교시 라벨 — 새 교시 신호로만 사용
}


def cluster_into_candidates(
    pages: list[list[Signal]]
) -> list[TopicCandidate]:
    """페이지별 신호를 묶어 토픽 시작 후보 산출.

    신호가 본문 첫 7라인 안에 모이면 강한 후보, 그 너머는 약한 보조.
    """
    candidates = []
    for pi, sigs in enumerate(pages):
        if not sigs:
            continue
        cand = TopicCandidate(page_idx=pi)
        # 첫 7라인 안의 신호만 토픽 시작 후보로 인정 (그 이후는 본문 sub-section)
        head_sigs = [s for s in sigs if s.line_idx < 7]
        if not head_sigs:
            continue
        for s in head_sigs:
            w = SIGNAL_WEIGHT.get(s.type, 0.1)
            cand.add(w, s.type, num=s.num, session=s.session, title=s.text)
        if cand.score >= 0.8:
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
