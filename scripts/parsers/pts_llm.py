"""PTS LLM 폴백 — PureTopicSegmenter 가 ok=False 일 때 호출되는 LLM 기반 분할기.

- Anthropic Claude Haiku 4.5 사용 (저렴 + 빠름)
- 페이지별 본문 첫 N줄을 입력 → 토픽 시작 페이지 라벨링
- 비용: PDF당 약 $0.01~0.02 (100p 기준)
- 옵트인 — 사용자가 명시 활성화한 케이스만 호출
- 시스템 프롬프트는 prompt caching 활용 (cache_control)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import fitz

from .base import ParseResult, Topic, derive_round_id, sanitize_filename

# 한 페이지당 입력에 포함하는 라인 수 (헤더 노이즈 흡수 위해 넉넉히)
PAGE_PREVIEW_LINES = 10
# 한 페이지당 입력 라인 최대 길이
LINE_MAX_CHARS = 100
# 응답 토큰 — 토픽 40개 × 100토큰 = 4000 토큰 + 여유
LLM_MAX_TOKENS = 8192
# 기본 모델 (정확도 우선)
LLM_MODEL = "claude-sonnet-4-5-20250929"


SYSTEM_PROMPT = """당신은 한국 IT 기술사 시험(정보관리기술사/컴퓨터시스템응용기술사) 해설집 PDF 분할 도우미입니다.

# 시험 구조 (학원 무관, 시험 자체 메타)
- 본시험 기출풀이: 1교시 13개 + 2~4교시 각 6개 = 총 31개 토픽
- KPC 모의고사: 1교시 16개 + 2~4교시 각 8개 = 총 40개 토픽
- ITPE 모의고사: 1교시 14개 + 2~4교시 각 7개 = 총 35개 토픽
- 1교시 단답형: 한 토픽당 1~3 페이지
- 2~4교시 서술형: 한 토픽당 3~6 페이지

# 토픽 시작 페이지 식별 신호 (학원별 가변)
- 본문 시작에 '문제', '문 제', '문', 'N.', 'N. <제목>', '제 N. <제목>' 등 등장
- 시험 메타 라벨 직후 등장: '출제영역', '도메인', '난이도', '★★★', '(상/중/하)', '키워드', '출제배경'
- 동기회: 'N 교시 / M 번 / Ⅰ.' 패턴
- 본문 깊숙한 'N. <섹션>' (가, 나, 다 항목)은 sub-section — 토픽 시작 아님

# 제외 페이지
- '[관리선택]', '[응용선택]', '제 N 교시(시험시간:)' 시험지 표지
- Copyright 한 줄만 있는 답안 작성용 빈 페이지
- 학원 광고/브랜딩 페이지

# 작업
입력: 페이지별 본문 첫 N줄
출력: 토픽 시작 페이지만 JSON 배열로

[
  {"page": <1-indexed>, "session": <1~4>, "num": <문제 번호>, "title": "<짧은 제목 50자 이내>"},
  ...
]

# 중요 규칙
- 모든 토픽을 빠짐없이 추출 (본시험 31, KPC모의 40, ITPE모의 35 정도가 정상)
- 같은 교시 안에서 num은 단조 증가 (1, 2, 3, ...)
- 새 교시 시작 시 num은 1로 리셋
- 본시험은 정관(13개)+컴응(13개)=26개 또는 정관 13개만 — 한 PDF에 같은 번호 두 번 나올 수 있음
- title 은 학원 광고/슬로건이 아닌 시험 토픽 제목만 (예: 'AI RMF', '자기회귀모형', '데이터 가치평가')
- 응답은 JSON 배열만. ```json fence 또는 그냥 배열 — 다른 설명 없이
"""


def _read_page_previews(pdf_path: Path, max_pages: int = 200) -> tuple[int, list[str]]:
    """페이지별 본문 첫 N줄을 (page_idx, "...") 튜플로 모아 한 텍스트로 합침."""
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    lines = []
    for i in range(min(total_pages, max_pages)):
        text = doc.load_page(i).get_text()
        page_lines = [ln.strip() for ln in text.split("\n") if ln.strip()][:PAGE_PREVIEW_LINES]
        # 라인 길이 제한
        page_lines = [ln[:LINE_MAX_CHARS] for ln in page_lines]
        preview = " | ".join(page_lines) if page_lines else "(빈 페이지)"
        lines.append(f"[Page {i+1}] {preview}")
    doc.close()
    return total_pages, lines


def _parse_llm_response(text: str) -> list[dict]:
    """LLM 응답에서 JSON 배열 추출. 다양한 형식 흡수 (```json fence, 그냥 array, 등)."""
    # ```json ... ``` 또는 ``` ... ``` fence 제거
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    text = text.strip()
    # JSON array 직접 파싱
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # 첫 [ 와 마지막 ] 사이 추출 시도
    a = text.find("[")
    b = text.rfind("]")
    if a >= 0 and b > a:
        try:
            data = json.loads(text[a:b + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []


def parse_pts_llm(pdf_path: Path, model: Optional[str] = None) -> ParseResult:
    """LLM 기반 토픽 분할. ANTHROPIC_API_KEY 필요."""
    if not pdf_path.exists():
        return ParseResult(ok=False, engine="pts_llm", reason=f"파일 없음: {pdf_path}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ParseResult(ok=False, engine="pts_llm", reason="ANTHROPIC_API_KEY 미설정")

    total_pages, page_lines = _read_page_previews(pdf_path)
    if total_pages == 0:
        return ParseResult(ok=False, engine="pts_llm", reason="빈 PDF")

    user_input = (
        f"PDF 총 페이지: {total_pages}\n\n"
        + "\n".join(page_lines)
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model or LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[
                {"role": "user", "content": user_input},
            ],
        )
        # 응답 텍스트 결합
        raw = "".join(
            block.text for block in msg.content if hasattr(block, "text")
        )
    except Exception as e:
        return ParseResult(ok=False, engine="pts_llm", reason=f"LLM 호출 실패: {str(e)[:120]}")

    parsed = _parse_llm_response(raw)
    if not parsed:
        return ParseResult(
            ok=False, engine="pts_llm",
            reason=f"LLM 응답 파싱 실패: {raw[:120]}",
        )

    # ParseResult.topics 변환
    parsed_sorted = sorted(parsed, key=lambda x: x.get("page", 0))
    topics: list[Topic] = []
    for idx, item in enumerate(parsed_sorted):
        page = int(item.get("page", 0))
        if page <= 0 or page > total_pages:
            continue
        # 페이지 끝 = 다음 토픽 시작 직전
        next_page = (
            int(parsed_sorted[idx + 1].get("page", total_pages + 1))
            if idx + 1 < len(parsed_sorted)
            else total_pages + 1
        )
        ps = page - 1  # 0-indexed
        pe = max(ps, next_page - 2)  # next_page - 1 - 1
        topics.append(Topic.from_range(
            session=int(item.get("session", 0)),
            num=int(item.get("num", 0)),
            title=str(item.get("title", "")),
            ps=ps, pe=pe,
        ))

    if not topics:
        return ParseResult(ok=False, engine="pts_llm", reason="LLM이 토픽 0건 반환")

    round_id = derive_round_id(pdf_path)
    counts = {s: sum(1 for t in topics if t.session == s) for s in [1, 2, 3, 4]}
    summary = (
        f"{round_id} (LLM): {len(topics)}건 "
        f"(M1={counts[1]}, M2={counts[2]}, M3={counts[3]}, M4={counts[4]})"
    )

    return ParseResult(
        ok=True,
        engine="pts_llm",
        round_id=round_id,
        topics=topics,
        summary=summary,
    )


def split_pts_llm(pdf_path: Path, out_dir: Path, model: Optional[str] = None) -> ParseResult:
    """parse_pts_llm + 분할 PDF 산출."""
    result = parse_pts_llm(pdf_path, model=model)
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


def llm_available() -> bool:
    """LLM 폴백 사용 가능한지."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
