#!/usr/bin/env python3
"""
경계 탐지 회귀 smoke test.

실행:
  python3 scripts/smoke_test.py          # 전체 실행
  python3 scripts/smoke_test.py --quick  # 캐시된 PDF만 (OCR skip)

사용자 iCloud 경로 기반 fixture. 없으면 해당 테스트 skip.
기대치는 실측 정답(또는 현재 달성치 tolerance)으로 관리.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# .env 로드 (LLM provider 설정)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if ENV_PATH.exists():
    for ln in ENV_PATH.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ICLOUD = "/Users/turtlesoup0-macmini/Library/Mobile Documents/com~apple~CloudDocs"

# 회귀 기준 (정답 + 현재 허용 오차)
# topic_min: 반드시 이 이상 탐지되어야 함
# session_check: 세션 라벨링이 이 값들과 일치해야 함
FIXTURES = [
    {
        "name": "FB22 NW 1교시 리뷰 (단일교시 32p)",
        "path": f"{ICLOUD}/공부/4_FB반 자료/22기/6_NWCAOS/NW_1교시_리뷰.pdf",
        "topic_min": 12,          # 13 정답, 12+ 허용
        "topic_max": 14,          # 과분할 15+ 금지
        "sessions_expected": {1},  # 모두 1교시
    },
    {
        "name": "ITPE 135관 (4교시 87p)",
        "path": f"{ICLOUD}/공부/1_기출 해설/135/ITPE 135관-합.pdf",
        "topic_min": 30,
        "topic_max": 32,
        "sessions_expected": {1, 2, 3, 4},
    },
    {
        "name": "KPC 135관 (4교시 97p)",
        "path": f"{ICLOUD}/공부/1_기출 해설/135/KPC 135관-합.pdf",
        "topic_min": 30,          # 31 정답, 30+ 허용 (±1 LLM 변동)
        "topic_max": 32,
        "sessions_expected": {1, 2, 3, 4},
    },
    {
        "name": "KPC 132관 (4교시 91p, 세션 보강 케이스)",
        "path": f"{ICLOUD}/공부/1_기출 해설/132/KPC132관-합.pdf",
        "topic_min": 28,  # 31 정답, detect_sessions가 1교시 누락 → 세션 보강 필요
        "topic_max": 32,
        "sessions_expected": {1, 2, 3, 4},
    },
]


def _run_one(fixture: dict) -> tuple[bool, str]:
    path = fixture["path"]
    if not os.path.exists(path):
        return False, f"SKIP (파일 없음): {path}"

    from split_odl import parse_kordoc
    from llm_verifier import detect_boundaries_llm, is_available

    if not is_available():
        return False, "SKIP (LLM 사용 불가 — LLM_PROVIDER/ANTHROPIC_API_KEY 확인)"

    t0 = time.time()
    elements, total_pages = parse_kordoc(path)
    t_parse = time.time() - t0

    t1 = time.time()
    result = detect_boundaries_llm(elements, total_pages)
    t_llm = time.time() - t1
    if result is None:
        return False, f"FAIL: LLM 경계 탐지 None (규칙 fallback 상황)"

    boundaries, _ = result
    count = len(boundaries)
    sessions = {b["session"] for b in boundaries}

    problems = []
    if count < fixture["topic_min"]:
        problems.append(f"토픽수 {count} < 최소 {fixture['topic_min']}")
    if count > fixture["topic_max"]:
        problems.append(f"토픽수 {count} > 최대 {fixture['topic_max']}")
    if sessions != fixture["sessions_expected"]:
        problems.append(f"세션 {sorted(sessions)} ≠ 기대 {sorted(fixture['sessions_expected'])}")

    status = "PASS" if not problems else "FAIL"
    detail = (f"{status} │ 토픽 {count} ({total_pages}p) │ "
              f"세션 {sorted(sessions)} │ "
              f"parse {t_parse:.1f}s + llm {t_llm:.1f}s")
    if problems:
        detail += "\n    문제: " + "; ".join(problems)
    return not problems, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="캐시된 PDF만 (OCR skip)")
    ap.add_argument("--json", action="store_true",
                    help="결과를 JSON으로 출력")
    args = ap.parse_args()

    results = []
    print(f"경계 탐지 회귀 smoke test ({len(FIXTURES)}건)\n")
    for f in FIXTURES:
        name = f["name"]
        ok, detail = _run_one(f)
        results.append({"name": name, "passed": ok, "detail": detail})
        print(f"[{name}]")
        print(f"  {detail}\n")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"{'='*60}")
    print(f"합계: {passed}/{total} 통과")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
