"""
ITPE 모의고사 해설집 PDF 진단 스크립트 (dry-run, 읽기 전용).

목적: (교시, 문제번호, 토픽) 3-튜플을 결정적으로 추출 가능한지,
      그리고 어디서 패턴이 깨지는지 fail-loud 로 보고.

분할은 수행하지 않는다. 보고서만 출력한다.

사용:
    python scripts/diagnose_itpe_mock.py <pdf_path>                       # fitz 엔진(기본)
    python scripts/diagnose_itpe_mock.py <pdf_path> --engine kordoc       # 토픽 제목 가독성 보강
    python scripts/diagnose_itpe_mock.py <pdf_path> --split <out_dir>     # 진단 + 분할

엔진 선택:
- fitz   (기본): page.get_text() + 슬라이딩 윈도우 정규식. ITPE 전 회차에서 안정 통과.
- kordoc       : 분류는 fitz 그대로 + kordoc 시험지 페이지에서 master topic 추출하여
                 잘려있던 토픽 제목을 풀 텍스트로 보강 (정관/컴응 트랙 인식).
                 회귀 위험 거의 없음 (페이지 분류·번호·범위 변경 없음, 제목만 enriched).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


# 매 페이지 상단에 반복되는 헤더 패턴
HEADER_BRAND_RE = re.compile(r"제\s*\d+\s*회\s*ITPE\s*실전\s*명품\s*모의고사\s*해설집")
HEADER_COPY_RE = re.compile(r"Copyright\s*ⓒ.*ITPE")
# 페이지 번호 자리: 1~3자리 숫자, 또는 'PAGE' / 'PAGE 4' 같은 placeholder 미치환 케이스
PAGE_NUM_RE = re.compile(r"^(?:PAGE(?:\s+\d{1,3})?|\d{1,3})$")

# 교시 표지: "제 N 교시(시험시간: 100 분)"
SESSION_HDR_RE = re.compile(r"제\s*([1-4])\s*교시\s*\(\s*시험시간")

# 컴응 선택문제 안내 페이지
SELECT_HDR_RE = re.compile(r"\[\s*컴퓨터시스템응용기술사\s*선택문제\s*\]")

# 문제번호 단독 라인 (1~2자리)
Q_NUM_RE = re.compile(r"^\d{1,2}$")
# 앵커 키워드
PROBLEM_ANCHOR = "문제"
DOMAIN_ANCHOR = "도메인"


@dataclass
class PageInfo:
    page_idx: int  # 0-based
    raw_lines: list[str]
    body_lines: list[str]  # 헤더 제거 후
    kind: str = "UNKNOWN"  # COVER | SESSION_HDR | SELECT_HDR | Q_START | Q_BODY | UNKNOWN
    session: Optional[int] = None  # 1~4 (해당 페이지가 속한 교시)
    q_num: Optional[int] = None  # Q_START 일 때만
    q_topic: Optional[str] = None  # Q_START 일 때만
    q_category: Optional[str] = None  # Q_START 일 때만
    notes: list[str] = field(default_factory=list)


def strip_header(lines: list[str]) -> list[str]:
    """매 페이지 상단의 브랜드/카피라이트/페이지번호 라인을 제거한 본문 라인 리턴.

    회차마다 헤더 구성이 다름:
    - 표준: [brand, copyright, pagenum]
    - 일부 회차/페이지: [brand, pagenum] (copyright 누락)
    - 일부 페이지: pagenum 자리에 'PAGE' 또는 'PAGE 4' 같은 placeholder
    """
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    if not cleaned:
        return []
    # brand 위치 (보통 라인 0)
    if not HEADER_BRAND_RE.search(cleaned[0]):
        return cleaned
    out_start = 1
    # copyright (옵션) — brand 직후 1라인
    if out_start < len(cleaned) and HEADER_COPY_RE.search(cleaned[out_start]):
        out_start += 1
    # pagenum (옵션) — brand/copyright 직후 1라인
    if out_start < len(cleaned) and PAGE_NUM_RE.match(cleaned[out_start]):
        out_start += 1
    return cleaned[out_start:]


def classify_page(body: list[str]) -> tuple[str, dict]:
    """본문 첫 7라인을 보고 페이지 종류 분류."""
    if not body:
        return "EMPTY", {}

    head = body[:8]

    # 교시 표지
    for ln in head:
        m = SESSION_HDR_RE.search(ln)
        if m:
            return "SESSION_HDR", {"session": int(m.group(1))}

    # 선택문제 안내
    for ln in head:
        if SELECT_HDR_RE.search(ln):
            # 동시에 13./6. 같은 인라인 시험지 문제도 있을 수 있으니
            inline_q = None
            for ln2 in head:
                m = re.match(r"^(\d{1,2})\.\s*(.+)$", ln2)
                if m:
                    inline_q = (int(m.group(1)), m.group(2).strip())
                    break
            return "SELECT_HDR", {"inline_q": inline_q}

    # Q_START — 본문 내 슬라이딩 윈도우로 [숫자 / 카테고리 / '문제' / 토픽 / '도메인'] 패턴 탐색.
    # 같은 페이지에 이전 해설 끝 + 다음 문제 시작이 함께 들어오는 경우(p.13 = `... RPA / 05 / NoCode와 RPA / 문제 / ...`)도 잡음.
    sliding_start = None
    for s in range(min(len(body), 30)):
        if Q_NUM_RE.match(body[s]):
            tail = body[s:]
            for k in range(2, min(len(tail), 6)):
                if tail[k] == PROBLEM_ANCHOR:
                    sliding_start = s
                    break
            if sliding_start is not None:
                break
    if sliding_start is not None:
        head = body[sliding_start:]
    if Q_NUM_RE.match(head[0]):
        # '문제' 앵커는 보통 idx 2 또는 3 (카테고리 길이에 따라)
        problem_idx = None
        for k in range(2, min(len(head), 6)):
            if head[k] == PROBLEM_ANCHOR:
                problem_idx = k
                break
        if problem_idx is None:
            return "Q_BODY", {}

        # '도메인' 앵커: 본문 어디든 (서술형 문제는 토픽이 매우 길 수 있음)
        domain_idx = None
        for k in range(problem_idx + 1, len(body)):
            if body[k] == DOMAIN_ANCHOR:
                domain_idx = k
                break

        category = " ".join(head[1:problem_idx]).strip()
        if domain_idx is not None:
            topic_lines = body[problem_idx + 1 : domain_idx]
            # 토픽 정규화: 짧으면(서답형 ≤ 2줄) 합치고, 서술형(긴 본문)은 첫 라인만.
            if len(topic_lines) <= 2:
                topic = " ".join(topic_lines).strip()
            else:
                topic = topic_lines[0].strip()
            weak = (problem_idx != 2) or (domain_idx != problem_idx + 2)
            return "Q_START", {
                "q_num": int(head[0]),
                "q_category": category,
                "q_topic": topic,
                "weak": weak,
            }
        # 도메인 앵커 못 찾음 — fail-loud 후보
        return "Q_START_PARTIAL", {
            "q_num": int(head[0]),
            "q_category": category,
            "q_topic": head[problem_idx + 1] if problem_idx + 1 < len(head) else "",
        }

    return "Q_BODY", {}


_ALLOWED_FILENAME_RE = re.compile(
    r"[^ -~가-힯ㄱ-ㆎ‐-⁯ -ɏ]+"
)


def sanitize_filename(s: str, max_len: int = 80, max_bytes: int = 180) -> str:
    """파일명 안전 정규화 (화이트리스트 + char/byte 한도)."""
    import unicodedata
    s = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", " ", s)
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
    """파일명에서 회차 식별자 추출. '모의_ITPE41-2603-합.pdf' → 'ITPE41-2603'."""
    stem = pdf_path.stem
    stem = re.sub(r"^모의_", "", stem)
    stem = re.sub(r"[-_]?합$", "", stem)
    return stem


def write_split_pdfs(
    src_doc: fitz.Document,
    pages: list,
    q_list: list,
    round_id: str,
    out_dir: Path,
) -> tuple[int, int]:
    """진단 결과를 바탕으로 분할 PDF를 out_dir에 저장."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written_q = 0
    written_exam = 0

    # 같은 (M, Q번호) 충돌 시 토픽이 같으면 a/b 접미사
    name_seen: dict[str, int] = {}

    # q_list 항목에 category 도 매핑 (page_idx 기준)
    cat_by_pidx = {p.page_idx: (p.q_category or "") for p in pages if p.kind in ("Q_START", "Q_START_PARTIAL")}
    topic_by_pidx = {p.page_idx: (p.q_topic or "") for p in pages if p.kind in ("Q_START", "Q_START_PARTIAL")}

    from kordoc_adapter import short_topic_label  # noqa: E402
    for sess, num, _topic, ps, pe in q_list:
        cat = cat_by_pidx.get(ps, "")
        topic_full = topic_by_pidx.get(ps, "")
        # 우선순위 (파일명 가독성):
        #   1) cat (시험지 카테고리 — 가장 짧고 식별성 ↑)
        #   2) short_topic_label(_topic) — kordoc enriched 토픽의 축약본
        #   3) short_topic_label(topic_full) — fitz 토픽의 축약본
        if cat and 4 <= len(cat) <= 60:
            label = cat
        elif _topic:
            label = short_topic_label(_topic, max_len=60)
        elif topic_full:
            label = short_topic_label(topic_full, max_len=60)
        else:
            label = f"Q{num:02d}"
        label_safe = sanitize_filename(label, max_len=70)
        base = f"{round_id}_M{sess}_Q{num:02d}_{label_safe}"
        if base in name_seen:
            name_seen[base] += 1
            base = f"{base}_{chr(ord('a') + name_seen[base] - 1)}"
        else:
            name_seen[base] = 1
        out_path = out_dir / f"{base}.pdf"
        new_doc = fitz.open()
        new_doc.insert_pdf(src_doc, from_page=ps, to_page=pe)
        # PDF 메타데이터: title/subject/keywords — Obsidian/Notion 검색성 ↑
        new_doc.set_metadata({
            "title": (_topic or topic_full or f"Q{num:02d}").strip()[:200],
            "subject": f"{round_id} 제{sess}교시 Q{num:02d}"
                       + (f" — {cat}" if cat else ""),
            "keywords": ", ".join(filter(None, [
                round_id, f"M{sess}", f"Q{num:02d}",
                cat, (_topic or topic_full or "").strip()[:120],
            ])),
            "author": "ITPE Topic Splitter (ITPE mock)",
        })
        new_doc.save(out_path)
        new_doc.close()
        written_q += 1

    # 시험지: SESSION_HDR + 후속 EXAM_PAPER + SELECT_HDR 페이지를 교시별로 묶음
    by_session_exam: dict[int, list[int]] = {}
    for p in pages:
        if p.kind in ("SESSION_HDR", "SELECT_HDR", "EXAM_PAPER") and p.session:
            by_session_exam.setdefault(p.session, []).append(p.page_idx)
    for sess, idxs in by_session_exam.items():
        if not idxs:
            continue
        idxs.sort()
        # 연속 구간으로 묶기
        groups: list[list[int]] = [[idxs[0]]]
        for k in idxs[1:]:
            if k == groups[-1][-1] + 1:
                groups[-1].append(k)
            else:
                groups.append([k])
        for gi, grp in enumerate(groups):
            suffix = "" if len(groups) == 1 else f"_{gi+1}"
            out_path = out_dir / f"{round_id}_M{sess}_시험지{suffix}.pdf"
            new_doc = fitz.open()
            new_doc.insert_pdf(src_doc, from_page=grp[0], to_page=grp[-1])
            new_doc.save(out_path)
            new_doc.close()
            written_exam += 1

    return written_q, written_exam


_ACTUAL_EXAM_RE = re.compile(r"기출\s*(문제|풀이|해설)|국가기술자격기술사시험문제")
_ITPE_MOCK_RE = re.compile(r"ITPE\s*실전\s*명품\s*모의고사")


def is_itpe_mock_pdf(pdf_path: Path) -> bool:
    """ITPE 모의고사 해설집 판정.

    분류 우선순위 (LR-007 시험 형식 토큰):
      1. 첫 5p 본문에 본시험 키워드('기출문제'/'기출풀이'/'기출해설'/'국가기술자격')
         있으면 즉시 False — 본시험은 모의고사 파서 적용 금지
      2. 본문에 'ITPE 실전 명품 모의고사' 있으면 True (시험 브랜드 + 종별)
      3. 파일명에 ITPE/itpe 가 있어도 본시험 키워드와 함께면 False
    """
    try:
        doc = fitz.open(pdf_path)
        head_text = ""
        for i in range(min(doc.page_count, 5)):
            head_text += doc.load_page(i).get_text() + "\n"
        doc.close()
    except Exception:
        head_text = ""

    if _ACTUAL_EXAM_RE.search(head_text):
        return False
    if _ITPE_MOCK_RE.search(head_text):
        return True
    # 본문 시그널 없을 때 — 파일명 fallback (단 본시험 키워드 부재 시)
    if re.search(r"ITPE", pdf_path.name, re.IGNORECASE):
        return True
    return False


def split_itpe_mock(pdf_path: Path, out_dir: Path,
                    *, engine: str = "fitz") -> dict:
    """ITPE 모의고사 PDF를 결정적 파서로 분할.

    engine:
      - "fitz" (기본): page.get_text() 슬라이딩 윈도우 정규식
      - "kordoc"     : fitz 분류 + kordoc 시험지 master 토픽 enrich (가독성 ↑)

    Returns:
        {
            "ok": bool,
            "round_id": str,
            "files": [{"path": str, "filename": str}, ...],
            "warnings": list[str],
            "summary": str,
        }
    """
    warnings: list[str] = []
    if not pdf_path.exists():
        return {"ok": False, "round_id": "", "files": [], "warnings": [f"파일 없음: {pdf_path}"], "summary": ""}

    doc = fitz.open(pdf_path)
    pages, q_list = analyze_pages(doc)
    if engine == "kordoc":
        try:
            pages, q_list, _stats = enrich_topics_with_kordoc(pdf_path, pages, q_list)
        except Exception as e:
            warnings.append(f"kordoc enrich 실패 → fitz 결과 사용: {str(e)[:120]}")

    # 최소 합리성 검사
    if not q_list:
        return {
            "ok": False, "round_id": "", "files": [], "warnings": ["문제 검출 실패 — ITPE 모의고사 포맷이 아닐 수 있습니다."], "summary": "",
        }

    # 번호 연속성 검사 — 누락된 번호는 경고로만 보고 (원본 PDF 결함 가능)
    for s in [1, 2, 3, 4]:
        nums = sorted(set(n for ss, n, *_ in q_list if ss == s))
        if not nums:
            continue
        gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        if gaps:
            warnings.append(f"제{s}교시 번호 누락: {gaps} (원본 PDF에 해설이 없을 수 있음)")

    round_id = derive_round_id(pdf_path)
    target = out_dir / round_id
    nq, ne = write_split_pdfs(doc, pages, q_list, round_id, target)

    files = []
    for p in sorted(target.iterdir()):
        if p.is_file() and p.suffix == ".pdf":
            files.append({"path": str(p), "filename": p.name})

    summary = f"{round_id}: {nq}문제 + {ne}시험지 = {len(files)}건"
    topics = [
        {
            "session": sess,
            "num": num,
            "title": topic,
            "page_start": ps + 1,
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


def analyze_pages(doc: fitz.Document) -> tuple[list, list]:
    """진단 핵심 로직만 추출 (출력 없이) — diagnose()와 split_itpe_mock()이 공유."""
    pages: list[PageInfo] = []
    current_session: Optional[int] = None
    seen_first_q_in_session: dict[int, bool] = {}
    last_q_num_in_session: Optional[int] = None

    for i in range(doc.page_count):
        raw = doc.load_page(i).get_text()
        raw_lines = raw.split("\n")
        body = strip_header(raw_lines)
        kind, meta = classify_page(body)

        if kind == "SESSION_HDR":
            current_session = meta["session"]
            seen_first_q_in_session[current_session] = False
            last_q_num_in_session = None
        elif kind in ("Q_START", "Q_START_PARTIAL"):
            q_num = meta["q_num"]
            if (
                current_session is not None
                and last_q_num_in_session is not None
                and q_num == 1
                and last_q_num_in_session > 1
            ):
                current_session = min(current_session + 1, 4)
                seen_first_q_in_session[current_session] = True
            elif current_session is None:
                current_session = 1
                seen_first_q_in_session[1] = True
            else:
                seen_first_q_in_session[current_session] = True
            last_q_num_in_session = q_num
        elif kind == "Q_BODY" and current_session and not seen_first_q_in_session.get(current_session, False):
            kind = "EXAM_PAPER"
        elif kind == "Q_BODY" and current_session is None:
            kind = "COVER"

        info = PageInfo(page_idx=i, raw_lines=raw_lines, body_lines=body, kind=kind, session=current_session)
        if kind == "Q_START":
            info.q_num = meta["q_num"]
            info.q_topic = meta["q_topic"]
            info.q_category = meta["q_category"]
            if meta.get("weak"):
                info.notes.append("topic-weak-match")
        elif kind == "Q_START_PARTIAL":
            info.q_num = meta["q_num"]
            info.q_topic = meta.get("q_topic", "")
            info.q_category = meta.get("q_category", "")
            info.notes.append("partial-template")
        elif kind == "SELECT_HDR" and meta.get("inline_q"):
            num, topic = meta["inline_q"]
            info.notes.append(f"inline-Q{num}: {topic[:40]}")
        pages.append(info)

    q_starts = [p for p in pages if p.kind in ("Q_START", "Q_START_PARTIAL")]
    q_list: list[tuple[int, int, str, int, int]] = []
    for idx, qs in enumerate(q_starts):
        next_start = q_starts[idx + 1].page_idx if idx + 1 < len(q_starts) else doc.page_count
        boundary = next_start
        for p in pages[qs.page_idx + 1 : next_start]:
            if p.kind in ("SESSION_HDR", "SELECT_HDR"):
                boundary = p.page_idx
                break
        q_list.append((qs.session or 0, qs.q_num or 0, qs.q_topic or "", qs.page_idx, boundary - 1))

    return pages, q_list


def enrich_topics_with_kordoc(
    pdf_path: Path, pages: list, q_list: list,
) -> tuple[list, list, dict]:
    """kordoc 시험지 페이지에서 토픽 master 추출 후 q_list 의 제목을 풀 텍스트로 보강.

    페이지 분류·번호·범위는 변경하지 않음 (fitz 결정 그대로 유지).

    매핑 규칙:
        - q_num 1-12 (또는 1-5) → master['common'][q_num]
        - q_num 13 (또는 6) → 정관/컴응 트랙은 페이지 순서로 결정:
            교시 내 같은 q_num 이 두 번 나오면 첫 번째=정관, 두 번째=컴응
        - master 가 없거나 짧으면 fitz 결과 유지

    Returns:
        (pages, enriched_q_list, stats)
        stats = {'enriched': N, 'unchanged': M, 'master_pages': K, ...}
    """
    from kordoc_adapter import (  # noqa: E402
        parse_kordoc_pages,
        extract_itpe_master_topics,
        merge_itpe_masters,
    )

    # 시험지 페이지 인덱스 (fitz 분류 기준)
    exam_paper_pages = [p.page_idx + 1 for p in pages
                         if p.kind in ("SESSION_HDR", "SELECT_HDR", "EXAM_PAPER")]
    if not exam_paper_pages:
        return pages, q_list, {"enriched": 0, "reason": "시험지 페이지 없음"}

    try:
        pages_blocks, _ = parse_kordoc_pages(str(pdf_path), verbose=True)
    except Exception as e:
        return pages, q_list, {"enriched": 0, "reason": f"kordoc 호출 실패: {e}"}

    # 교시별 master 누적
    masters_by_session: dict[int, dict[str, dict[int, str]]] = {}
    for p in pages:
        if p.kind not in ("SESSION_HDR", "SELECT_HDR", "EXAM_PAPER"):
            continue
        if p.session is None:
            continue
        m = extract_itpe_master_topics(pages_blocks.get(p.page_idx + 1, []))
        if p.session not in masters_by_session:
            masters_by_session[p.session] = m
        else:
            masters_by_session[p.session] = merge_itpe_masters(
                [masters_by_session[p.session], m])

    # enrich 후보 sanity check — table 셀 분산으로 인한 garbled master 거부.
    # ITPE27 같은 회차에서 시험지 table 셀이 어절 단위로 흩어져 cells 가
    # join 되면 다른 q_num 토픽 라벨이 함께 끌려옴 ("AI비서 3. 개인정보..." 같은 형태).
    def _is_clean_topic(t: str) -> bool:
        if not t or len(t) > 200:
            return False
        # master 에서 prefix "N. " 가 이미 제거된 토픽 본문에 또 다른 "N. <한글/영문>"
        # 라벨이 등장하면 table 셀 분산으로 다른 q_num 의 토픽이 섞인 노이즈
        if re.search(r"\b\d{1,2}\.\s+[가-힣A-Za-z(]", t):
            return False
        return True

    # q_list enrich: 같은 (교시, q_num) 이 두 번 등장하는 케이스(정관/컴응)
    # 처리 — 등장 순서대로 'jeonggwan' → 'compeung'
    enriched: list = []
    enrich_count = 0
    seen_session_qnum: dict[tuple[int, int], int] = {}
    for sess, num, topic, ps, pe in q_list:
        master = masters_by_session.get(sess, {})
        # 트랙 결정
        key = (sess, num)
        seen_session_qnum[key] = seen_session_qnum.get(key, 0) + 1
        order = seen_session_qnum[key]
        # common 우선, 같은 q_num 두 번째면 jeonggwan→compeung
        new_title = topic
        if order == 1:
            cand = master.get("common", {}).get(num) or master.get("jeonggwan", {}).get(num)
            if (cand and len(cand) > len(topic) + 4
                    and _is_clean_topic(cand)):
                new_title = cand
                enrich_count += 1
        else:  # 두 번째 등장 → 컴응
            cand = master.get("compeung", {}).get(num)
            if (cand and len(cand) > len(topic) + 4
                    and _is_clean_topic(cand)):
                new_title = cand
                enrich_count += 1
        enriched.append((sess, num, new_title, ps, pe))

    stats = {
        "enriched": enrich_count,
        "unchanged": len(q_list) - enrich_count,
        "master_sessions": list(sorted(masters_by_session.keys())),
        "master_pages": len(exam_paper_pages),
    }
    return pages, enriched, stats


def diagnose(pdf_path: Path, split_dir: Optional[Path] = None,
             *, engine: str = "fitz") -> int:
    if not pdf_path.exists():
        print(f"❌ 파일이 없습니다: {pdf_path}")
        return 2

    print(f"=== {pdf_path.name} (engine={engine}) ===")
    doc = fitz.open(pdf_path)
    print(f"총 페이지: {doc.page_count}\n")

    pages, q_list = analyze_pages(doc)

    if engine == "kordoc":
        try:
            pages, q_list, stats = enrich_topics_with_kordoc(pdf_path, pages, q_list)
            print(f"[kordoc enrich] 토픽 제목 풀텍스트 보강: "
                  f"{stats['enriched']}건 갱신, {stats.get('unchanged', 0)}건 유지 "
                  f"(master 교시 {stats.get('master_sessions')})\n")
        except Exception as e:
            print(f"⚠️  kordoc enrich 실패 — fitz 결과 그대로 사용: {str(e)[:200]}\n")

    # ---- 출력: 페이지 분류 ----
    print("[페이지 분류]")
    for p in pages:
        tag = p.kind
        sess = f"M{p.session}" if p.session else "  "
        if p.kind == "Q_START":
            extra = f"Q{p.q_num:02d} | {p.q_category[:20]:20} | {p.q_topic[:50]}"
        elif p.kind == "Q_START_PARTIAL":
            extra = f"⚠ Q{p.q_num:02d} | {p.q_category[:20]:20} | {p.q_topic[:50]}"
        elif p.kind == "SESSION_HDR":
            extra = f"제 {p.session} 교시 표지"
        elif p.kind == "SELECT_HDR":
            extra = "[컴응 선택문제]" + (
                f" | {p.notes[0][len('inline-'):]}" if p.notes and p.notes[0].startswith("inline-") else ""
            )
        else:
            extra = ""
        note = ""
        if p.notes and not p.notes[0].startswith("inline-"):
            note = "  // " + ", ".join(p.notes)
        print(f"  p.{p.page_idx + 1:>3} {sess} {tag:18} {extra}{note}")

    # ---- 문제 요약 ----
    print("\n[문제 요약 — (교시, 번호, 토픽)]")
    for sess, num, topic, ps, pe in q_list:
        flag = ""
        print(f"  M{sess} Q{num:02d} | p.{ps+1:>3}-{pe+1:<3} | {topic}{flag}")

    # ---- 자기 검증 ----
    print("\n[자기 검증]")
    # 회차별 변동 허용. 1교시는 13~14(컴응 13번 해설 유무), 2-4교시는 6~7(컴응 해설 유무)
    expected_range = {1: (13, 14), 2: (6, 7), 3: (6, 7), 4: (6, 7)}
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

    # 같은 (교시, 번호) 중복 — 정관/컴응 분리 안 한 상태에서 자연스러운 케이스
    from collections import Counter
    dup = Counter((s, n) for s, n, *_ in q_list)
    dups = [(k, v) for k, v in dup.items() if v > 1]
    if dups:
        print(f"\n  ℹ️  같은 (교시, 번호) 등장: {len(dups)}건 (정관/컴응 분리되지 않음)")
        for (s, n), v in dups:
            topics = [t for ss, nn, t, *_ in q_list if ss == s and nn == n]
            print(f"     M{s} Q{n:02d} ×{v}: {topics}")

    # 페이지 누락/중복 검사
    covered = set()
    for _, _, _, ps, pe in q_list:
        for k in range(ps, pe + 1):
            covered.add(k)
    cover_kind_pages = {
        p.page_idx
        for p in pages
        if p.kind in ("SESSION_HDR", "SELECT_HDR", "COVER", "EXAM_PAPER", "EMPTY")
    }
    missing = []
    for i in range(doc.page_count):
        if i in covered or i in cover_kind_pages:
            continue
        # p.1 (책 표지) 같은 케이스도 여기 들어옴
        missing.append(i)
    if missing:
        print(f"\n  ⚠️  어느 청크에도 안 들어간 페이지: {[m+1 for m in missing]}")

    # 단조성 검사 — 한 교시 안에서 번호가 단조 증가하는가
    print("\n[교시 내 번호 단조성]")
    for s in [1, 2, 3, 4]:
        nums = [n for ss, n, *_ in q_list if ss == s]
        ok = nums == sorted(nums)
        sign = "✓" if ok else "✗"
        print(f"  {sign} 제{s}교시 번호 시퀀스: {nums}")

    # 부분 템플릿 매칭 보고
    partial = [p for p in pages if p.kind == "Q_START_PARTIAL"]
    if partial:
        print(f"\n  ⚠️  템플릿 부분 매칭(폴백) 페이지: {[p.page_idx+1 for p in partial]}")
        for p in partial:
            print(f"     p.{p.page_idx+1} body[:7]={p.body_lines[:7]}")

    monotonic_ok = all(
        sorted([n for ss, n, *_ in q_list if ss == s]) == [n for ss, n, *_ in q_list if ss == s]
        for s in [1, 2, 3, 4]
    )
    # 연속성 검사: 각 교시 정관 트랙은 1..N (N=12 또는 13 또는 5 또는 6) 이 빠짐 없이 등장해야 함.
    continuity_ok = True
    print("\n[교시 내 번호 연속성]")
    for s in [1, 2, 3, 4]:
        nums = sorted(set(n for ss, n, *_ in q_list if ss == s))
        if not nums:
            continue
        expected_seq = list(range(1, max(nums) + 1))
        gaps = [n for n in expected_seq if n not in nums]
        ok = not gaps
        continuity_ok = continuity_ok and ok
        sign = "✓" if ok else "✗"
        gap_str = f" — 누락: {gaps}" if gaps else ""
        print(f"  {sign} 제{s}교시: 등장 번호 {nums}{gap_str}")
    overall_ok = all_ok and not partial and not missing and monotonic_ok and continuity_ok
    print("\n" + ("✅ 진단 통과" if overall_ok else "⚠️  검증 실패 — 위 항목 확인 필요"))

    if split_dir is not None:
        round_id = derive_round_id(pdf_path)
        target = split_dir / round_id
        print(f"\n[분할 실행] → {target}")
        nq, ne = write_split_pdfs(doc, pages, q_list, round_id, target)
        print(f"  문제 PDF: {nq}건, 시험지 PDF: {ne}건 작성")

    return 0 if overall_ok else 1


def compare_engines(pdf_path: Path) -> int:
    """fitz vs kordoc(enriched) q_list 비교."""
    from kordoc_adapter import print_q_list_diff  # noqa: E402
    if not pdf_path.exists():
        print(f"❌ 파일이 없습니다: {pdf_path}")
        return 2
    print(f"=== {pdf_path.name} (compare fitz↔kordoc) ===")
    doc = fitz.open(pdf_path)
    print(f"총 페이지: {doc.page_count}\n")
    pages_fitz, q_fitz = analyze_pages(doc)
    try:
        _, q_kordoc, _ = enrich_topics_with_kordoc(pdf_path, pages_fitz, list(q_fitz))
    except Exception as e:
        print(f"⚠️  kordoc enrich 실패: {str(e)[:200]}")
        return 1
    print_q_list_diff(q_fitz, q_kordoc)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    pdf_arg = Path(args[0])
    if "--compare" in args:
        sys.exit(compare_engines(pdf_arg))
    split_arg: Optional[Path] = None
    if "--split" in args:
        i = args.index("--split")
        if i + 1 >= len(args):
            print("--split 다음에 출력 디렉터리 경로가 필요합니다.")
            sys.exit(2)
        split_arg = Path(args[i + 1])
    engine_arg = "fitz"
    if "--engine" in args:
        i = args.index("--engine")
        if i + 1 >= len(args):
            print("--engine 다음에 엔진 이름(fitz|kordoc)이 필요합니다.")
            sys.exit(2)
        engine_arg = args[i + 1]
        if engine_arg not in ("fitz", "kordoc"):
            print(f"알 수 없는 엔진: {engine_arg!r} (fitz|kordoc 중 선택)")
            sys.exit(2)
    sys.exit(diagnose(pdf_arg, split_arg, engine=engine_arg))
