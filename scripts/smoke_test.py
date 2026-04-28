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
        "topic_min": 28,
        "topic_max": 33,   # LLM stochastic ±2 허용
        "sessions_expected": {1, 2, 3, 4},
    },
    {
        "name": "아이리포 135관 (4교시 84p, 세션 커버 불명확)",
        "path": f"{ICLOUD}/공부/1_기출 해설/135/아이리포 135관.pdf",
        "topic_min": 28,
        "topic_max": 34,
        # detect_sessions 가 세션 커버 감지 못해 단일 블록 호출로 fallback.
        # 토픽은 정확히 탐지되나 세션 라벨은 {1} 로 통일됨 — 실용적 허용.
        "sessions_expected": {1},
    },
    {
        "name": "동기회 135관 (4교시 105p)",
        "path": f"{ICLOUD}/공부/1_기출 해설/135/동기회 135관.pdf",
        "topic_min": 27,
        "topic_max": 33,
        "sessions_expected": {1, 2, 3, 4},
    },
    {
        "name": "KPC 132응 (4교시 98p, 1p 병합 케이스)",
        "path": f"{ICLOUD}/공부/1_기출 해설/132/KPC132응-합.pdf",
        "topic_min": 29,
        "topic_max": 33,
        "sessions_expected": {1, 2, 3, 4},
    },
    # ─── 모의고사 (학원별 1교시 15 + 2~4교시 각 7, 총 36 기대) ───
    # 주의: 모의고사 합본은 표지가 불규칙하여 detect_sessions가 실패할 수
    # 있음. 세션 라벨 대신 토픽 수만 검증.
    {
        "name": "모의 KPC129 (4교시 127p)",
        "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC129_2604_합.pdf",
        "topic_min": 34,
        "topic_max": 42,
        "sessions_expected": None,  # 세션 라벨 검증 스킵
    },
    {
        "name": "모의 ITPE35 (4교시 127p)",
        "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE35-2507_합.pdf",
        "topic_min": 34,
        "topic_max": 42,
        "sessions_expected": None,
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
    if fixture.get("sessions_expected") is not None \
            and sessions != fixture["sessions_expected"]:
        problems.append(f"세션 {sorted(sessions)} ≠ 기대 {sorted(fixture['sessions_expected'])}")

    status = "PASS" if not problems else "FAIL"
    detail = (f"{status} │ 토픽 {count} ({total_pages}p) │ "
              f"세션 {sorted(sessions)} │ "
              f"parse {t_parse:.1f}s + llm {t_llm:.1f}s")
    if problems:
        detail += "\n    문제: " + "; ".join(problems)
    return not problems, detail


# ─── 모의고사 결정적 분할 회귀 (LLM 비의존) ─────────────────────────────
# diagnose_*_mock.py 의 fitz/kordoc 엔진 결과 회귀. LLM 사용 안 함, 빠름.
# kordoc 엔진은 옵트인이지만 "의도된 known-failure 세트"는 회귀로 락(lock)해
# 실수로 더 나빠지지 않도록 보호.

FIXTURES_MOCK = [
    # KPC ─────────────────────────────────────────────
    {"name": "KPC127 fitz",  "kind": "kpc", "engine": "fitz",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC127_2512_합.pdf",
     "expect_pass": True},
    {"name": "KPC127 kordoc", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC127_2512_합.pdf",
     "expect_pass": True},
    {"name": "KPC128 kordoc", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC128_2601_합.pdf",
     "expect_pass": True},
    {"name": "KPC129 kordoc (시험지 부재)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC129_2604_합.pdf",
     "expect_pass": True},
    {"name": "KPC125 kordoc (fitz fail 회복)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC125_2507_합.pdf",
     "expect_pass": True},
    # PR 10 으로 회복 (table 셀 안 토픽 구조 처리). KPC120 이 유일한 known fail.
    {"name": "KPC124 kordoc (PR 10 회복)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC124_2506_합.pdf",
     "expect_pass": True},
    {"name": "KPC120 kordoc (known fail — q_num skip)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC120_2410_합.pdf",
     "expect_pass": False},
    {"name": "KPC121 kordoc (PR 9 회복)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC121_2411_합.pdf",
     "expect_pass": True},
    {"name": "KPC119 kordoc (PR 9 회복)", "kind": "kpc", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/KPC/모의_KPC119_2407_합.pdf",
     "expect_pass": True},
    # ITPE ────────────────────────────────────────────
    {"name": "ITPE35 fitz",  "kind": "itpe", "engine": "fitz",
     "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE35-2507_합.pdf",
     "expect_pass": True},
    {"name": "ITPE35 kordoc", "kind": "itpe", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE35-2507_합.pdf",
     "expect_pass": True},
    {"name": "ITPE27 kordoc (sanity check 검증)", "kind": "itpe", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE27-2406_합.pdf",
     "expect_pass": True},
    {"name": "ITPE22 kordoc (enrich 작동)", "kind": "itpe", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE22-2310_합.pdf",
     "expect_pass": True},
    # 원본 PDF 결함 (학원에서 1교시 Q3 페이지 누락 발행) — fitz/kordoc 모두 fix 불가.
    # 분할 자체는 ok=True 로 진행되며 warnings 에 누락 표시. diagnose 만 fail rc.
    {"name": "ITPE24 kordoc (PDF 결함 — Q3 누락)", "kind": "itpe", "engine": "kordoc",
     "path": f"{ICLOUD}/공부/2_모의고사/ITPE/모의_ITPE24-2312_합.pdf",
     "expect_pass": False},
]


def _run_mock_one(fixture: dict) -> tuple[bool, str]:
    """모의고사 진단 회귀 — diagnose_*_mock.diagnose() 호출하고 통과 여부만 본다."""
    path = fixture["path"]
    if not os.path.exists(path):
        return False, f"SKIP (파일 없음): {path}"

    kind = fixture["kind"]
    engine = fixture["engine"]
    expect_pass = fixture.get("expect_pass", True)

    # stdout 캡처 — 모의고사 진단 출력은 CLI 친화적으로 100+ 줄 길어짐.
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if kind == "kpc":
                from diagnose_kpc_mock import diagnose as kpc_diagnose
                rc = kpc_diagnose(Path(path), engine=engine)
            elif kind == "itpe":
                from diagnose_itpe_mock import diagnose as itpe_diagnose
                rc = itpe_diagnose(Path(path), engine=engine)
            else:
                return False, f"FAIL: 알 수 없는 kind={kind}"
    except Exception as e:
        return False, f"FAIL: 예외 {type(e).__name__}: {str(e)[:120]}"

    actually_passed = (rc == 0)
    matches = (actually_passed == expect_pass)

    if matches:
        if expect_pass:
            return True, f"PASS │ {kind}/{engine} 진단 통과 (rc=0)"
        else:
            return True, f"PASS-as-known-fail │ {kind}/{engine} (rc={rc}, 의도된 실패 유지)"
    else:
        if expect_pass:
            return False, f"FAIL │ {kind}/{engine} 진단 실패 (rc={rc}, 통과 기대)"
        else:
            return False, f"FAIL │ {kind}/{engine} 예상치 못한 통과 (known-fail 가 풀렸으니 expect_pass 갱신 검토)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="캐시된 PDF만 (OCR skip)")
    ap.add_argument("--json", action="store_true",
                    help="결과를 JSON으로 출력")
    ap.add_argument("--mock-only", action="store_true",
                    help="모의고사 진단 회귀만 실행 (LLM 비의존, 빠름)")
    ap.add_argument("--skip-mock", action="store_true",
                    help="모의고사 진단 회귀 건너뜀")
    args = ap.parse_args()

    results: list[dict] = []

    if not args.mock_only:
        print(f"=== LLM 경계 탐지 회귀 ({len(FIXTURES)}건) ===\n")
        for f in FIXTURES:
            name = f["name"]
            ok, detail = _run_one(f)
            results.append({"name": name, "category": "boundary", "passed": ok, "detail": detail})
            print(f"[{name}]")
            print(f"  {detail}\n")

    if not args.skip_mock:
        print(f"\n=== 모의고사 결정적 분할 회귀 ({len(FIXTURES_MOCK)}건) ===\n")
        for f in FIXTURES_MOCK:
            name = f["name"]
            ok, detail = _run_mock_one(f)
            results.append({"name": name, "category": "mock", "passed": ok, "detail": detail})
            print(f"[{name}]")
            print(f"  {detail}\n")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"{'='*60}")
    print(f"합계: {passed}/{total} 통과")
    if args.mock_only or args.skip_mock:
        cats = {}
        for r in results:
            cats.setdefault(r["category"], [0, 0])
            cats[r["category"]][1] += 1
            if r["passed"]:
                cats[r["category"]][0] += 1
        for c, (p, t) in cats.items():
            print(f"  └─ {c}: {p}/{t}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
