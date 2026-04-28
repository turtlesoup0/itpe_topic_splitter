"""
kordoc CLI 어댑터 — 모의고사 진단/분할 스크립트가 공유하는 유틸.

split_odl.py 의 parse_kordoc()이 별도로 v2 boundary 탐지용 elements 를
반환하는 반면, 이 어댑터는 **페이지 단위 IRBlock 그룹**을 노출해
diagnose_*_mock.py 가 페이지별 분류 로직(SESSION_PAPER / Q_START / Q_BODY 등)을
font_size + type 신호로 작성할 수 있게 합니다.

설계 원칙:
- split_odl.parse_kordoc 와 캐시 키 분리(스키마 다름) — 서로 영향 없음.
- kordoc CLI 자동 탐지: $KORDOC_CLI → 알려진 npx 캐시 경로 → `npx -y kordoc`.
- 호출자에게 fitz/PyMuPDF 의존을 강제하지 않음 (이 모듈은 fitz import 안 함).
- 실패 시 `RuntimeError` 로 명시 — 호출자가 fitz 폴백을 결정하도록.

공개 API:
    parse_kordoc_pages(pdf_path, *, no_header_footer=True) -> tuple[dict[int, list[Block]], int]
    Block (TypedDict)
    KORDOC_HEADER_NOISE_RE     KPC/ITPE 모두 등장하는 페이지 헤더/푸터 패턴
    is_header_noise(text)      편의 함수
    block_text(block)          정규화된 텍스트 (공백 변형 흡수)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, TypedDict


# ─── kordoc CLI 위치 자동 탐지 ────────────────────────────────────────

_KORDOC_CLI_FALLBACKS = [
    # split_odl.py 가 기본값으로 사용하는 경로 (개발용)
    "/tmp/kordoc/dist/cli.js",
    # npx --no-install 캐시
    "/Users/turtlesoup0-macmini/.npm/_npx/2a9cbde48d0ad81d/node_modules/kordoc/dist/cli.js",
]


def _resolve_kordoc_cli() -> tuple[list[str], bool]:
    """kordoc CLI 실행 명령을 반환.

    Returns:
        (cmd_prefix, is_npx_fallback)
        cmd_prefix 는 `[node, cli.js]` 형태 또는 `[npx, -y, kordoc]`.
    """
    env_path = os.environ.get("KORDOC_CLI")
    if env_path and os.path.isfile(env_path):
        return ["node", env_path], False
    for p in _KORDOC_CLI_FALLBACKS:
        if os.path.isfile(p):
            return ["node", p], False
    # 최후 수단: npx (네트워크 가능 시 자동 다운로드)
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "kordoc"], True
    raise RuntimeError(
        "kordoc CLI 를 찾지 못했습니다. $KORDOC_CLI 환경변수에 cli.js 경로를 "
        "지정하거나 `npm i -g kordoc` 후 재시도하세요."
    )


# ─── 디스크 캐시 ──────────────────────────────────────────────────────
# split_odl 의 parse_kordoc 캐시(pages-flattened elements)와 스키마가 다르므로
# 별도 디렉터리/스키마 키를 사용해 충돌을 방지합니다.

_CACHE_SCHEMA = "pages-v1"


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "itpe-splitter" / "kordoc-pages"


def _pdf_sha256(pdf_path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(pdf_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _cache_path(pdf_hash: str, no_header_footer: bool) -> Path:
    flag = "nhf" if no_header_footer else "raw"
    return _cache_dir() / f"{pdf_hash}_{_CACHE_SCHEMA}_{flag}.json"


def _cache_load(pdf_path: str, no_header_footer: bool) -> Optional[tuple]:
    h = _pdf_sha256(pdf_path)
    if not h:
        return None
    p = _cache_path(h, no_header_footer)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        pages_blocks = data.get("pages_blocks")
        total = data.get("total_pages")
        if not isinstance(pages_blocks, dict) or not isinstance(total, int):
            return None
        # JSON 키는 문자열이라 int 로 복원
        normalized = {int(k): v for k, v in pages_blocks.items()}
        return normalized, total
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _cache_save(pdf_path: str, no_header_footer: bool,
                pages_blocks: dict, total_pages: int) -> None:
    h = _pdf_sha256(pdf_path)
    if not h:
        return
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = _cache_path(h, no_header_footer)
        p.write_text(
            json.dumps({"pages_blocks": pages_blocks, "total_pages": total_pages},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


# ─── Block 정규화 ────────────────────────────────────────────────────

class Block(TypedDict, total=False):
    type: str        # 'paragraph' | 'heading' | 'list' | 'table' | 'separator' | 'image'
    text: str
    page: int        # 1-indexed
    font_size: float
    heading_level: int
    is_table: bool


def block_text(b: Block) -> str:
    """Block 의 정규화된 텍스트 (양 끝 공백 정리)."""
    return (b.get("text") or "").strip()


# ─── 페이지 헤더/푸터 노이즈 ───────────────────────────────────────
# kordoc `--no-header-footer` 가 KPC/ITPE 학원 PDF 의 반복 헤더를
# 완전히는 제거하지 못하므로 별도 후처리를 합니다.
# **슬로건 어절 변형(LR-007)에 강한 본문 앵커 기반이 아니라, 페이지마다 박혀 있는
# 학원 브랜드 고정 텍스트 + Copyright 만 화이트리스트로 제거**합니다.

KORDOC_HEADER_NOISE_PATTERNS = [
    r"^kpc$",                              # 작은 워터마크
    r"^한국생산성본부$",
    r"^ICT의\s*가치를\s*이끄는\s*사람\.?\s*$",
    r"^들!!?$",                            # "사람" 다음 줄로 분리된 슬로건 잔재 (LR-007 케이스)
    r"^!!?$",                              # "들" 까지 떨어져나간 잔재
    r"^사람들!!?$",                         # 변형 1
    r"^이끄는\s*사람\.?$",                  # 슬로건 어절분리 변형
    r"^ICT의?\s*가치를?$",                  # "ICT의 가치를 이끄는" 의 첫 조각
    r"^ICT\s*의?\s*가치.{0,8}$",            # "ICT 의 가치를 이끄는" 변형 (공백/조사 분산)
    r".*이끄는\s*사람.*",                   # "이끄는 사람" 어디든 포함된 짧은 라인
    r"^\s*인공지능\s*$",                    # 헤더 표지 잔재
    r"^\s*누구나\s*ICT\s*전문가가\s*될\s*수\s*있는\s*세상.*",
    r"^Copyright\s*ⓒ.*Korea\s*Productivity\s*Center.*",
    r"^Copyright\s*ⓒ.*ITPE.*",
    r"^제\s*\d+\s*회\s*ITPE\s*실전\s*명품\s*모의고사(\s*해설집)?$",
    r"^KPC\s*기술사\s*IMPACT\s*실전모의고사$",
    r"^127\s*회\s*:?\s*KPC.*",             # p.42 같은 1-line 변형 헤더
    r"^\d+$",                              # 페이지 번호 (paragraph fs=8 단독 숫자)
    r"^,$",                                # 워터마크 단독 콤마
    r"^\.\d+$",                            # ".2" 같은 워터마크 단편
    r"^\d+\s*[,.\s]+\d*$",                 # "16 13 . 5", "34," 등 워터마크 숫자 잔재
    r"^[\s,.]*$",                          # 공백/콤마/마침표만 남은 라인
]

KORDOC_HEADER_NOISE_RE = re.compile(
    "|".join(f"(?:{p})" for p in KORDOC_HEADER_NOISE_PATTERNS),
    re.IGNORECASE,
)


def is_header_noise(text: str) -> bool:
    """학원 PDF 페이지 헤더/푸터/워터마크 잔재 여부."""
    t = (text or "").strip()
    if not t:
        return True
    if KORDOC_HEADER_NOISE_RE.match(t):
        return True
    return False


# ─── 워터마크 (큰 폰트 그래픽 텍스트) 필터 ─────────────────────────────
# KPC PDF 페이지 배경에 그래픽으로 박혀 있는 큰 글자가 kordoc 추출에 등장.
# 예: fs=98 "34,", fs=159 ".2", fs=37 "6. 2".
# 본문 paragraph/heading 의 정상 폰트는 fs<=22 범위. fs>=24 단독은 노이즈.

NOISE_FONT_SIZE_THRESHOLD = 18.0
# 시험 표지 ("제127회 KPC 기술사 IMPACT 실전모의고사" fs=20)는 길이>15 라 통과.
# p.51 등에서 본 워터마크 잔재 ("15. 1 14" fs=21, "34," fs=98)는 길이≤10 + 의미없음 → 제거 대상.
_WATERMARK_LENGTH_LIMIT = 15
_WATERMARK_NUMERIC_ONLY_RE = re.compile(r"^[\s\d.,]+$")  # 숫자/공백/점만 — 명백 노이즈


def is_watermark_block(b: Block) -> bool:
    """배경 그래픽 텍스트(큰 폰트 + 짧은 의미없는 텍스트) 판정.

    중요: 시험 표지("제 N 회 ... 모의고사" fs=20)와 페이지 헤더(fs<18)는 보존.
    """
    fs = b.get("font_size") or 0
    if fs < NOISE_FONT_SIZE_THRESHOLD:
        return False
    text = block_text(b)
    # fs>=18 + 짧은 텍스트 → 워터마크
    if len(text) <= _WATERMARK_LENGTH_LIMIT:
        # 단, "N. 토픽…"으로 시작하는 list 는 정상 토픽 헤더일 수 있음 (fs=13~18)
        # — 그런데 실제 KPC 토픽 헤더는 항상 fs<=13 이므로 fs>=18 이면 워터마크로 분류해도 안전
        return True
    # fs>=18 + 길이>15 + 숫자/공백/점만 → 워터마크
    if _WATERMARK_NUMERIC_ONLY_RE.match(text):
        return True
    return False


# ─── kordoc CLI 호출 + 페이지별 그룹핑 ────────────────────────────────

def parse_kordoc_pages(
    pdf_path: str,
    *,
    no_header_footer: bool = True,
    use_cache: bool = True,
    timeout: int = 180,
    verbose: bool = False,
) -> tuple[dict[int, list[Block]], int]:
    """PDF 를 kordoc CLI 로 파싱해 페이지별 IRBlock 그룹과 총 페이지 수를 반환.

    Returns:
        (pages_blocks, total_pages)
        pages_blocks: dict[1-indexed page → list[Block]]. 없는 페이지는 빈 리스트.
        total_pages : kordoc metadata 의 pageCount.

    Raises:
        RuntimeError: kordoc CLI 미발견, 호출 실패, 또는 JSON 파싱 실패.
    """
    if use_cache:
        cached = _cache_load(pdf_path, no_header_footer)
        if cached is not None:
            if verbose:
                pages_blocks, total = cached
                print(f"  [kordoc-cache] {total}p, "
                      f"{sum(len(v) for v in pages_blocks.values())} blocks 로드")
            return cached

    cmd_prefix, is_npx = _resolve_kordoc_cli()
    cmd = [*cmd_prefix, pdf_path, "--format", "json", "--silent"]
    if no_header_footer:
        cmd.append("--no-header-footer")

    if verbose:
        print(f"  [kordoc-call] {'npx' if is_npx else 'node'} → {Path(pdf_path).name}")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"kordoc CLI 시간 초과 ({timeout}s): {Path(pdf_path).name}") from e

    raw = result.stdout or ""
    json_start = raw.find("{")
    if result.returncode != 0 or json_start < 0:
        stderr = (result.stderr or "")[:300]
        raise RuntimeError(
            f"kordoc CLI 실패 (rc={result.returncode}): {stderr}"
        )

    try:
        data = json.loads(raw[json_start:])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"kordoc JSON 파싱 실패: {e}") from e

    if not data.get("success", True):
        raise RuntimeError(f"kordoc 응답 실패: {data.get('error', '?')}")

    blocks_raw = data.get("blocks", [])
    metadata = data.get("metadata") or {}
    total_pages = metadata.get("pageCount") or data.get("totalPages") or 0
    if not total_pages and blocks_raw:
        total_pages = max((b.get("pageNumber", 0) for b in blocks_raw), default=0)

    pages_blocks: dict[int, list[Block]] = {}
    for raw_b in blocks_raw:
        pg = raw_b.get("pageNumber") or 0
        if pg <= 0:
            continue
        style = raw_b.get("style") or {}
        block: Block = {
            "type": raw_b.get("type", "paragraph"),
            "text": raw_b.get("text") or "",
            "page": pg,
            "font_size": float(style.get("fontSize") or 0),
        }
        if "level" in raw_b:
            try:
                block["heading_level"] = int(raw_b["level"])
            except (TypeError, ValueError):
                pass
        if raw_b.get("type") == "table":
            block["is_table"] = True
            # 테이블 셀 텍스트도 평탄화 (분류용 신호로 활용 가능)
            table = raw_b.get("table") or {}
            cell_texts = []
            for row in table.get("cells") or []:
                for cell in row:
                    ct = (cell.get("text") or "").strip()
                    if ct:
                        cell_texts.append(ct)
            if cell_texts and not block["text"]:
                block["text"] = " | ".join(cell_texts)
        pages_blocks.setdefault(pg, []).append(block)

    if use_cache:
        # JSON 직렬화 위해 키를 문자열로 변환
        serializable = {str(k): v for k, v in pages_blocks.items()}
        _cache_save(pdf_path, no_header_footer, serializable, total_pages)

    return pages_blocks, total_pages


# ─── 본문 블록 추출 (헤더/워터마크 제거) ───────────────────────────────

def filter_body_blocks(
    page_blocks: list[Block],
    *,
    drop_table_marker: bool = True,
) -> list[Block]:
    """페이지 블록 중 본문에 해당하는 것만 남김.

    제거 대상:
      - 페이지 헤더/푸터 노이즈 (학원 브랜드 + Copyright + 페이지 번호)
      - 큰 폰트 워터마크 (fs >= 24 + 짧은 텍스트)
      - 빈 텍스트 블록 (table 자체는 drop_table_marker=False 면 보존)
    """
    body: list[Block] = []
    for b in page_blocks:
        if b.get("type") == "table":
            if not drop_table_marker:
                body.append(b)
            continue
        if is_watermark_block(b):
            continue
        if is_header_noise(block_text(b)):
            continue
        if not block_text(b):
            continue
        body.append(b)
    return body


# ─── KPC 모의고사 페이지 분류 (kordoc 신호 기반) ─────────────────────
# KPC127 분석에서 정리한 시그널:
#   시험지(SESSION_PAPER): list fs≈13 개수 ≥ 4 (전체 문제 목록)
#   Q_START(해설): 본문에 "N. 토픽" 형태의 list/heading fs≈13 단일 출현
#   Q_BODY: 위 두 패턴 부재
#   EMPTY_PAGE: 본문 블록 거의 없음

KPC_TOPIC_LIST_FONT_RANGE = (12.0, 14.5)   # fs≈13 (모의고사 회차별 변형 흡수)
KPC_SESSION_PAPER_MIN_LIST_COUNT = 4
QNUM_TOPIC_RE = re.compile(r"^(\d{1,2})\.\s*(.*)$")
KPC_SELECT_RE = re.compile(r"\[\s*(관리|응용)\s*선택\s*\]")
SESSION_PAPER_INLINE_RE = re.compile(r"제\s*[1-4]\s*교시\s*\(\s*시험시간")
# ★별점 라인 — KPC 모의고사 모든 해설 페이지(단답형/서술형 공통)의 시작 신호.
# 회차/페이지에 따라 "★★★☆☆ (별 5 개 기준)" 또는 "★★★☆☆" 단독 출현 둘 다 가능.
KPC_DIFFICULTY_RE = re.compile(r"^[★☆]{2,5}")


def _is_topic_list_block(b: Block) -> bool:
    """list/heading fs≈13 + "N. ..." 패턴 (KPC 모의고사 토픽 시그니처)."""
    if b.get("type") not in ("list", "heading", "paragraph"):
        return False
    fs = b.get("font_size") or 0
    lo, hi = KPC_TOPIC_LIST_FONT_RANGE
    if not (lo <= fs <= hi):
        return False
    text = block_text(b)
    return bool(QNUM_TOPIC_RE.match(text))


def _has_difficulty_marker(body: list[Block]) -> bool:
    """페이지에 KPC 별점 라인(★★★☆☆ ...) 이 있는지."""
    return any(KPC_DIFFICULTY_RE.match(block_text(b)) for b in body)


def _extract_topic_from_qstart(body: list[Block]) -> tuple[Optional[int], str]:
    """Q_START 페이지에서 (q_num, topic_title) 추출.

    추출 우선순위:
      A) 본문 fs≈13 list/heading 중 "N. ..." 매칭 + 이후 fs≈13 continuation collation
      B) 시험지 master-list 매핑은 호출자(analyze_pages_kordoc) 책임
      C) 매칭 실패 시 (None, "")
    """
    topic_blocks = [b for b in body if _is_topic_list_block(b)]
    if not topic_blocks:
        return None, ""
    first = topic_blocks[0]
    m = QNUM_TOPIC_RE.match(block_text(first))
    if not m:
        return None, ""
    q_num = int(m.group(1))
    base = m.group(2).strip()
    # continuation: first 직후의 fs≈13 heading/paragraph 들
    try:
        idx = body.index(first)
    except ValueError:
        idx = -1
    cont = []
    if idx >= 0:
        lo, hi = KPC_TOPIC_LIST_FONT_RANGE
        for nb in body[idx + 1: idx + 8]:
            if nb.get("type") in ("heading", "paragraph") and lo <= (nb.get("font_size") or 0) <= hi:
                nt = block_text(nb)
                if QNUM_TOPIC_RE.match(nt):
                    break
                cont.append(nt)
            else:
                # ★별점 또는 다른 fs 만나면 중단
                break
    title = (base + " " + " ".join(cont)).strip()
    return q_num, title


def kpc_classify_page(page_blocks: list[Block]) -> tuple[str, dict]:
    """kordoc IRBlock 기반 KPC 모의고사 페이지 분류.

    fitz 버전(diagnose_kpc_mock.classify_page)과 동일한 (kind, meta) 시그니처를
    돌려줘서 호출 측 로직을 그대로 재사용할 수 있게 합니다.

    분류 우선순위:
      1) SESSION_PAPER — '제 N 교시' / '[관리|응용 선택]' / topic-list ≥4 개
      2) Q_START (강) — topic-list ≥1 (q_num/title 모두 추출)
      3) Q_START (약) — ★별점 라인 존재하지만 topic-list 없음
                       (단답형이거나 토픽 제목이 워터마크에 묻힌 경우 — q_num=None)
      4) EMPTY_PAGE — 본문 블록 ≤ 1개
      5) Q_BODY — 그 외
    """
    body = filter_body_blocks(page_blocks, drop_table_marker=True)

    # 1) SESSION_PAPER 우선 신호 — '제 N 교시' / '[관리|응용 선택]'
    for b in body:
        text = block_text(b)
        if SESSION_PAPER_INLINE_RE.search(text):
            return "SESSION_PAPER", {}
        if KPC_SELECT_RE.search(text):
            return "SESSION_PAPER", {}

    # topic-list 블록 수집
    topic_blocks = [b for b in body if _is_topic_list_block(b)]

    # 2) topic-list ≥ 4 → 시험지
    if len(topic_blocks) >= KPC_SESSION_PAPER_MIN_LIST_COUNT:
        return "SESSION_PAPER", {}

    has_star = _has_difficulty_marker(body)

    # 3) Q_START (강) — topic-list ≥1
    if topic_blocks:
        q_num, title = _extract_topic_from_qstart(body)
        if q_num is not None:
            return "Q_START", {"q_num": q_num, "q_topic": title,
                               "engine": "kordoc", "signal": "topic_list"}

    # 4) Q_START (약) — ★ 별점만 있고 topic-list 없음
    #    단답형/토픽 제목이 워터마크에 묻힌 케이스. q_num 은 호출자가 master-list 또는
    #    단조 증가 가정으로 보정.
    if has_star:
        return "Q_START", {"q_num": None, "q_topic": "",
                           "engine": "kordoc", "signal": "star_only"}

    # 5) EMPTY_PAGE
    if len(body) <= 1:
        return "EMPTY_PAGE", {}

    # 6) Q_BODY
    return "Q_BODY", {}


def extract_topic_from_body_opener(page_blocks: list[Block]) -> str:
    """KPC 해설 페이지 본문 첫 fs≈11 list "1. <text>" 에서 토픽 제목 후보 추출.

    KPC 해설 정형 구조:
        ★★★☆☆ (별점)
        <키워드 라인 paragraph fs=10>
        <키워드 라인>
        list fs=11: "1. <첫 부주제 — 개요/정의/개념>"   ← 이 라인이 토픽의 의미 핵심
        list fs=10: "- ..."
        ...

    시험지 부재 회차(KPC129 등)에서 master 매핑 없이 토픽 제목을 본문에서 직접 복원.

    Returns:
        부주제 1번 텍스트 (예: "도메인 특화 모델, DSLM 의 정의") 또는 빈 문자열.
    """
    body = filter_body_blocks(page_blocks)
    # 검색 범위 15 블록까지 (헤더 잔재 + 별점 + 키워드 paragraphs 통과 후 본문 opener 까지)
    for b in body[:15]:
        if b.get("type") not in ("list", "paragraph"):
            continue
        fs = b.get("font_size") or 0
        if not (10.5 <= fs <= 12.0):
            continue
        text = block_text(b)
        m = re.match(r"^1\.\s+(.+)$", text)
        if not m:
            continue
        t = m.group(1).strip()
        if len(t) < 3:
            continue
        # 너무 일반적인 도입절 제외 ("개요", "정의" 단독)
        if t in ("개요", "정의", "개념"):
            continue
        return t
    return ""


def extract_kpc_session_paper_topics(page_blocks: list[Block]) -> list[tuple[int, str]]:
    """SESSION_PAPER 페이지에서 토픽 마스터 목록 (q_num, title) 추출.

    KPC 시험지 페이지에는 해당 교시의 전체 문제 목록이 list fs≈13 으로 나열됨.
    multi-line 토픽은 직후 heading fs≈13 으로 continuation.
    분리된 PDF 파일(p.1 의 table 안에 있는 경우 등)은 table 텍스트도 함께 검사.
    """
    body = filter_body_blocks(page_blocks, drop_table_marker=False)
    out: list[tuple[int, str]] = []
    lo, hi = KPC_TOPIC_LIST_FONT_RANGE
    i = 0
    while i < len(body):
        b = body[i]
        text = block_text(b)
        # table 내부에 토픽이 들어가는 케이스 (p.1)
        if b.get("type") == "table" and text:
            for line in text.split(" | "):
                ln = line.strip()
                m = QNUM_TOPIC_RE.match(ln)
                if m:
                    out.append((int(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        # list/heading fs≈13 + "N. ..." 매칭
        if (b.get("type") in ("list", "heading", "paragraph")
                and lo <= (b.get("font_size") or 0) <= hi):
            m = QNUM_TOPIC_RE.match(text)
            if m:
                q_num = int(m.group(1))
                title = m.group(2).strip()
                # continuation: 직후 fs≈13 heading 들
                j = i + 1
                while j < len(body):
                    nb = body[j]
                    if (nb.get("type") in ("heading", "paragraph")
                            and lo <= (nb.get("font_size") or 0) <= hi):
                        nt = block_text(nb)
                        if QNUM_TOPIC_RE.match(nt):
                            break
                        title = (title + " " + nt).strip()
                        j += 1
                    else:
                        break
                out.append((q_num, title))
                i = j
                continue
        i += 1
    return out


# ─── ITPE 모의고사 토픽 master 추출 (하이브리드 전략) ──────────────────
# ITPE 는 fitz analyze_pages 가 안정 작동하므로 페이지 분류는 fitz 에 위임.
# kordoc 의 가치는: 시험지 페이지에서 풀 텍스트 토픽 master 를 추출해
# fitz 가 잘라먹은 토픽 제목을 풍부하게 채워주는 것.
#
# ITPE 시험지 페이지 구조:
#   - heading fs=14 "제 N 회 ITPE 실전 명품 모의고사 해설집" (페이지 헤더)
#   - heading fs=20 "제 N 회 ITPE 실전 명품 모의고사" (시험 표지)
#   - heading fs=12 "제 N 교시(시험시간: ...)"
#   - table 블록의 cells 에 1. 토픽... (정관 일반 12개)
#   - paragraph/list fs=12 multi-line "Response) 비교 ⏎ 3. 시큐어..." (continuation)
#   - heading fs=12 "[정보관리기술사 선택문제]" + list fs=12 "13. <정관 13>"
#   - heading fs=12 "[컴퓨터시스템응용기술사 선택문제]" + list fs=12 "13. <컴응 13>"

ITPE_TRACK_HEADER_JEONGGWAN_RE = re.compile(r"\[\s*정보관리기술사\s*선택\s*문제\s*\]")
ITPE_TRACK_HEADER_COMPEUNG_RE = re.compile(r"\[\s*컴퓨터시스템응용기술사\s*선택\s*문제\s*\]")
ITPE_SESSION_HDR_RE = re.compile(r"제\s*([1-4])\s*교시\s*\(\s*시험시간")
ITPE_END_MARKER_RE = re.compile(r"^[“”\"\']?\s*끝\s*[“”\"\']?\s*$")
# "1. 토픽" / "13. 토픽" 매칭 — ITPE 는 보통 fs=12 영역
_ITPE_QNUM_LINE_RE = re.compile(r"^(\d{1,2})\.\s+(.{4,})$")


def extract_itpe_master_topics(
    page_blocks: list[Block],
) -> dict[str, dict[int, str]]:
    """ITPE 시험지 페이지 1장에서 토픽 master 추출.

    Returns:
        {'common': {q_num: title, ...},        # 1-12 공통 (정관·컴응 분기 전)
         'jeonggwan': {13: title},              # [정보관리기술사 선택문제] 직후 13
         'compeung': {13: title}}               # [컴퓨터시스템응용기술사 선택문제] 직후 13

    회차/페이지마다 1-12 분포가 다를 수 있음 (1교시 vs 2-4교시).
    이 함수는 단일 시험지 페이지를 처리하므로 호출자(analyze)에서 교시별로 마스터를 누적.
    """
    body = filter_body_blocks(page_blocks, drop_table_marker=False)
    out = {"common": {}, "jeonggwan": {}, "compeung": {}}
    track = "common"
    pending_lines: list[str] = []  # paragraph multi-line 처리용

    def _try_match_line(line: str, target: dict[int, str]) -> None:
        ln = line.strip()
        m = _ITPE_QNUM_LINE_RE.match(ln)
        if not m:
            return
        q = int(m.group(1))
        t = m.group(2).strip()
        if not (1 <= q <= 16):
            return
        # 더 긴 제목 우선 (continuation 의 가치)
        if q not in target or len(t) > len(target[q]):
            target[q] = t

    for b in body:
        text = block_text(b)
        if not text:
            continue
        # 트랙 전환
        if ITPE_TRACK_HEADER_JEONGGWAN_RE.search(text):
            track = "jeonggwan"
            continue
        if ITPE_TRACK_HEADER_COMPEUNG_RE.search(text):
            track = "compeung"
            continue
        target_dict = out[track]
        # table 블록: 셀이 " | "로 join 되어 있음
        if b.get("type") == "table":
            for piece in text.split(" | "):
                _try_match_line(piece, target_dict)
            continue
        # paragraph multi-line: 줄바꿈으로 split
        for piece in text.split("\n"):
            _try_match_line(piece, target_dict)
    return out


def merge_itpe_masters(masters: list[dict[str, dict[int, str]]]) -> dict[str, dict[int, str]]:
    """여러 시험지 페이지의 master 들을 합침 (같은 교시 시험지가 여러 페이지에 걸친 경우)."""
    merged = {"common": {}, "jeonggwan": {}, "compeung": {}}
    for m in masters:
        for track, d in m.items():
            for q, t in d.items():
                if q not in merged[track] or len(t) > len(merged[track][q]):
                    merged[track][q] = t
    return merged


# ─── master anchor matching (PR 5) ─────────────────────────────────
# 문제: 시험지 master 순서와 해설 페이지 등장 순서가 어긋난 회차에서
#       단조 q_num 매핑(last+1)을 쓰면 토픽이 한 칸씩 밀려 잘못 매핑됨.
#       (예: KPC120 1교시 - 해설집이 master Q11/AGI 를 빼고 Q12/스펙트럼부터 시작)
# 해결: 페이지 본문에 master[q] 토픽의 anchor 키워드가 등장하면 그 q 사용.

# 토픽에서 anchor 추출 시 무시할 도입어
_TOPIC_LEAD_RE = re.compile(
    r"^(?:최근\s+|최신\s+|국내?\s*(?:외)?\s*|정부는?\s+|디지털\s+전환?에?\s+|"
    r"\d+\s*년\s*\d*월?\s*[,]?\s*|\“|\")"
)


# 너무 흔해서 다른 토픽과 충돌하는 토큰들 — anchor 후보에서 제외
_GENERIC_ANCHOR_TOKENS = {
    "AI", "IT", "ICT", "DX", "AI-",
    "인공지능", "데이터", "기술", "시스템", "정보", "보안", "관리",
    "디지털", "서비스", "프로젝트", "소프트웨어", "네트워크", "플랫폼",
    "최근", "최신", "다음에", "이에", "관련하여", "대하여",
    # 학원 PDF 페이지 헤더/표지 — table 텍스트 검사 시 false-positive 방지
    "KPC", "ITPE", "IMPACT", "기술사", "모의고사", "실전모의고사",
    "한국생산성본부", "Copyright", "한국정보통신기술협회",
}


def _topic_anchor_tokens(topic: str) -> list[str]:
    """master 토픽에서 본문 매칭에 쓸 후보 anchor 토큰들을 추출.

    우선순위:
        1) 영문 약어 + 한글 정의 ("AI-RAN", "PWA", "HBM" 등)
        2) 첫 명사구 (도입어 제거 후 6자 이상으로 강화)
        3) 괄호 안 영문 정의 ("(Domain-Specific Language Models)")

    generic 토큰("인공지능", "AI" 등)은 제외 — 다른 토픽과 충돌.
    """
    if not topic:
        return []
    s = topic.strip()
    out: list[str] = []

    # 1) 영문 대문자 약어 (3-7자, 하이픈 허용)
    for m in re.finditer(r"\b([A-Z][A-Z0-9\-]{2,7})\b", s):
        token = m.group(1)
        if token in _GENERIC_ANCHOR_TOKENS:
            continue
        out.append(token)

    # 2) 도입어 제거 후 첫 한글 명사구 1-2 단어. 조사 trim 으로 짧고 정확한 anchor 생성.
    # substring 매칭 성공률 ↑ ("프로젝트 위험관리에" → "프로젝트 위험관리").
    cleaned = _TOPIC_LEAD_RE.sub("", s).lstrip()
    m = re.match(r"^([가-힣]{2,}(?:\s+[A-Za-z0-9가-힣\-]+){0,1})", cleaned)
    if m:
        token = m.group(1).strip()
        token = _normalize_korean_token(token)
        if len(token) >= 4 and token not in _GENERIC_ANCHOR_TOKENS:
            out.append(token)

    # 3) 괄호 안 영문 정의
    for m in re.finditer(r"\(([A-Za-z][A-Za-z\s\-]{6,40})\)", s):
        token = m.group(1).strip()
        if token not in _GENERIC_ANCHOR_TOKENS:
            out.append(token)

    seen = set()
    uniq = []
    for t in out:
        if t not in seen and len(t) >= 4:
            seen.add(t)
            uniq.append(t)
    uniq.sort(key=len, reverse=True)
    return uniq


# 한국어 단어 끝 조사들 (token 정규화용)
_KOR_TRAILING_JOSA_RE = re.compile(
    r"(?:으로|에서|에게|이?다|이?며|에|를|을|이|가|는|은|의|와|과|도|로)$"
)


def _normalize_korean_token(t: str) -> str:
    """한국어 단어 끝 조사 제거 (위험관리에 → 위험관리)."""
    if not t:
        return t
    # 조사 1회만 제거 (이중 조사는 회차/패턴 분석 시 추가)
    while True:
        m = _KOR_TRAILING_JOSA_RE.search(t)
        if not m:
            break
        # 조사 제거 후 길이가 충분해야 (조사가 단어의 80% 이상이면 stop)
        suffix_len = m.end() - m.start()
        if len(t) - suffix_len < 2:
            break
        t = t[:m.start()]
        break  # 1회만
    return t


def _master_topic_keywords(text: str) -> set[str]:
    """master 토픽 또는 페이지 본문에서 검색용 단어 토큰 셋 추출.

    - 한글 명사구: 2자+ 연속 한글, 끝 조사 제거(위험관리에 → 위험관리)
    - 영문 약어/명사: 대문자로 시작 3자+

    조사 정규화로 "위험관리" / "위험관리에" / "위험관리의" 가 모두 동일 토큰으로 매칭.
    """
    if not text:
        return set()
    tokens: set[str] = set()
    for m in re.finditer(r"[가-힣]{2,}", text):
        t = _normalize_korean_token(m.group())
        if not t or len(t) < 2:
            continue
        if t in _GENERIC_ANCHOR_TOKENS:
            continue
        tokens.add(t)
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9\-]{2,11})\b", text):
        t = m.group(1)
        if t in _GENERIC_ANCHOR_TOKENS:
            continue
        tokens.add(t)
    return tokens


def has_master_anchor_in_page(page_blocks: list[Block], master_topic: str,
                                head_block_count: int = 10) -> bool:
    """master_topic 의 anchor 토큰이 페이지 본문(첫 head_block_count 블록)에 있는가.

    PR 8: q_num override 의 1차 sanity check 로 사용.
    expected_q 의 master 토픽 anchor 가 본문에 매칭되면 단조 매핑이 정확하다고 보고
    그 외 q 의 anchor 가 매칭되더라도 무시 (KPC128 같은 정상 회차의 회귀 방지).
    """
    if not master_topic:
        return False
    anchors = _topic_anchor_tokens(master_topic)
    if not anchors:
        return False
    body = filter_body_blocks(page_blocks)
    head_text = " ".join(block_text(b) for b in body[:head_block_count])
    if not head_text:
        return False
    for a in anchors:
        if a in head_text:
            return True
    return False


def kpc_match_q_by_master(
    page_blocks: list[Block], master: dict[int, str],
    *, head_block_count: int = 10, min_overlap: int = 2,
) -> tuple[Optional[int], Optional[str]]:
    """페이지 본문이 master 어떤 토픽과 매칭되는지 (q_num, matched_anchor) 반환.

    매칭 단계 (1순위 → 2순위):
        1) 기존 substring anchor matching (_topic_anchor_tokens 사용)
           — 정확하지만 한국어 조사로 substring 실패 가능
        2) 단어 토큰 교집합 매칭 (_master_topic_keywords)
           — min_overlap 개 이상 공통 토큰이면 매칭

    Returns:
        (q_num, anchor) 또는 매칭 실패 시 (None, None)
    """
    if not master:
        return None, None
    # KPC124 같은 회차는 토픽 제목이 table 셀에 박힘 → table 텍스트도 포함
    body = filter_body_blocks(page_blocks, drop_table_marker=False)
    head_text = " ".join(block_text(b) for b in body[:head_block_count])
    if not head_text:
        return None, None

    # 1) substring anchor matching
    candidates: list[tuple[int, int, int, str]] = []
    for q, topic in master.items():
        for anchor in _topic_anchor_tokens(topic):
            pos = head_text.find(anchor)
            if pos >= 0:
                candidates.append((q, len(anchor), pos, anchor))
                break
    if candidates:
        candidates.sort(key=lambda x: (-x[1], x[2]))
        q, _, _, anchor = candidates[0]
        return q, anchor

    # 2) 토큰 교집합 매칭 (substring 실패 시 fallback)
    page_tokens = _master_topic_keywords(head_text)
    if not page_tokens:
        return None, None
    overlap_candidates: list[tuple[int, int, str]] = []  # (q, overlap, sample_token)
    for q, topic in master.items():
        topic_tokens = _master_topic_keywords(topic)
        common = page_tokens & topic_tokens
        if len(common) >= min_overlap:
            # 샘플 토큰 — 가장 긴 것 (디버그용)
            sample = max(common, key=len)
            overlap_candidates.append((q, len(common), sample))
    if not overlap_candidates:
        return None, None
    # 더 큰 overlap 우선
    overlap_candidates.sort(key=lambda x: -x[1])
    q, _, sample = overlap_candidates[0]
    return q, sample


# ─── 파일명용 토픽 축약 헬퍼 (PR 5) ─────────────────────────────────
# 현재 분할 PDF 파일명에 토픽 풀텍스트가 그대로 박혀 가독성이 떨어짐.
# KPC 서술형은 "최근 X 에 대하여 다음을 설명하시오. 가. ~ 나. ~ 다. ~" 형태로 60-200자 길이.
# 핵심 키워드 + 영문 약어 위주로 축약해 파일 목록에서 한눈에 식별 가능하게 만든다.
# 메타데이터 title 은 풀텍스트를 그대로 유지(검색성).

# "가. <부주제>" 추출 — 서술형 sub-prompt (KPC 모의고사 핵심 의미 단위)
_TOPIC_SUB_RE = re.compile(r"[가나다라마]\.\s*([^가-힣]?[^.\n]{2,50}?)(?=\s*[가나다라마]\.|\s*$)")
# 도입절 패턴 — "최근/최신/X은 ... 기술이다. 다음을 설명하시오." 형태
_TOPIC_PREAMBLE_RE = re.compile(
    r"^(?:최근\s*|최신\s*|국내?\s*외?\s*|정부\s*는?\s+)?"
    r".*?(?:이?다\.|\.\s*다음에?)\s*"
)
# 영문 약어/대문자 토큰 (3-12자, 하이픈 허용)
_ABBR_RE = re.compile(r"\b([A-Z][A-Za-z0-9\-]{2,11})\b")


def _trim_sub_topic(t: str) -> str:
    """부주제(가./나./다.) 텍스트를 18자 내외로 정리."""
    s = t.strip().rstrip(" ,.").strip()
    # "X 의 개념 및 ..." → "X" / "X 와 Y 비교" → 그대로
    # 18자 초과면 첫 명사구로
    if len(s) <= 22:
        return s
    # 첫 마침표/쉼표/괄호 전까지
    for sep in [", ", " 및 ", "(", " ㆍ "]:
        idx = s.find(sep)
        if 4 < idx < 22:
            return s[:idx].strip()
    return s[:22].rstrip(" ,.")


def short_topic_label(title: str, max_len: int = 60) -> str:
    """토픽을 파일명에 적합한 짧은 라벨로 의미 축약.

    KPC 서술형 패턴: "최근 X 에 대하여 다음을 설명하시오. 가. ~ 나. ~ 다. ~"
    핵심 의미 = X (주 키워드) + 가/나/다 부주제들 → 이걸 추출해 간결화.

    우선순위:
        1) 짧으면(≤max_len) 그대로
        2) 부주제(가./나./다.) 가 있고 그 합산이 짧으면 부주제 위주로 압축
           (선택: 영문 약어가 첫 부분에 있으면 prefix)
        3) 부주제 부재면 "에 대하여 다음을 설명" 정형 절단 후 첫 명사구
        4) fallback: 첫 max_len 자
    """
    if not title:
        return ""
    s = re.sub(r"\s+", " ", title.strip())
    if len(s) <= max_len:
        return s

    # ── 1) 부주제 추출 ──
    sub_raw = _TOPIC_SUB_RE.findall(s)
    sub_topics = [_trim_sub_topic(t) for t in sub_raw if len(t.strip()) >= 2]
    sub_topics = [t for t in sub_topics if t]

    # ── 2) 영문 약어 (제목 앞부분에 있는 것) ──
    # 첫 60자 안의 영문 약어를 우선 (긴 도입절 안의 우연한 약어 회피)
    head_60 = s[:60]
    abbr_candidates = [m.group(1) for m in _ABBR_RE.finditer(head_60)
                        if m.group(1).upper() not in
                        {"AI", "IT", "ICT", "DX", "AND", "FOR", "PDF", "API"}]
    main_abbr = abbr_candidates[0] if abbr_candidates else ""

    # ── 3) 부주제 위주 합성 ──
    if sub_topics:
        joined = " · ".join(sub_topics[:3])
        if len(joined) > max_len - 5:
            joined = " · ".join(sub_topics[:2])
        if main_abbr and main_abbr not in joined:
            cand = f"{main_abbr}: {joined}"
        else:
            cand = joined
        if len(cand) <= max_len:
            return cand
        # 너무 길면 첫 부주제만
        if len(sub_topics[0]) <= max_len - 4 and main_abbr:
            return f"{main_abbr}: {sub_topics[0]}"
        return sub_topics[0][:max_len]

    # ── 4) 부주제 부재: 도입절 제거 + 첫 명사구 ──
    body = s
    # "X 에 대하여 다음을 설명하시오" 종결 패턴 직전까지
    m = re.search(
        r"(?:에\s*대[하해](?:여|서)?\s*)?다음에?\s*(?:대[하해](?:여|서)?\s*)?"
        r"(?:설명|답|기술)하?(?:시오|십시오)?",
        body,
    )
    if m and m.start() > 3:
        body = body[:m.start()].rstrip(" ,.").strip()

    # 도입절 "X 은 ... 기술이다." 제거 후 마지막 명사구 살리기
    if "이다." in body[:max_len + 30]:
        idx = body.rfind("이다.", 0, max_len + 30)
        if idx > 10:
            after = body[idx + 3:].strip()
            if 4 <= len(after) <= max_len:
                body = after

    body = re.sub(r"\s*에\s*대[하해](?:여|서)?\s*$", "", body).strip()
    body = re.sub(r"\s*은|는|을|를|이|가\s*$", "", body).strip()

    if main_abbr and main_abbr not in body[:30]:
        body = f"{main_abbr}: {body}"

    if len(body) > max_len:
        body = body[:max_len].rstrip()
    return body or s[:max_len]


# ─── 엔진 비교 출력 헬퍼 (--compare 모드 공통) ────────────────────────

def print_q_list_diff(
    fitz_q_list: list, kordoc_q_list: list,
    *, label_a: str = "fitz", label_b: str = "kordoc",
) -> dict:
    """두 엔진의 q_list 결과를 (sess, num) 키 기준으로 정렬해 표 형태 출력.

    q_list element = (session, q_num, topic, page_start, page_end)
    같은 (session, q_num) 이 두 번 등장하는 ITPE 정관/컴응 케이스도 처리 — 출현 순서로 매칭.

    Returns:
        통계 dict (양쪽 카운트, 토픽 차이 건수 등)
    """
    def _by_key(items: list) -> dict[tuple, list]:
        d: dict[tuple, list] = {}
        for sess, num, topic, ps, pe in items:
            key = (sess, num)
            d.setdefault(key, []).append((topic, ps, pe))
        return d

    a = _by_key(fitz_q_list)
    b = _by_key(kordoc_q_list)
    all_keys = sorted(set(a.keys()) | set(b.keys()))

    # 한 줄당 최대 너비 제한
    def _fmt(topic: str, ps: int, pe: int) -> str:
        snippet = (topic or "").strip().replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        return f"p.{ps + 1:>3}-{pe + 1:<3} {snippet}"

    print(f"\n[{label_a} vs {label_b} 비교]")
    print(f"  {'Key':<10} | {label_a:<70} | {label_b:<70}")
    print(f"  {'-'*10}-+-{'-'*70}-+-{'-'*70}")

    only_a = only_b = topic_diff = pagerange_diff = 0
    for key in all_keys:
        sess, num = key
        la = a.get(key, [])
        lb = b.get(key, [])
        max_n = max(len(la), len(lb))
        for idx in range(max_n):
            ka = f"M{sess}Q{num:02d}" + (f"#{idx+1}" if max_n > 1 else "")
            ta = la[idx] if idx < len(la) else None
            tb = lb[idx] if idx < len(lb) else None
            sa = _fmt(*ta) if ta else "(없음)"
            sb = _fmt(*tb) if tb else "(없음)"
            marker = " "
            if ta is None:
                marker = "+"; only_b += 1
            elif tb is None:
                marker = "-"; only_a += 1
            else:
                if ta[0].strip() != tb[0].strip():
                    topic_diff += 1
                    marker = "≠" if marker == " " else marker
                if ta[1] != tb[1] or ta[2] != tb[2]:
                    pagerange_diff += 1
                    marker = "p"
            print(f"  {marker} {ka:<8} | {sa:<70} | {sb:<70}")

    print()
    print(f"  요약: {label_a} {len(fitz_q_list)} vs {label_b} {len(kordoc_q_list)} | "
          f"only-{label_a} {only_a} | only-{label_b} {only_b} | "
          f"토픽 차이 {topic_diff} | 페이지 범위 차이 {pagerange_diff}")
    return {
        f"{label_a}_count": len(fitz_q_list),
        f"{label_b}_count": len(kordoc_q_list),
        "only_a": only_a, "only_b": only_b,
        "topic_diff": topic_diff, "pagerange_diff": pagerange_diff,
    }


# ─── CLI 디버그 진입점 ────────────────────────────────────────────────

def _debug_main(argv: list[str]) -> int:
    """python kordoc_adapter.py <pdf> [--page N] — 디버그용."""
    if not argv:
        print(__doc__)
        return 2
    pdf = argv[0]
    page_filter: Optional[int] = None
    if "--page" in argv:
        i = argv.index("--page")
        if i + 1 < len(argv):
            page_filter = int(argv[i + 1])
    pages_blocks, total = parse_kordoc_pages(pdf, verbose=True)
    print(f"총 페이지: {total}, 블록 합계: {sum(len(v) for v in pages_blocks.values())}")
    targets = [page_filter] if page_filter else sorted(pages_blocks.keys())[:5]
    for pg in targets:
        print(f"\n=== p.{pg} ===")
        body = filter_body_blocks(pages_blocks.get(pg, []))
        kind, meta = kpc_classify_page(pages_blocks.get(pg, []))
        print(f"  분류: {kind} {meta}")
        for b in body[:20]:
            t = b.get("type")
            fs = b.get("font_size")
            text = block_text(b)[:120].replace("\n", " ⏎ ")
            print(f"  [{t:9} fs={fs}] {text}")
    return 0


if __name__ == "__main__":
    sys.exit(_debug_main(sys.argv[1:]))
