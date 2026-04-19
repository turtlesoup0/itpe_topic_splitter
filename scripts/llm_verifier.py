#!/usr/bin/env python3
"""
LLM 기반 토픽 분할 검증기 (Haiku)

기존 규칙 기반 boundary 결과를 LLM으로 후처리하여 품질을 높인다.
- A. 제목 + 키워드 추출: boundary 텍스트 → 간결한 제목 + 핵심 키워드
- B. 경계 검증: 끝 페이지 + 다음 페이지 → 같은 토픽인지 판단
- C. 저신뢰 구간 재판정: confidence < 0.5 구간에서 경계 재탐색

LLM 실패 시 기존 결과를 그대로 유지 (graceful degradation).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── 설정 ────────────────────────────────────────────────────────

# Provider 선택: "anthropic" (기본, Haiku API) | "mlx" (로컬 MLX-LM 서버)
#   환경변수 LLM_PROVIDER로 전환
DEFAULT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
DEFAULT_MLX_URL = "http://127.0.0.1:8090"
DEFAULT_MLX_MODEL = "Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2"

MAX_CONCURRENT = 10  # 동시 API 호출 수 제한
_SYNC_TIMEOUT = 300  # enhance_boundaries_sync 타임아웃(초, MLX 고려해 확장)

# 싱글턴 클라이언트 캐시
_client_cache: Any = None

# MODEL 상수는 하위 호환을 위해 유지하지만 provider에 따라 동적으로 결정됨
MODEL = DEFAULT_MODEL_ANTHROPIC


def _provider() -> str:
    """현재 LLM provider (소문자 정규화)."""
    return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()


def is_available() -> bool:
    """LLM 검증 사용 가능 여부 (호출 시점에 환경변수 확인)."""
    p = _provider()
    if p == "mlx":
        # MLX는 로컬 서버 가동 여부로 판단 (URL 존재만 체크, 실제 연결은 런타임 검증)
        return bool(os.environ.get("MLX_URL", DEFAULT_MLX_URL))
    # 기본: Anthropic API 키
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))


class _MLXClient:
    """MLX-LM OpenAI 호환 서버를 Anthropic SDK 인터페이스로 래핑.

    client.messages.create(model=, max_tokens=, system=, messages=[]) 시그니처 호환.
    Response는 .content[0].text 로 접근 가능.
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._session = None
        # 하위 코드가 client.messages.create(...)로 호출 → self가 messages 역할
        self.messages = self

    async def _session_get(self):
        # httpx 사용 (anthropic SDK가 이미 의존하므로 추가 의존성 0)
        import httpx
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=10.0),
                limits=httpx.Limits(max_connections=MAX_CONCURRENT * 2,
                                    max_keepalive_connections=MAX_CONCURRENT),
            )
        return self._session

    async def create(self, *, model=None, max_tokens: int,
                     system: str, messages: list, **_):
        session = await self._session_get()
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "messages": [{"role": "system", "content": system}] + list(messages),
            # SuperGemma4 thinking 억제 (chat_template.jinja 에 enable_thinking 조건 존재)
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = await session.post(f"{self.base_url}/v1/chat/completions",
                                   json=body)
        data = resp.json()
        text = (data.get("choices", [{}])[0]
                    .get("message", {}).get("content") or "")

        # Anthropic SDK의 resp.content[0].text 형태로 반환
        class _Block:
            __slots__ = ("text",)

            def __init__(self, t): self.text = t

        class _Resp:
            __slots__ = ("content",)

            def __init__(self, t): self.content = [_Block(t)]

        return _Resp(text)


def _get_client():
    """Provider별 Lazy import + 싱글턴 클라이언트 반환."""
    global _client_cache, MODEL
    if _client_cache is not None:
        return _client_cache

    p = _provider()
    if p == "mlx":
        base = os.environ.get("MLX_URL", DEFAULT_MLX_URL)
        mlx_model = os.environ.get("MLX_MODEL", DEFAULT_MLX_MODEL)
        MODEL = mlx_model  # 참고용 (실제 호출은 _MLXClient가 자체 보유)
        _client_cache = _MLXClient(base, mlx_model)
        logger.info(f"LLM provider = MLX ({base}, {mlx_model})")
    else:
        import anthropic
        MODEL = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL_ANTHROPIC)
        _client_cache = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"])
        logger.info(f"LLM provider = Anthropic ({MODEL})")
    return _client_cache


# ─── 공통 유틸 ───────────────────────────────────────────────────

def _extract_text(elements: list, start: int, end: int,
                  max_chars: int = 2000, *, tag_pages: bool = True) -> str:
    """페이지 범위의 elements를 텍스트로 결합.

    Args:
        start, end: 페이지 번호 (1-indexed, inclusive)
        tag_pages: True면 각 줄에 [pN] 태그 부착 (reclassify용)
    """
    lines: list[str] = []
    total = 0
    for e in elements:
        pg = e["page"]
        if pg < start or pg > end:
            continue
        c = e.get("content", "").strip()
        if not c:
            continue
        lines.append(f"[p{pg}] {c}" if tag_pages else c)
        total += len(c) + (10 if tag_pages else 0)
        if total >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


def _parse_json(raw: str) -> Optional[dict]:
    """LLM 응답에서 첫 번째 JSON 객체를 추출."""
    m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if not m:
        # 중첩 JSON 시도 (boundaries 배열이 포함된 경우)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


async def _call_llm(client, sem: asyncio.Semaphore,
                    system: str, user_text: str,
                    max_tokens: int = 200) -> Optional[dict]:
    """LLM 호출 + JSON 파싱 공통 래퍼."""
    async with sem:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        return _parse_json(resp.content[0].text.strip())


# ─── 0. LLM 우선 경계 탐지 ───────────────────────────────────────

_BOUNDARY_SYSTEM = """정보처리기술사 해설 PDF의 토픽 경계를 JSONL 한 줄씩 출력하세요.

형식 (한 줄 = 한 토픽):
{"num": 1, "title": "제목(40자 이내)", "page_start": N, "page_end": M, "session": S}

핵심 규칙 (반드시 지키기):
A. **중복 금지**: 같은 토픽을 두 번 출력하지 마세요.
   (잘못된 예: "양자 암호 기술 p49-49"와 "양자 암호 기술 상세 p61-63" 두 번 출력 → 금지. 실제 해당 토픽 범위 한 번만)
B. **중첩 금지**: 페이지 범위는 절대 겹치지 않아야 함.
   이전 토픽의 page_end < 다음 토픽의 page_start.
   특히 세션 전체 범위를 하나의 토픽으로 묶지 마세요.
   (잘못된 예: 세션2 전체 p29-42 를 한 토픽으로 → 금지. 세션2 안에 6개 토픽이 있으면 각각 분리)
C. **페이지 범위 유효**: page_end >= page_start, 둘 다 1~전체 페이지 수 사이.

일반 규칙:
1. 각 토픽은 문제 번호("N.", "N번", "I.", "II.")로 시작.
2. 표지/목차/저작권 페이지는 제외 (실제 해설 토픽만).
3. 본시험: 1교시=단답형 13개(2~3p), 2~4교시=논술 6개(4~6p).
   표지에 "제 N 교시"가 있으면 세션 전환 신호.
4. 출제의도/참조 문구("131회 2교시" 등) 내 교시 언급은 구조 신호 아님.
5. 주간 모의고사/리뷰는 단일 교시이고 토픽 수 불규칙할 수 있음.
6. num은 문서 전체에서 1부터 증가 (세션마다 리셋하지 않음).
7. 배열/설명 없이 JSONL만 (한 줄 = 한 완전한 JSON 객체).
8. 포맷 힌트:
   - KPC: 각 토픽 제목 직후 "★★★☆☆" 별점 패턴 → 새 토픽 시작의 강한 신호
   - ITPE: "I. 개요" 류 로마 숫자 소제목
   - 공통: 페이지 첫 줄에 "N." 아라비아 번호 + 제목
   이런 신호가 보이면 놓치지 말고 반드시 새 토픽으로 기록.

출력 전 자체 검증: 내가 출력한 토픽들의 page_start 를 정렬했을 때 단조 증가하는가? 중복 제목은 없는가? 요약에서 "★" 별점 또는 "N." 번호 패턴이 나타난 페이지를 빠뜨리지 않았는가?"""


def _page_summary(elements: list, total_pages: int,
                  max_lines_per_page: int = 5,
                  max_chars_per_line: int = 80) -> str:
    """페이지별 상위 N줄을 간결 요약한 텍스트 (LLM 경계 탐지 입력용)."""
    page_heads: dict[int, list[str]] = {}
    for e in elements:
        pg = e.get("page", 0)
        c = (e.get("content") or "").strip()
        if not c or len(c) < 3:
            continue
        if "Copyright" in c or "All rights reserved" in c:
            continue
        page_heads.setdefault(pg, []).append(c)

    lines = []
    for pg in sorted(page_heads.keys()):
        heads = [l[:max_chars_per_line] for l in page_heads[pg][:max_lines_per_page]]
        lines.append(f"[p{pg:02d}] " + " | ".join(heads))
    return "\n".join(lines)


def _parse_jsonl(raw: str) -> list[dict]:
    """JSONL 형식에서 완전한 JSON 객체들을 추출. 깨진 줄은 복구 시도."""
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",").rstrip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            # key 오타 보정 시도 (세션 -> s 같은 shortform 복구)
            fixed = re.sub(r'"s"\s*:', '"session":', line)
            fixed = re.sub(r'"p_s"\s*:', '"page_start":', fixed)
            fixed = re.sub(r'"p_e"\s*:', '"page_end":', fixed)
            try:
                obj = json.loads(fixed)
            except Exception:
                continue
        if isinstance(obj, dict) and "num" in obj and "page_start" in obj:
            out.append(obj)
    return out


def _strip_title_prefix(t: str) -> str:
    """제목 앞 번호 접두사 제거.

    예: "1. 할당간선" → "할당간선"
        "II. 정의" → "정의"
        "3) QoS" → "QoS"
    LLM이 원문 첫 줄을 그대로 복사한 경우를 정리.
    """
    if not t:
        return t
    t = t.strip()
    # 아라비아/로마 숫자 + 구분자(. 또는 )) + 공백
    t = re.sub(r'^\s*\d{1,2}\s*[.)]\s+', '', t)
    t = re.sub(r'^\s*[IVXivx]{1,4}\s*[.)]\s+', '', t)
    # "문제 1." 같은 형태
    t = re.sub(r'^\s*문제\s*\d{1,2}\s*[.)]?\s*', '', t)
    return t.strip()


def _normalize_title(t: str) -> str:
    """제목 정규화 (중복 감지용): 번호접두사·괄호·공백 제거, 소문자."""
    t = _strip_title_prefix(t or '')
    t = re.sub(r'\([^)]*\)', '', t)             # 괄호 제거
    t = re.sub(r'[\s\-_·,.]+', '', t)           # 공백·구분자 제거
    return t.lower()


def _merge_duplicate_titles(bdy: list[dict]) -> list[dict]:
    """같은 세션 내 제목이 유사한 토픽 병합.

    병합 조건 (OR):
    (a) 정규화 제목 완전 일치 ("양자 암호 기술 QKD" vs "양자 암호 기술 상세")
    (b) 앞 prefix 일치 ("6G 이동통신기술" vs "6G 이동통신기술 성능 요구사항")
    (c) 한쪽 제목이 다른 쪽에 완전히 포함 (부분 문자열)
    """
    if len(bdy) <= 1:
        return bdy

    def _prefix_key(t: str, n: int = 4) -> str:
        """정규화 후 앞 n자 (인접 조건과 결합해 false positive 제한)."""
        return _normalize_title(t)[:n]

    # 세션별로 순차 처리 (인접 경계 병합에 유리)
    by_sess: dict[int, list[dict]] = {}
    for b in bdy:
        by_sess.setdefault(int(b.get("session", 1)), []).append(b)

    result: list[dict] = []
    for sn in sorted(by_sess):
        items = sorted(by_sess[sn], key=lambda x: int(x.get("page_start", 0)))
        merged_session: list[dict] = []
        for b in items:
            title = b.get("title", "")
            norm = _normalize_title(title)
            pref = _prefix_key(title)
            ps = int(b.get("page_start", 0))
            pe = int(b.get("page_end", ps))
            merged_into_prev = False
            # 세션 내 이미 추가된 경계와 비교
            for prev in merged_session:
                p_title = prev.get("title", "")
                p_norm = _normalize_title(p_title)
                p_pref = _prefix_key(p_title)
                p_ps = int(prev.get("page_start", 0))
                p_pe = int(prev.get("page_end", p_ps))
                # 제목 유사도: 완전일치 OR prefix 일치(>=5자) OR 포함관계
                similar = (
                    (p_norm and p_norm == norm) or
                    (len(p_pref) >= 4 and p_pref == pref) or
                    (p_norm and norm and
                     (p_norm in norm or norm in p_norm))
                )
                if not similar:
                    continue
                # 인접(<=2p)한 경우만 범위 병합, 멀리 떨어진 경우 뒤쪽 삭제
                if ps - p_pe <= 2:
                    prev["page_end"] = max(p_pe, pe)
                    prev["page_start"] = min(p_ps, ps)
                    if len(p_title) > len(title) and title.strip():
                        prev["title"] = title
                merged_into_prev = True
                break
            if not merged_into_prev:
                merged_session.append(b)
        result.extend(merged_session)
    return result


def _remove_containing_boundaries(bdy: list[dict]) -> list[dict]:
    """다른 토픽을 완전히 포함하는 부모 토픽 제거 (LLM 세션-전체 hallucination 방어).

    예: {p29-42} 안에 {p30-32}, {p33-36}이 있으면 p29-42를 제거.
    """
    result = []
    for i, b in enumerate(bdy):
        ps_i = int(b.get("page_start", 0))
        pe_i = int(b.get("page_end", ps_i))
        sn_i = int(b.get("session", 1))
        if ps_i >= pe_i:
            result.append(b)
            continue
        contains_others = 0
        for j, c in enumerate(bdy):
            if i == j:
                continue
            if int(c.get("session", 1)) != sn_i:
                continue
            ps_j = int(c.get("page_start", 0))
            pe_j = int(c.get("page_end", ps_j))
            # c가 b 내부에 완전히 포함되고 b가 c보다 엄격히 넓으면 contain 카운트
            if ps_i <= ps_j and pe_j <= pe_i and (pe_i - ps_i) > (pe_j - ps_j):
                contains_others += 1
        if contains_others >= 2:
            # 2개 이상의 자식 토픽을 포함 → 세션-전체 hallucination으로 간주해 제거
            continue
        result.append(b)
    return result


def _validate_llm_boundaries(bdy: list[dict], total_pages: int) -> tuple[bool, str]:
    """LLM 경계 결과 검증. (ok, reason).

    역행 체크는 세션 내부에서만 수행 (세션 전환 시 num 리셋 허용).
    """
    if not bdy:
        return False, "경계 0개"
    pages_covered: set[int] = set()

    # 세션별 그룹화 → 각 세션 내에서 page_start 오름차순 확인
    by_session: dict[int, list[dict]] = {}
    for b in bdy:
        ps, pe = b.get("page_start"), b.get("page_end", b.get("page_start"))
        if not isinstance(ps, int) or not isinstance(pe, int):
            return False, f"페이지 번호 타입 오류 {b}"
        if ps < 1 or pe > total_pages or ps > pe:
            return False, f"페이지 범위 오류 {ps}-{pe} (문서 {total_pages}p)"
        for p in range(ps, pe + 1):
            pages_covered.add(p)
        by_session.setdefault(int(b.get("session", 1)), []).append(b)

    # 세션 내 역행 체크: page_start 기준 정렬 시 연속이어야 함.
    # num은 블록별로 독립 부여될 수 있어 신뢰하지 않음.
    for sn, group in by_session.items():
        group_sorted = sorted(group, key=lambda x: int(x.get("page_start", 0)))
        last_end = 0
        for b in group_sorted:
            ps = int(b["page_start"])
            if ps < last_end - 2:
                return False, f"세션{sn} 페이지 역행 {last_end} → {ps}"
            last_end = max(last_end, int(b.get("page_end", ps)))

    # 세션 간 연속성: session N 끝 페이지 < session N+1 시작 페이지
    session_ranges = []
    for sn in sorted(by_session):
        pages = [p for b in by_session[sn]
                 for p in range(int(b["page_start"]),
                                int(b.get("page_end", b["page_start"])) + 1)]
        session_ranges.append((sn, min(pages), max(pages)))
    for i in range(len(session_ranges) - 1):
        _, _, end_i = session_ranges[i]
        sn_j, start_j, _ = session_ranges[i + 1]
        if start_j < end_i - 2:
            return False, f"세션{sn_j} 시작 p{start_j} < 이전 세션 끝 p{end_i}"

    coverage = len(pages_covered) / max(1, total_pages)
    if coverage < 0.2:
        return False, f"커버리지 {coverage:.0%} 과소"
    return True, f"OK (커버 {coverage:.0%}, {len(bdy)}개)"


def _llm_boundaries_request_sync(doc_text: str, total_pages: int,
                                  timeout: float = 300.0,
                                  page_count_hint: int = 0) -> Optional[str]:
    """LLM에 경계 탐지 요청 (동기 래퍼). provider 독립.

    max_tokens는 입력 페이지 수에 비례해서 동적 산정:
      페이지당 평균 100 토큰(JSONL 한 줄 ~80자 + 여유) × 페이지 수 + 500 안전마진
    """
    user = (f"문서 {total_pages}p 요약:\n\n{doc_text}\n\n"
            f"JSONL 출력 (한 줄 한 토픽):")

    # 동적 max_tokens: 입력 페이지 수에 비례
    pg = page_count_hint or total_pages
    max_tokens = min(4000, max(500, pg * 100 + 500))

    p = _provider()
    try:
        if p == "mlx":
            import httpx
            url = (os.environ.get("MLX_URL", DEFAULT_MLX_URL).rstrip("/")
                   + "/v1/chat/completions")
            model = os.environ.get("MLX_MODEL", DEFAULT_MLX_MODEL)
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _BOUNDARY_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            with httpx.Client(timeout=timeout) as c:
                r = c.post(url, json=body)
                data = r.json()
            return (data.get("choices", [{}])[0]
                        .get("message", {}).get("content") or "")
        else:
            # Anthropic
            import anthropic
            client = anthropic.Anthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"])
            model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL_ANTHROPIC)
            resp = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=_BOUNDARY_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text if resp.content else ""
    except Exception as e:
        logger.warning("LLM boundary request failed: %s", e)
        return None


def _detect_session_ranges(elements: list, total_pages: int) -> list[tuple[int, int, int]]:
    """세션 블록 추출 (세션번호, 페이지_시작, 페이지_끝). 규칙 기반 detect_sessions 활용."""
    try:
        # 지연 import (circular 방지)
        from detect_boundaries_v2 import detect_sessions
        sessions = detect_sessions(elements, total_pages)
        if not sessions:
            return [(1, 1, total_pages)]
        return [(int(s.session_num or 1), int(s.page_start), int(s.page_end))
                for s in sessions]
    except Exception as e:
        logger.info("detect_sessions 실패 → 단일 세션: %s", e)
        return [(1, 1, total_pages)]


# 토픽 종료 구조 마커 — 반복되더라도 필터 제외 (경계 신호 보존)
_STRUCTURE_MARKERS = frozenset({"끝", "END", "Q.E.D.", "이상"})


def _norm_for_repeat(s: str) -> str:
    """반복 감지용 정규화: 공백/특수문자 제거, 소문자.

    "누구나 ICT 전문가..."와 "누구나ICT 전문가..." 를 동일 그룹으로 취급.
    """
    return re.sub(r'[\s#!\-_.:;,(){}\[\]·"\'"'']+', '', (s or '')).lower()


def _detect_repeated_lines(elements: list, page_start: int, page_end: int,
                             min_pages: int = 3) -> set[str]:
    """페이지 범위에서 반복되는 텍스트 라인 = 헤더/푸터로 간주 (2-tier).

    Tier 1 (80% 이상 반복): 길이 무관 필터
      - 짧은 단어라도 거의 모든 페이지에 반복되면 헤더 (e.g. "135 회", "ICT의")
    Tier 2 (40% 이상 반복 + 10자+): 긴 반복만 필터
      - 애매한 반복(토픽 제목 등)을 과도 필터하지 않도록 길이 제한

    공백/특수문자 변형 다른 버전도 **정규화 후 동일 그룹**으로 묶어 필터
    (e.g. "누구나 ICT"와 "누구나ICT" 병합).

    예외: _STRUCTURE_MARKERS에 등록된 단어는 절대 필터 안 함 ("끝" 등).
    """
    # 정규화된 키 → (원본 라인 집합, 페이지 집합)
    norm_groups: dict[str, tuple[set[str], set[int]]] = {}
    for e in elements:
        pg = e.get("page", 0)
        if pg < page_start or pg > page_end:
            continue
        c = (e.get("content") or "").strip()
        if not c or len(c) < 2:
            continue
        key = _norm_for_repeat(c)
        if not key:
            continue
        if key not in norm_groups:
            norm_groups[key] = (set(), set())
        norm_groups[key][0].add(c)
        norm_groups[key][1].add(pg)

    total = max(1, page_end - page_start + 1)
    t1 = max(min_pages, int(total * 0.8))   # tier 1: 80%+
    t2 = max(min_pages, int(total * 0.4))   # tier 2: 40%+ (긴 것만)

    result: set[str] = set()
    for key, (lines, pages) in norm_groups.items():
        # white-list 스킵: 구조 마커는 정규화 후 비교
        if any(_norm_for_repeat(m) == key for m in _STRUCTURE_MARKERS):
            continue
        count = len(pages)
        # 길이 기준: 그룹 내 평균 길이
        avg_len = sum(len(l) for l in lines) / max(1, len(lines))
        if count >= t1:
            result |= lines
        elif count >= t2 and avg_len >= 10:
            result |= lines
    return result


def _page_summary_range(elements: list, page_start: int, page_end: int,
                         max_lines_per_page: int = 5,
                         max_chars_per_line: int = 80,
                         total_pages: Optional[int] = None) -> str:
    """특정 페이지 범위의 요약. 반복 헤더/푸터 자동 필터.

    반복 감지는 **전체 문서 범위**로 수행해야 짧은 세션 블록에서도
    헤더가 제대로 걸러짐 (e.g. 세션3의 p48-70 범위만으론 "135 회"
    반복 빈도가 부족해 필터 실패).
    """
    # 전체 문서에서 반복 필터 대상 계산
    max_pg = total_pages or max(
        (e.get("page", 0) for e in elements), default=page_end)
    repeated = _detect_repeated_lines(elements, 1, max_pg)

    page_heads: dict[int, list[str]] = {}
    for e in elements:
        pg = e.get("page", 0)
        if pg < page_start or pg > page_end:
            continue
        c = (e.get("content") or "").strip()
        if not c or len(c) < 3:
            continue
        if "Copyright" in c or "All rights reserved" in c:
            continue
        if c in repeated:
            continue
        page_heads.setdefault(pg, []).append(c)
    lines = []
    for pg in sorted(page_heads.keys()):
        heads = [l[:max_chars_per_line]
                 for l in page_heads[pg][:max_lines_per_page]]
        lines.append(f"[p{pg:02d}] " + " | ".join(heads))
    return "\n".join(lines)


def detect_boundaries_llm(
    elements: list, total_pages: int,
) -> Optional[tuple[list[dict], list[str]]]:
    """LLM 우선 경계 탐지. 세션별 개별 호출 → 병합.

    세션 블록이 감지되면 각 세션을 별도 LLM 호출로 처리하여
    hallucination/누락 위험을 줄이고 병렬 처리 가능.
    """
    if not is_available():
        return None
    if total_pages <= 0 or not elements:
        return None

    sessions = _detect_session_ranges(elements, total_pages)
    logger.info("LLM 경계 탐지: %d개 세션 블록", len(sessions))

    # 세션 보강: detect_sessions가 앞/뒤 페이지를 놓친 경우 처리.
    # 규칙:
    # - 블록 크기가 비정상(<5p)이면 detect_sessions 결과 전체 무시 → 전체 1회 호출
    # - 앞/뒤 누락 5p 이상 → 독립 블록으로 추가 (KPC 132관: p1-27 → 1교시 추가)
    # - 앞/뒤 누락 1~4p → 인접 블록에 흡수 (동기회 135관: p1-4 → 1교시 확장)
    if sessions:
        # 블록 크기 sanity: 비정상적으로 작은 블록(<5p)이 있으면 session 신호 신뢰 불가
        sizes = [pe - ps + 1 for _, ps, pe in sessions]
        if any(sz < 5 for sz in sizes):
            logger.info("세션 블록 크기 비정상 (%s) → 세션 정보 무시",
                         sizes)
            sessions = []
        else:
            _pages_only: list[list[int]] = [[ps, pe] for _, ps, pe in sessions]
            # 앞쪽 누락
            gap_front = _pages_only[0][0] - 1
            if gap_front >= 5:
                _pages_only.insert(0, [1, _pages_only[0][0] - 1])
            elif gap_front >= 1:
                _pages_only[0][0] = 1
            # 뒤쪽 누락
            gap_back = total_pages - _pages_only[-1][1]
            if gap_back >= 5:
                _pages_only.append([_pages_only[-1][1] + 1, total_pages])
            elif gap_back >= 1:
                _pages_only[-1][1] = total_pages
            # 중간 gap은 건드리지 않음 (연속 블록을 합치지 않도록)

            sessions_before = len(sessions)
            sessions = [(i + 1, ps, pe) for i, (ps, pe) in enumerate(_pages_only)]
            if len(sessions) != sessions_before:
                logger.info("세션 블록 보강: %d개 → %d개",
                             sessions_before, len(sessions))

    # 세션 블록 신뢰도 판단:
    # - 4블록 + 문서 전체 커버(p1~total_pages) → 정규 4교시 시험, 블록별 호출
    # - 아닌 경우(불완전 또는 FB22처럼 2블록) → 전체 문서 단일 호출 fallback
    covers_all = (bool(sessions)
                  and sessions[0][1] <= 1
                  and sessions[-1][2] >= total_pages)
    is_clean_4session = (len(sessions) == 4) and covers_all
    treat_as_multi_session = is_clean_4session
    if not is_clean_4session:
        logger.info("세션 블록 %d개(커버%s) → 전체 문서 단일 호출 fallback",
                     len(sessions), "✓" if covers_all else "✗")
        sessions = [(1, 1, total_pages)]

    t0 = time.time()

    # 세션별 호출을 ThreadPoolExecutor로 병렬 실행.
    # MLX-LM 서버는 요청 큐잉이지만, Anthropic은 병렬 배칭.
    # 동시 호출이어도 MLX는 순차 처리되지만 클라이언트 대기시간은 같음.
    def _call_for_session(args):
        sn, ps, pe = args
        doc_text = _page_summary_range(
            elements, ps, pe, total_pages=total_pages)
        if not doc_text:
            return sn, None
        raw = _llm_boundaries_request_sync(
            doc_text, total_pages, page_count_hint=(pe - ps + 1))
        return sn, raw

    max_workers = min(len(sessions), 4)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers) as pool:
        futures = {
            pool.submit(_call_for_session, (sn, ps, pe)): (sn, ps, pe)
            for sn, ps, pe in sessions
        }
        session_results: dict[int, tuple[int, int, Optional[str]]] = {}
        for fut in concurrent.futures.as_completed(futures):
            sn_ps_pe = futures[fut]
            try:
                _, raw = fut.result()
            except Exception as e:
                logger.info("세션%d LLM 호출 예외 — 전체 fallback: %s",
                             sn_ps_pe[0], e)
                return None
            session_results[sn_ps_pe[0]] = (*sn_ps_pe[1:], raw)

    all_bdy: list[dict] = []
    for sn, ps, pe in sessions:
        ps_, pe_, raw = session_results[sn][0], session_results[sn][1], session_results[sn][2]
        if raw is None:
            logger.info("세션%d LLM 호출 실패 — 전체 fallback", sn)
            return None
        section_bdy = _parse_jsonl(raw)
        section_bdy = [
            b for b in section_bdy
            if isinstance(b.get("page_start"), int)
            and ps <= b["page_start"] <= pe
        ]
        assigned_sn = sn if treat_as_multi_session else 1
        for b in section_bdy:
            b["session"] = assigned_sn
        all_bdy.extend(section_bdy)
        logger.info("세션%d(블록): %d개 경계", sn, len(section_bdy))

    bdy = all_bdy
    # 1. 명백한 단일 오류 자동 수정 (범위 밖, start>end)
    cleaned: list[dict] = []
    fixed = 0
    for b in bdy:
        ps = b.get("page_start")
        pe = b.get("page_end", ps)
        if not isinstance(ps, int) or not isinstance(pe, int):
            fixed += 1
            continue  # 타입 오류 → 제거
        if ps < 1 or ps > total_pages:
            fixed += 1
            continue  # 범위 밖 → 제거
        if pe < ps or pe > total_pages:
            # start는 유효하나 end가 이상 → start로 보정 (단일 페이지 토픽)
            b["page_end"] = ps
            fixed += 1
        cleaned.append(b)
    if fixed:
        logger.info("LLM 경계 %d개 단일 오류 자동 수정", fixed)
    bdy = cleaned

    # 2. 세션-전체 hallucination 자동 제거
    before = len(bdy)
    bdy = _remove_containing_boundaries(bdy)
    removed = before - len(bdy)
    if removed:
        logger.info("LLM 중첩 경계 %d개 자동 제거 (세션-전체 hallucination)", removed)

    # 3. 중복 제목 병합 (같은 세션 내)
    before = len(bdy)
    bdy = _merge_duplicate_titles(bdy)
    merged = before - len(bdy)
    if merged:
        logger.info("LLM 중복 제목 %d개 병합", merged)

    ok, reason = _validate_llm_boundaries(bdy, total_pages)
    if not ok:
        logger.info("LLM 경계 탐지 검증 실패 — 규칙 기반 fallback: %s", reason)
        return None

    # 정규화: detect_boundaries_v2 호환 포맷으로 매핑
    # 세션 오름차순 + 세션 내 page_start 오름차순으로 전역 순번 재부여
    bdy.sort(key=lambda x: (int(x.get("session", 1)),
                            int(x.get("page_start", 0))))
    warnings: list[str] = [f"LLM-first 경계 탐지 사용 ({len(bdy)}개, {time.time()-t0:.1f}s)"]

    # 세션 라벨 통합: 모든 경계가 동일한 1개 세션에 속해 있고 detect_sessions도
    # 단일/약한 감지였다면(세션수 == 1) session=1로 통일.
    # 아니면 LLM이 출력한 session 값을 존중하되, 비정상 값(1~4 밖)은 1로 보정.
    session_values = sorted({int(b.get("session", 1)) for b in bdy})
    collapse_single = (len(session_values) == 1)

    boundaries = []
    for i, b in enumerate(bdy):
        ps = int(b["page_start"])
        pe = int(b.get("page_end", ps))
        session_q = int(b.get("num", 0)) or (i + 1)
        raw_sess = int(b.get("session", 1))
        sess = 1 if collapse_single else (raw_sess if 1 <= raw_sess <= 4 else 1)
        # 제목 정제: 번호 접두사 제거, 40자 이내
        title = _strip_title_prefix(str(b.get("title", "")))
        boundaries.append({
            "num": i + 1,  # 전역 연속 번호
            "title": title[:80] or f"토픽_p{ps}",
            "page": ps, "page_start": ps, "page_end": pe,
            "session": sess,
            "session_q": session_q,
            "fmt": "llm",
            "confidence": 0.95,  # LLM 결과는 높은 신뢰
        })
    return boundaries, warnings


# ─── A. 제목 + 키워드 추출 ──────────────────────────────────────

_TITLE_SYSTEM = """\
당신은 기술사 시험 답안지 분석 전문가입니다.
주어진 텍스트에서 기술사 토픽(문제)의 핵심 제목과 키워드를 추출하세요.

규칙:
- title: 토픽의 핵심 주제명을 간결하게 (40자 이내 명사구). "~에 대하여 설명하시오" 등 출제 지시문 제거
- keywords: 해당 토픽의 핵심 기술 키워드 5~10개 (영문 약어 포함)
- "I. 개요", "1. 정의" 같은 목차 번호가 아닌, 실제 주제 키워드를 추출
- JSON 형식: {"title": "추출된 제목", "keywords": ["키워드1", "키워드2", ...]}
- 설명이나 부연 없이 JSON만 반환"""


async def _extract_title_and_keywords(
    client, sem: asyncio.Semaphore,
    text: str, boundary_idx: int,
) -> tuple[int, str, list[str]]:
    """단일 boundary의 제목 + 키워드를 LLM으로 추출."""
    try:
        data = await _call_llm(client, sem, _TITLE_SYSTEM, text)
        if data:
            title = _strip_title_prefix(data.get("title", ""))
            kw = data.get("keywords", [])
            keywords = [str(k).strip() for k in kw if k] if isinstance(kw, list) else []
            # 키워드 내 번호 접두사도 정리 (가끔 "1. 토픽명"이 키워드로 섞여 들어옴)
            keywords = [_strip_title_prefix(k) for k in keywords]
            keywords = [k for k in keywords if k]
            return boundary_idx, title, keywords
    except Exception as e:
        logger.warning("title/keyword extraction failed [%d]: %s",
                       boundary_idx, e)
    return boundary_idx, "", []


# ─── B. 경계 검증 ────────────────────────────────────────────────

_VERIFY_SYSTEM = """\
당신은 기술사 시험 답안지 분석 전문가입니다.
두 페이지의 텍스트가 주어집니다. 같은 토픽의 연속인지, 다른 토픽의 시작인지 판단하세요.

판단 기준:
- 번호(1. 2. 3.)가 처음부터 다시 시작하면 → 새 토픽
- "I. 개요"가 새로 나오면 → 새 토픽
- 주제 도메인이 완전히 바뀌면 → 새 토픽 (예: 보안→네트워크)
- 내용이 자연스럽게 이어지면 → 같은 토픽

JSON 형식: {"same_topic": true/false, "reason": "한 줄 사유"}"""


async def _verify_one_boundary(
    client, sem: asyncio.Semaphore,
    end_text: str, next_text: str, boundary_idx: int,
) -> tuple[int, Optional[bool]]:
    """경계가 유효한지 검증. True=같은토픽(경계제거), False=다른토픽(유지)."""
    prompt = (
        f"=== 페이지 A (현재 토픽 마지막) ===\n{end_text}\n\n"
        f"=== 페이지 B (다음 토픽 시작 후보) ===\n{next_text}"
    )
    try:
        data = await _call_llm(client, sem, _VERIFY_SYSTEM, prompt,
                               max_tokens=100)
        if data:
            return boundary_idx, data.get("same_topic")
    except Exception as e:
        logger.warning("boundary verify failed [%d]: %s", boundary_idx, e)
    return boundary_idx, None


# ─── C. 저신뢰 구간 재판정 ───────────────────────────────────────

_RECLASSIFY_SYSTEM = """\
당신은 기술사 시험 답안지 분석 전문가입니다.
여러 페이지에 걸친 텍스트가 주어집니다. 이 구간에 2개 이상의 서로 다른 토픽이 포함되어 있는지 판단하고,
토픽 경계가 있다면 새 토픽이 시작되는 페이지 번호와 제목을 찾아주세요.

판단 기준:
- 번호(1. 2. 3.)가 1부터 다시 시작 → 새 토픽 시작
- "I. 개요"가 다시 등장 → 새 토픽 시작
- 주제 도메인이 전혀 다른 내용으로 전환 → 새 토픽 시작
- ★ 또는 별표 패턴이 새로 등장 → 새 토픽 시작

JSON 형식:
{"boundaries": [{"page": 페이지번호, "title": "토픽 제목"}]}
경계가 없으면: {"boundaries": []}"""


async def _reclassify_section(
    client, sem: asyncio.Semaphore,
    text: str, section_idx: int,
) -> tuple[int, list[dict]]:
    """저신뢰 구간에서 경계 재탐색."""
    try:
        data = await _call_llm(client, sem, _RECLASSIFY_SYSTEM, text)
        if data:
            return section_idx, data.get("boundaries", [])
    except Exception as e:
        logger.warning("reclassify failed [%d]: %s", section_idx, e)
    return section_idx, []


# ─── 통합 enhance 함수 ───────────────────────────────────────────

@dataclass
class EnhanceResult:
    """LLM 검증 결과."""
    boundaries: list[dict]
    titles_updated: int
    boundaries_removed: int
    boundaries_added: int
    skipped: bool  # API 키 없어서 스킵됨


def _skip_result(boundaries: list[dict], skipped: bool = False) -> EnhanceResult:
    """변경 없이 원본 반환하는 헬퍼."""
    return EnhanceResult(
        boundaries=boundaries,
        titles_updated=0, boundaries_removed=0,
        boundaries_added=0, skipped=skipped,
    )


async def enhance_boundaries(
    boundaries: list[dict],
    elements: list,
    total_pages: int,
) -> EnhanceResult:
    """
    규칙 기반으로 생성된 boundaries를 LLM으로 검증·보정.

    Args:
        boundaries: detect_boundaries_v2 결과 (dict 리스트)
        elements: kordoc 파싱 결과
        total_pages: 총 페이지 수

    Returns:
        EnhanceResult with 보정된 boundaries
    """
    if not is_available():
        logger.info("ANTHROPIC_API_KEY not set — LLM verification skipped")
        return _skip_result(boundaries, skipped=True)

    if not boundaries:
        return _skip_result(boundaries)

    client = _get_client()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    titles_updated = 0
    boundaries_removed = 0
    boundaries_added = 0

    # ── A. 제목 + 키워드 추출 (문제지 제외) ──────────────────────
    title_tasks = []
    for i, b in enumerate(boundaries):
        if "문제지" in b.get("title", ""):
            continue
        text = _extract_text(elements, b["page_start"], b["page_end"],
                             max_chars=1500, tag_pages=False)
        if text.strip():
            title_tasks.append(
                _extract_title_and_keywords(client, sem, text, i))

    # ── B. 경계 검증 (confidence < 0.7) ──────────────────────────
    verify_tasks = []
    for i, b in enumerate(boundaries):
        if b.get("confidence", 1.0) >= 0.7 or i == 0:
            continue
        prev = boundaries[i - 1]
        end_text = _extract_text(elements, prev["page_end"], prev["page_end"],
                                 max_chars=800, tag_pages=False)
        next_text = _extract_text(elements, b["page_start"], b["page_start"],
                                  max_chars=800, tag_pages=False)
        if end_text.strip() and next_text.strip():
            verify_tasks.append(
                _verify_one_boundary(client, sem, end_text, next_text, i))

    # ── C. 저신뢰 구간 재판정 (confidence < 0.5 + 4p 이상) ──────
    reclass_tasks = []
    reclass_map: dict[int, int] = {}  # section_idx → boundary_idx
    for i, b in enumerate(boundaries):
        span = b["page_end"] - b["page_start"] + 1
        if b.get("confidence", 1.0) >= 0.5 or span < 4:
            continue
        text = _extract_text(elements, b["page_start"], b["page_end"])
        if text.strip():
            sid = len(reclass_tasks)
            reclass_map[sid] = i
            reclass_tasks.append(
                _reclassify_section(client, sem, text, sid))

    # ── 병렬 실행 ────────────────────────────────────────────────
    all_tasks = title_tasks + verify_tasks + reclass_tasks
    if not all_tasks:
        return _skip_result(boundaries)

    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    n_t, n_v = len(title_tasks), len(verify_tasks)
    title_results = results[:n_t]
    verify_results = results[n_t:n_t + n_v]
    reclass_results = results[n_t + n_v:]

    # ── A 적용: 제목 + 키워드 업데이트 ───────────────────────────
    for r in title_results:
        if isinstance(r, Exception):
            continue
        idx, title, keywords = r
        if not (0 <= idx < len(boundaries)):
            continue
        if title:
            old = boundaries[idx].get("title", "")
            boundaries[idx]["title"] = title
            logger.info("title updated: Q%d '%s' → '%s'",
                        boundaries[idx]["num"], old, title)
            titles_updated += 1
        if keywords:
            boundaries[idx]["keywords"] = keywords

    # ── B 적용: 잘못된 경계 제거 ─────────────────────────────────
    remove_indices: set[int] = set()
    for r in verify_results:
        if isinstance(r, Exception):
            continue
        idx, same_topic = r
        if (same_topic is True and 0 <= idx < len(boundaries)
                and boundaries[idx].get("confidence", 1.0) < 0.6):
            remove_indices.add(idx)
            logger.info("boundary removed: Q%d p%d (conf=%.2f)",
                        boundaries[idx]["num"],
                        boundaries[idx]["page_start"],
                        boundaries[idx]["confidence"])

    # ── C 적용: 새 경계 삽입 ─────────────────────────────────────
    insert_boundaries: list[dict] = []
    for r in reclass_results:
        if isinstance(r, Exception):
            continue
        section_idx, new_bounds = r
        orig_idx = reclass_map.get(section_idx)
        if orig_idx is None or not new_bounds:
            continue
        orig = boundaries[orig_idx]
        for nb in new_bounds:
            pg = nb.get("page")
            if not (isinstance(pg, int) and orig["page_start"] < pg <= orig["page_end"]):
                continue
            insert_boundaries.append({
                "num": 0, "title": nb.get("title", "") or f"토픽_p{pg}",
                "page": pg, "page_start": pg, "page_end": orig["page_end"],
                "fmt": "llm_reclassify",
                "session": orig.get("session", 0), "session_q": 0,
                "confidence": 0.6,
            })
            orig["page_end"] = pg - 1
            boundaries_added += 1
            logger.info("boundary added: p%d '%s'", pg, nb.get("title", ""))

    # ── 제거 + 삽입 반영 ─────────────────────────────────────────
    if remove_indices:
        for idx in sorted(remove_indices, reverse=True):
            if idx > 0:
                boundaries[idx - 1]["page_end"] = boundaries[idx]["page_end"]
            boundaries.pop(idx)
            boundaries_removed += 1

    if insert_boundaries:
        boundaries.extend(insert_boundaries)

    # 페이지 순서 정렬 + 번호 재부여
    boundaries.sort(key=lambda b: b["page_start"])
    for i, b in enumerate(boundaries):
        b["num"] = i + 1

    return EnhanceResult(
        boundaries=boundaries,
        titles_updated=titles_updated,
        boundaries_removed=boundaries_removed,
        boundaries_added=boundaries_added,
        skipped=False,
    )


# ─── 동기 래퍼 (threading 환경용) ─────────────────────────────────

def enhance_boundaries_sync(
    boundaries: list[dict],
    elements: list,
    total_pages: int,
) -> EnhanceResult:
    """동기 환경에서 호출 가능한 래퍼. 내부적으로 asyncio 루프를 생성."""
    coro = enhance_boundaries(boundaries, elements, total_pages)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # 이미 이벤트 루프 실행 중 → 별도 스레드에서 asyncio.run
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=_SYNC_TIMEOUT)
