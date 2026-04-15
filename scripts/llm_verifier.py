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
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── 설정 ────────────────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"
MAX_CONCURRENT = 10  # 동시 API 호출 수 제한
_SYNC_TIMEOUT = 120  # enhance_boundaries_sync 타임아웃(초)

# 싱글턴 클라이언트 캐시
_client_cache: Any = None


def is_available() -> bool:
    """LLM 검증 사용 가능 여부 (호출 시점에 환경변수 확인)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))


def _get_client():
    """Lazy import + 싱글턴 클라이언트 반환."""
    global _client_cache
    if _client_cache is None:
        import anthropic
        _client_cache = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"])
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
            title = data.get("title", "").strip()
            kw = data.get("keywords", [])
            keywords = [str(k).strip() for k in kw if k] if isinstance(kw, list) else []
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
