"""
KPC 모의고사 해설집 PDF 진단 스크립트 (dry-run, 읽기 전용 + --split 모드).

기대 구조:
- 매 페이지 상단 4줄 헤더 (브랜드/회차/부제/학원명) — 변형 허용
- 문제 시작: '문' '제' 2줄(또는 '문    제' 1줄) + 'N. 토픽'
- 교시 표지 페이지 없음 — 번호 리셋(1로 돌아감)으로 교시 자동 분리
- 1교시 16문제, 2~4교시 각 8문제 (총 40)

사용:
    python scripts/diagnose_kpc_mock.py <pdf_path>             # 진단만
    python scripts/diagnose_kpc_mock.py <pdf_path> --split <out_dir>  # 진단 + 분할
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


# 헤더 인식 — 4줄 형태 (변형 허용)
HEADER_BRAND_RE = re.compile(r"누구나\s*ICT.*?전문가.*?세상")
HEADER_ROUND_RE = re.compile(r"^\d+\s*회$")
HEADER_SUB_RE = re.compile(r"ICT\s*의?\s*가치를\s*이끄는")
HEADER_PUB_RE = re.compile(r"KPC.*?기술사.*?IMPACT.*?실전모의고사")

# 문제 시작 앵커
PROBLEM_ANCHOR = "문"
PROBLEM_ANCHOR_2 = "제"
# 단일 라인 변형: '문         제' / '문 제' 등
PROBLEM_ANCHOR_INLINE_RE = re.compile(r"^문\s+제$")
# 출제영역 / 난이도 (서술형 종료 신호)
DOMAIN_INLINE_RE = re.compile(r"^출제영역\s*(.*)$")
# 'N. 토픽' 또는 'N. 토픽 (multi-line)'
QNUM_TOPIC_RE = re.compile(r"^(\d{1,2})\.\s*(.+)$")


@dataclass
class PageInfo:
    page_idx: int
    raw_lines: list[str]
    body_lines: list[str]
    kind: str = "UNKNOWN"
    session: Optional[int] = None
    q_num: Optional[int] = None
    q_topic: Optional[str] = None
    q_domain: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def strip_header(lines: list[str]) -> list[str]:
    """KPC 매 페이지 상단 4줄 헤더 제거. 변형(공백/구두점)을 너그럽게 허용."""
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    if not cleaned:
        return []
    out_start = 0
    # 1. 브랜드 라인 (보통 라인 0)
    if out_start < len(cleaned) and HEADER_BRAND_RE.search(cleaned[out_start]):
        out_start += 1
    # 2. 'NN 회' 라인
    if out_start < len(cleaned) and HEADER_ROUND_RE.match(cleaned[out_start]):
        out_start += 1
    # 3. 'ICT의 가치를 이끄는 (사람)' 라인
    if out_start < len(cleaned) and HEADER_SUB_RE.search(cleaned[out_start]):
        out_start += 1
    # 4. 'KPC 기술사 IMPACT 실전모의고사' 라인
    if out_start < len(cleaned) and HEADER_PUB_RE.search(cleaned[out_start]):
        out_start += 1
    return cleaned[out_start:]


_TOPIC_END_PREFIXES = ("출", "난", "키", "참")
_QNUM_ONLY_RE = re.compile(r"^(\d{1,2})\.\s*$")
_JE_INLINE_QNUM_RE = re.compile(r"^제\s+(\d{1,2})\.\s*(.*)$")


def _gather_topic_after(body: list[str], start: int, max_lines: int = 18) -> str:
    """start 부터 토픽 종료 신호 전까지 라인을 모아 한 문장으로 합침."""
    frags = []
    for j in range(start, min(len(body), start + max_lines)):
        ln = body[j]
        if any(ln.startswith(p) for p in _TOPIC_END_PREFIXES) or ln.startswith("출제") or DOMAIN_INLINE_RE.match(ln):
            break
        frags.append(ln)
    return " ".join(frags).strip()


def classify_page(body: list[str]) -> tuple[str, dict]:
    """본문 라인들을 보고 페이지 종류 분류.

    KPC 변형 4가지를 모두 처리:
      A) '문' / '제' / 'N. 토픽'              (정상 형태)
      B) '문 제' (한 줄) / 'N. 토픽'           (3교시 등 단일 라인 헤더)
      C) '문' / '제 N. 토픽' (인라인)           (1교시 단답형 다수)
      D) '문' / '제' / 'N.' / 단어1 / 단어2…    (fragmented — 어절 단위)
    """
    if not body:
        return "EMPTY", {}

    # '문' 또는 '문 제' 라인의 위치 찾기
    mun_idx = None
    inline_munje = False
    for s in range(min(len(body), 5)):
        if body[s] == PROBLEM_ANCHOR:
            mun_idx = s
            break
        if PROBLEM_ANCHOR_INLINE_RE.match(body[s]):
            mun_idx = s
            inline_munje = True
            break
    if mun_idx is None:
        return "Q_BODY", {}

    # B) '문 제' 단일 라인
    if inline_munje:
        for k in range(mun_idx + 1, min(len(body), mun_idx + 5)):
            m = QNUM_TOPIC_RE.match(body[k])
            if m:
                topic = (m.group(2).strip() + " " + _gather_topic_after(body, k + 1, 5)).strip()
                return "Q_START", {"q_num": int(m.group(1)), "q_topic": topic, "variant": "B"}
            mq = _QNUM_ONLY_RE.match(body[k])
            if mq:
                topic = _gather_topic_after(body, k + 1)
                return "Q_START", {"q_num": int(mq.group(1)), "q_topic": topic, "variant": "B-frag"}
        return "Q_BODY", {}

    # 다음 라인 = '제' 또는 '제 ...'
    if mun_idx + 1 >= len(body):
        return "Q_BODY", {}
    next_ln = body[mun_idx + 1]

    # C) '제 N. 토픽' 인라인
    inline_match = _JE_INLINE_QNUM_RE.match(next_ln)
    if inline_match and inline_match.group(2).strip():
        topic = inline_match.group(2).strip()
        # 토픽이 다음 라인까지 흐르는 케이스
        topic_extra = _gather_topic_after(body, mun_idx + 2, 4)
        if topic_extra:
            topic = (topic + " " + topic_extra).strip()
        return "Q_START", {"q_num": int(inline_match.group(1)), "q_topic": topic, "variant": "C"}

    # A) '제' 단독 다음 'N. 토픽' (변형 A) 또는 'N.' + fragment (변형 D)
    if next_ln == PROBLEM_ANCHOR_2 or next_ln.startswith("제"):
        scan_start = mun_idx + 2
        # '제 N.' 만 있고 토픽이 다음 라인부터인 경우
        je_only_qnum = _JE_INLINE_QNUM_RE.match(next_ln)
        if je_only_qnum and not je_only_qnum.group(2).strip():
            # '제 N.' 형태 → 다음 라인부터 fragment 모음
            topic = _gather_topic_after(body, scan_start)
            return "Q_START", {"q_num": int(je_only_qnum.group(1)), "q_topic": topic, "variant": "C-frag"}
        # '제' 단독 다음에 'N. 토픽' 또는 'N.' 검색
        for k in range(scan_start, min(len(body), scan_start + 5)):
            m = QNUM_TOPIC_RE.match(body[k])
            if m:
                topic = (m.group(2).strip() + " " + _gather_topic_after(body, k + 1, 5)).strip()
                return "Q_START", {"q_num": int(m.group(1)), "q_topic": topic, "variant": "A"}
            mq = _QNUM_ONLY_RE.match(body[k])
            if mq:
                topic = _gather_topic_after(body, k + 1)
                return "Q_START", {"q_num": int(mq.group(1)), "q_topic": topic, "variant": "D"}

    return "Q_BODY", {}


def analyze_pages(doc: fitz.Document) -> tuple[list, list]:
    """페이지 분류 + 교시 자동 분리 (번호 리셋 기반)."""
    pages: list[PageInfo] = []
    current_session: Optional[int] = None
    last_q_num: Optional[int] = None

    for i in range(doc.page_count):
        raw = doc.load_page(i).get_text()
        raw_lines = raw.split("\n")
        body = strip_header(raw_lines)
        kind, meta = classify_page(body)

        if kind == "Q_START":
            q_num = meta["q_num"]
            if current_session is None:
                current_session = 1
            elif last_q_num is not None and q_num < last_q_num:
                # 단조성 위반 = 새 교시 시작 (KPC는 교시 표지가 없음)
                current_session = min(current_session + 1, 4)
            last_q_num = q_num

        info = PageInfo(
            page_idx=i,
            raw_lines=raw_lines,
            body_lines=body,
            kind=kind,
            session=current_session,
        )
        if kind == "Q_START":
            info.q_num = meta["q_num"]
            info.q_topic = meta["q_topic"]
            if meta.get("variant") == "B":
                info.notes.append("variant-B")
        pages.append(info)

    # Q_START 들 사이의 페이지 범위 산출
    q_starts = [p for p in pages if p.kind == "Q_START"]
    q_list: list[tuple[int, int, str, int, int]] = []
    for idx, qs in enumerate(q_starts):
        next_start = q_starts[idx + 1].page_idx if idx + 1 < len(q_starts) else doc.page_count
        q_list.append((qs.session or 0, qs.q_num or 0, qs.q_topic or "", qs.page_idx, next_start - 1))

    return pages, q_list


_ALLOWED_FILENAME_RE = re.compile(
    r"[^ -~"          # ASCII printable
    r"가-힯"            # 한글 완성형 가-힣
    r"ㄱ-ㆎ"            # 한글 자모
    r"‐-⁯"            # 일반 punctuation
    r" -ɏ"            # 라틴 확장 (악센트 등)
    r"]+"
)


def sanitize_filename(s: str, max_len: int = 70, max_bytes: int = 180) -> str:
    """파일명 안전 정규화. macOS APFS는 NFD 변환 후 NAME_MAX(255 바이트) 한도 +
    일부 비-BMP/깨진 유니코드 시퀀스를 거부하므로 화이트리스트 + 길이 둘 다 적용."""
    import unicodedata
    s = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", " ", s)
    # PDF 추출 시 발생하는 깨진 유니코드(히브리어 ׿, 키릴 ӿ 등) 제거
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


def derive_round_id(pdf_path: Path) -> str:
    """'모의_KPC129_2604_합.pdf' → 'KPC129-2604'."""
    stem = pdf_path.stem
    stem = re.sub(r"^모의_", "", stem)
    stem = re.sub(r"[-_]?합$", "", stem)
    stem = stem.replace("_", "-")
    return stem


def write_split_pdfs(
    src_doc: fitz.Document,
    pages: list,
    q_list: list,
    round_id: str,
    out_dir: Path,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    name_seen: dict[str, int] = {}
    for sess, num, topic, ps, pe in q_list:
        topic_safe = sanitize_filename(topic) if topic else f"Q{num:02d}"
        base = f"{round_id}_M{sess}_Q{num:02d}_{topic_safe}"
        if base in name_seen:
            name_seen[base] += 1
            base = f"{base}_{chr(ord('a') + name_seen[base] - 1)}"
        else:
            name_seen[base] = 1
        out_path = out_dir / f"{base}.pdf"
        new_doc = fitz.open()
        new_doc.insert_pdf(src_doc, from_page=ps, to_page=pe)
        new_doc.save(out_path)
        new_doc.close()
        written += 1
    return written


def is_kpc_mock_pdf(pdf_path: Path) -> bool:
    """KPC 모의고사 PDF 판정 — 너그럽게 통과시키고 자기검증으로 false positive 걸러냄.
    파일명에 KPC 또는 첫 5페이지 안에 KPC 모의고사 헤더가 있으면 True.
    """
    name = pdf_path.name
    if re.search(r"KPC", name, re.IGNORECASE):
        return True
    try:
        doc = fitz.open(pdf_path)
        # 1페이지 헤더가 단어 단위 분리된 변형 PDF가 있어 첫 5p 까지 검사
        for i in range(min(doc.page_count, 5)):
            text = doc.load_page(i).get_text()
            if HEADER_BRAND_RE.search(text) and HEADER_PUB_RE.search(text):
                doc.close()
                return True
        doc.close()
    except Exception:
        pass
    return False


def split_kpc_mock(pdf_path: Path, out_dir: Path) -> dict:
    warnings: list[str] = []
    if not pdf_path.exists():
        return {"ok": False, "round_id": "", "files": [], "warnings": [f"파일 없음: {pdf_path}"], "summary": ""}

    doc = fitz.open(pdf_path)
    pages, q_list = analyze_pages(doc)

    if not q_list:
        return {
            "ok": False, "round_id": "", "files": [], "warnings": ["문제 검출 실패 — KPC 모의고사 포맷이 아닐 수 있습니다."], "summary": "",
        }

    # 교시별 카운트 / 연속성 검사
    expected_range = {1: (13, 17), 2: (6, 9), 3: (6, 9), 4: (6, 9)}
    for s in [1, 2, 3, 4]:
        nums = sorted(set(n for ss, n, *_ in q_list if ss == s))
        if not nums:
            warnings.append(f"제{s}교시 검출 0건")
            continue
        cnt = len(nums)
        lo, hi = expected_range[s]
        if not (lo <= cnt <= hi):
            warnings.append(f"제{s}교시 카운트 이상: {cnt} (기대 {lo}~{hi})")
        gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        if gaps:
            warnings.append(f"제{s}교시 번호 누락: {gaps}")

    round_id = derive_round_id(pdf_path)
    target = out_dir / round_id
    nq = write_split_pdfs(doc, pages, q_list, round_id, target)

    files = []
    for p in sorted(target.iterdir()):
        if p.is_file() and p.suffix == ".pdf":
            files.append({"path": str(p), "filename": p.name})

    counts_per_session = ", ".join(
        f"M{s}={sum(1 for ss, *_ in q_list if ss == s)}" for s in [1, 2, 3, 4]
    )
    summary = f"{round_id}: {len(files)}건 ({counts_per_session})"
    topics = [
        {
            "session": sess,
            "num": num,
            "title": topic,
            "page_start": ps + 1,  # 1-indexed
            "page_end": pe + 1,
            "pages": pe - ps + 1,
        }
        for sess, num, topic, ps, pe in q_list
    ]
    doc.close()
    return {
        "ok": True,
        "round_id": round_id,
        "files": files,
        "warnings": warnings,
        "summary": summary,
        "topics": topics,
    }


def diagnose(pdf_path: Path, split_dir: Optional[Path] = None) -> int:
    if not pdf_path.exists():
        print(f"❌ 파일이 없습니다: {pdf_path}")
        return 2

    print(f"=== {pdf_path.name} ===")
    doc = fitz.open(pdf_path)
    print(f"총 페이지: {doc.page_count}\n")

    pages, q_list = analyze_pages(doc)

    print("[페이지 분류]")
    for p in pages:
        sess = f"M{p.session}" if p.session else "  "
        if p.kind == "Q_START":
            extra = f"Q{p.q_num:02d} | {p.q_topic[:70]}"
        else:
            extra = ""
        note = ""
        if p.notes:
            note = "  // " + ", ".join(p.notes)
        print(f"  p.{p.page_idx + 1:>3} {sess} {p.kind:10} {extra}{note}")

    print("\n[문제 요약]")
    for sess, num, topic, ps, pe in q_list:
        print(f"  M{sess} Q{num:02d} | p.{ps+1:>3}-{pe+1:<3} | {topic[:90]}")

    print("\n[자기 검증]")
    expected_range = {1: (13, 17), 2: (6, 9), 3: (6, 9), 4: (6, 9)}
    actual: dict[int, int] = {}
    for sess, *_ in q_list:
        actual[sess] = actual.get(sess, 0) + 1
    all_ok = True
    for s in [1, 2, 3, 4]:
        a = actual.get(s, 0)
        lo, hi = expected_range[s]
        ok = lo <= a <= hi
        all_ok = all_ok and ok
        sign = "✓" if ok else "✗"
        print(f"  {sign} 제{s}교시: 추출 {a}문제 (기대 {lo}~{hi})")

    print("\n[연속성 검사]")
    cont_ok = True
    for s in [1, 2, 3, 4]:
        nums = sorted(set(n for ss, n, *_ in q_list if ss == s))
        if not nums:
            continue
        gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        ok = not gaps
        cont_ok = cont_ok and ok
        sign = "✓" if ok else "✗"
        gap_str = f" — 누락: {gaps}" if gaps else ""
        print(f"  {sign} 제{s}교시: 번호 {nums}{gap_str}")

    overall_ok = all_ok and cont_ok
    print("\n" + ("✅ 진단 통과" if overall_ok else "⚠️  검증 실패"))

    if split_dir is not None:
        round_id = derive_round_id(pdf_path)
        target = split_dir / round_id
        print(f"\n[분할 실행] → {target}")
        nq = write_split_pdfs(doc, pages, q_list, round_id, target)
        print(f"  분할 PDF: {nq}건 작성")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    pdf_arg = Path(args[0])
    split_arg: Optional[Path] = None
    if "--split" in args:
        i = args.index("--split")
        if i + 1 >= len(args):
            print("--split 다음에 출력 디렉터리 경로가 필요합니다.")
            sys.exit(2)
        split_arg = Path(args[i + 1])
    sys.exit(diagnose(pdf_arg, split_arg))
