#!/usr/bin/env python3
"""
FB반 자료 분석 스크립트
- 기출 적중률 분석 (FB반 → 137회/138회)
- 과목별 토픽 분포
- 미출제 토픽 관리
- 기수별 학습 진화 분석
- 학습 갭 분석
"""
import json, os, re, sys
from collections import Counter, defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "data")

# ─────────────────────────────────────────
# 1. 데이터 로드
# ─────────────────────────────────────────
def load_json(name):
    path = os.path.join(DATA_DIR, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_all():
    topics = load_json("topics.json")
    e137 = load_json("exam137_report.json")
    e138 = load_json("exam138_report.json")
    return topics, e137, e138


# ─────────────────────────────────────────
# 2. 기출 문제 중복 제거
# ─────────────────────────────────────────
def dedup_exam_questions(exam_report):
    """source별 중복 제거 → (exam, session, q_num) 기준 고유 문제 추출"""
    dedup = {}
    for r in exam_report["results"]:
        key = (r["exam"], r["session"], r["q_num"])
        if key not in dedup:
            dedup[key] = r["q_title"]
    return dedup


# ─────────────────────────────────────────
# 3. 매칭 엔진
# ─────────────────────────────────────────

# 137회 고유 문제별 핵심 키워드 (수동 정의 - 정확도 위해)
EXAM_137_KEYWORDS = {
    # 관 1교시
    ("관", 1, 1): {"terms": ["IGP", "EGP", "동적라우팅"], "label": "IGP/EGP 동적 라우팅 프로토콜"},
    ("관", 1, 2): {"terms": ["디지털포렌식", "아트팩트", "FORENSIC"], "label": "디지털 포렌식 아트팩트"},
    ("관", 1, 3): {"terms": ["MODBUS"], "label": "MODBUS 프로토콜"},
    ("관", 1, 4): {"terms": ["암호문공격", "CIPHERTEXTATTACK"], "label": "암호문 공격(Ciphertext Attack)"},
    ("관", 1, 5): {"terms": ["GNN", "GRAPHNEURALNETWORK", "그래프신경망"], "label": "GNN(Graph Neural Network)"},
    ("관", 1, 6): {"terms": ["AI거버넌스", "AIGOVERNANCE"], "label": "AI 거버넌스"},
    ("관", 1, 7): {"terms": ["트랜스포머", "TRANSFORMER", "MOE", "MIXTUREOFEXPERTS"], "label": "트랜스포머/MoE"},
    ("관", 1, 8): {"terms": ["AI신뢰성검인증", "신뢰성검증제도"], "label": "AI 신뢰성 검인증 제도(CAT)"},
    ("관", 1, 9): {"terms": ["AB테스팅", "AB테스트", "ABTESTING"], "label": "A/B 테스팅"},
    ("관", 1, 10): {"terms": ["데이터늪", "DATASWAMP"], "label": "데이터 늪(Data Swamp)"},
    ("관", 1, 11): {"terms": ["역공학", "재공학", "REVERSEENGINEERING", "REENGINEERING"], "label": "소프트웨어 역공학/재공학"},
    ("관", 1, 12): {"terms": ["이진탐색트리", "BINARYSEARCHTREE"], "label": "이진 탐색 트리"},
    ("관", 1, 13): {"terms": ["연관규칙", "ASSOCIATIONRULE"], "label": "데이터마이닝 연관 규칙 분석"},
    # 관 2교시
    ("관", 2, 1): {"terms": ["캐시메모리", "CACHEMEMORY", "캐시일관성", "CACHECOHERENCE"], "label": "캐시메모리"},
    ("관", 2, 2): {"terms": ["운영전환", "전자상거래"], "label": "전자상거래 시스템 운영전환"},
    ("관", 2, 3): {"terms": ["MCP", "MODELCONTEXTPROTOCOL"], "label": "MCP(Model Context Protocol) 보안"},
    ("관", 2, 4): {"terms": ["초거대AI", "AI도입가이드라인"], "label": "공공부문 초거대AI 도입 가이드라인"},
    ("관", 2, 5): {"terms": ["Q5"], "label": "2교시 Q5 (제목 미추출)"},
    ("관", 2, 6): {"terms": ["Q6"], "label": "2교시 Q6 (제목 미추출)"},
    # 관 3교시
    ("관", 3, 1): {"terms": ["스케줄링기법", "프로세스스케줄링"], "label": "운영체제 스케줄링 기법"},
    ("관", 3, 2): {"terms": ["정보시스템감리", "운영감리", "유지보수감리"], "label": "정보시스템 운영/유지보수 감리"},
    ("관", 3, 3): {"terms": ["MULTIREGION", "멀티리전", "재해복구시스템"], "label": "Multi-Region Active-Active 재해복구"},
    ("관", 3, 4): {"terms": ["Q4"], "label": "3교시 Q4 (제목 미추출)"},
    ("관", 3, 5): {"terms": ["Q5"], "label": "3교시 Q5 (제목 미추출)"},
    ("관", 3, 6): {"terms": ["Q6"], "label": "3교시 Q6 (제목 미추출)"},
    # 관 4교시
    ("관", 4, 1): {"terms": ["BPF", "BERKELEYPACKETFILTER"], "label": "BPF 악성코드"},
    ("관", 4, 2): {"terms": ["벡터데이터베이스", "HNSW", "VECTORDATABASE"], "label": "벡터 데이터베이스/HNSW"},
    ("관", 4, 3): {"terms": ["쿠버네티스", "KUBERNETES", "K8S"], "label": "쿠버네티스(Kubernetes)"},
    ("관", 4, 4): {"terms": ["UML", "행위다이어그램"], "label": "UML 행위 다이어그램"},
    ("관", 4, 5): {"terms": ["Q5"], "label": "4교시 Q5 (제목 미추출)"},
    ("관", 4, 6): {"terms": ["대가산정"], "label": "소프트웨어 사업 대가산정"},
}

# 138회 고유 문제별 핵심 키워드
EXAM_138_KEYWORDS = {
    ("관", 1, 1): {"terms": ["AIRMF", "AI위험관리프레임워크"], "label": "AI RMF(Risk Management Framework)"},
    ("관", 1, 2): {"terms": ["프로젝트위험관리", "위험관리프로세스"], "label": "프로젝트 위험관리"},
    ("관", 1, 3): {"terms": ["ISO42001", "IEC42001", "42001"], "label": "ISO/IEC 42001:2023"},
    ("관", 1, 4): {"terms": ["베이즈정리", "베이즈", "BAYES"], "label": "베이즈 정리"},
    ("관", 1, 5): {"terms": ["안면인식", "얼굴인식결제"], "label": "안면인식 결제 서비스"},
    ("관", 1, 6): {"terms": ["테일러링", "TAILORING"], "label": "개발방법론 테일러링"},
    ("관", 1, 7): {"terms": ["자기회귀모형", "AUTOREGRESSIVE", "이동평균모형", "ARIMA"], "label": "자기회귀모형/이동평균모형"},
    ("관", 1, 8): {"terms": ["의사결정나무", "DECISIONTREE"], "label": "분류 알고리즘 의사결정나무"},
    ("관", 1, 9): {"terms": ["제로트러스트", "ZEROTRUST"], "label": "제로 트러스트"},
    ("관", 1, 10): {"terms": ["기능안전", "IEC61508", "FUNCTIONALSAFETY"], "label": "기능안전(IEC 61508)"},
    ("관", 1, 11): {"terms": ["소프트웨어정의", "SDX", "SDV", "소프트웨어정의기술"], "label": "소프트웨어 정의 기술(SDx)"},
    ("관", 1, 12): {"terms": ["디지털트윈", "DIGITALTWIN"], "label": "디지털 트윈"},
    ("관", 1, 13): {"terms": ["CCPA", "GDPR", "개인정보보호법비교"], "label": "개인정보보호법 비교 (CCPA/GDPR)"},
    # 2교시
    ("관", 2, 1): {"terms": ["AIBOM", "AIBILLOFMATERIALS"], "label": "AI-BOM"},
    ("관", 2, 2): {"terms": ["형상관리", "CONFIGURATIONMANAGEMENT"], "label": "형상관리"},
    ("관", 2, 3): {"terms": ["CMMI", "CAPABILITYMATURITYMODEL"], "label": "CMMI 3.0"},
    ("관", 2, 4): {"terms": ["데이터품질관리", "데이터품질"], "label": "데이터 품질관리"},
    ("관", 2, 5): {"terms": ["멀티클라우드", "MULTICLOUD"], "label": "멀티 클라우드"},
    ("관", 2, 6): {"terms": ["자율주행", "AUTONOMOUSDRIVING"], "label": "자율주행"},
    # 3교시
    ("관", 3, 1): {"terms": ["SAAS", "SOFTWAREASASERVICE"], "label": "SaaS"},
    ("관", 3, 2): {"terms": ["블록체인", "BLOCKCHAIN"], "label": "블록체인"},
    ("관", 3, 3): {"terms": ["RAG", "RETRIEVALAUGMENTED"], "label": "RAG(검색 증강 생성)"},
    ("관", 3, 4): {"terms": ["로드밸런싱", "LOADBALANCING"], "label": "로드밸런싱"},
    ("관", 3, 5): {"terms": ["클라우드네이티브", "CLOUDNATIVE"], "label": "전자정부 클라우드 네이티브"},
    ("관", 3, 6): {"terms": ["OSPF", "BGP"], "label": "OSPF/BGP"},
    # 4교시
    ("관", 4, 1): {"terms": ["ISP", "ISMP"], "label": "ISP/ISMP"},
    ("관", 4, 2): {"terms": ["AGENTICAI", "에이전틱"], "label": "Agentic AI"},
    ("관", 4, 3): {"terms": ["마이크로서비스", "MSA", "MICROSERVICE"], "label": "마이크로서비스(MSA)"},
    ("관", 4, 4): {"terms": ["양자컴퓨팅", "QUANTUMCOMPUTING", "양자"], "label": "양자 컴퓨팅"},
    ("관", 4, 5): {"terms": ["온디바이스AI", "ONDEVICEAI"], "label": "온디바이스 AI"},
    ("관", 4, 6): {"terms": ["DEVSECOPS"], "label": "DevSecOps"},
}


def normalize(s):
    """텍스트 정규화: 공백 제거, 대문자화"""
    s = re.sub(r"[\s\-_/·•.,;:()（）「」\[\]{}]", "", s)
    return s.upper()


def match_topic_to_exam(topics, exam_keywords, exam_num):
    """FB 토픽과 기출 문제 키워드 매칭

    매칭 조건:
    - 제목에서 키워드 발견: +3점/키워드 (높은 신뢰도)
    - 본문에서 키워드 발견: +1점/키워드 (낮은 신뢰도)
    - 최소 매칭 기준: 제목에서 1개 이상 OR 본문에서 2개 이상
    - 출제의도 참조는 별도 표시 (매칭 점수에 불포함)
    """
    results = {}  # key → list of matching topics

    for qkey, qinfo in exam_keywords.items():
        terms = qinfo["terms"]
        label = qinfo["label"]
        matches = []

        # Skip Q5/Q6 with no real title
        if all(t in ("Q5", "Q6", "Q4") for t in terms):
            results[qkey] = {"label": label, "matches": [], "skipped": True}
            continue

        for t in topics:
            search_title = normalize(t.get("q_title", ""))
            search_content = normalize(t.get("content", ""))

            # Method 1: 출제의도에 회차 직접 언급 (별도 표시용)
            intent_match = False
            raw_intent = t.get("intent", "")
            if str(exam_num) in raw_intent and "회" in raw_intent:
                for m in re.findall(r"(\d{2,3})\s*(?:회|관리|응용|컴시응)", raw_intent):
                    if int(m) == exam_num:
                        intent_match = True
                        break

            # Method 2: 핵심 키워드 매칭
            title_hits = 0
            content_hits = 0
            for term in terms:
                nterm = normalize(term)
                if len(nterm) < 2:
                    continue
                if nterm in search_title:
                    title_hits += 1
                elif nterm in search_content:
                    content_hits += 1

            # 점수 산출: 제목 매칭 = 3점/건, 본문 매칭 = 1점/건
            score = title_hits * 3 + content_hits * 1

            # 최소 기준: 제목에서 1개 이상 OR 본문에서 2개 이상
            is_valid = title_hits >= 1 or content_hits >= 2
            if not is_valid:
                continue

            matches.append({
                "gen": t["gen"],
                "week": t["week"],
                "title": t["q_title"][:60],
                "score": score,
                "intent_ref": intent_match,
                "title_hits": title_hits,
                "content_hits": content_hits,
            })

        # 점수순 정렬, 상위 5개
        matches.sort(key=lambda x: -x["score"])
        results[qkey] = {"label": label, "matches": matches[:5], "skipped": False}

    return results


def extract_exam_refs_from_intent(topics):
    """출제의도에서 기출 회차 번호 추출"""
    exam_refs = defaultdict(list)  # exam_num → list of topics
    for t in topics:
        intent = t.get("intent", "")
        for m in re.findall(r"(\d{2,3})\s*(?:회|관리|응용|컴시응)", intent):
            num = int(m)
            if 80 <= num <= 140:
                exam_refs[num].append({
                    "gen": t["gen"],
                    "week": t["week"],
                    "title": t["q_title"][:60],
                })
    return exam_refs


# ─────────────────────────────────────────
# 4. 통계 산출
# ─────────────────────────────────────────

def subject_stats(topics):
    """과목별 토픽 분포"""
    by_subject = Counter()
    by_gen_subject = defaultdict(Counter)
    for t in topics:
        subj = t.get("subject", "UNKNOWN")
        gen = t["gen"]
        by_subject[subj] += 1
        by_gen_subject[gen][subj] += 1
    return by_subject, by_gen_subject


def unexamined_topics(topics):
    """미출제 토픽 목록"""
    result = []
    for t in topics:
        intent = t.get("intent", "")
        if "미출제" in intent:
            result.append({
                "gen": t["gen"],
                "week": t["week"],
                "subject": t.get("subject", "UNKNOWN"),
                "title": t["q_title"][:60],
                "intent": intent[:100],
            })
    return result


def gen_stats(topics):
    """기수별 통계"""
    by_gen = Counter()
    by_gen_week = defaultdict(set)
    by_gen_session = defaultdict(Counter)
    for t in topics:
        gen = t["gen"]
        by_gen[gen] += 1
        by_gen_week[gen].add(t["week"])
        sess = t.get("session", "UNKNOWN")
        by_gen_session[gen][sess] += 1
    return by_gen, by_gen_week, by_gen_session


# ─────────────────────────────────────────
# 5. 마크다운 리포트 생성
# ─────────────────────────────────────────

def generate_report(topics, e137, e138):
    lines = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Header ──
    lines.append("---")
    lines.append("title: FB반 자료 분석 리포트")
    lines.append(f"date: {now}")
    lines.append("tags: [분석, FB반, 기출, 적중률]")
    lines.append("---")
    lines.append("")
    lines.append("# FB반 자료 분석 리포트")
    lines.append(f"> 생성일: {now}")
    lines.append("")

    # ── 1. 요약 ──
    lines.append("## 1. 전체 요약")
    lines.append("")

    by_gen, by_gen_week, by_gen_session = gen_stats(topics)
    by_subject, by_gen_subject = subject_stats(topics)
    unexam = unexamined_topics(topics)

    lines.append(f"| 항목 | 값 |")
    lines.append(f"|---|---|")
    lines.append(f"| 총 토픽 수 | **{len(topics)}**개 |")
    for g in sorted(by_gen.keys()):
        lines.append(f"| {g} | {by_gen[g]}개 ({len(by_gen_week[g])}주차) |")
    lines.append(f"| 출제의도 있음 | {sum(1 for t in topics if t.get('intent','').strip())}개 |")
    lines.append(f"| 작성방안 있음 | {sum(1 for t in topics if t.get('approach','').strip())}개 |")
    lines.append(f"| 본문 있음 | {sum(1 for t in topics if t.get('content','').strip())}개 |")
    lines.append(f"| 미출제 토픽 | {len(unexam)}개 |")
    lines.append("")

    # ── 2. 과목별 분포 ──
    lines.append("## 2. 과목별 토픽 분포")
    lines.append("")
    lines.append("| 과목 | 전체 | 19기 | 20기 | 21기 |")
    lines.append("|---|---|---|---|---|")
    for subj, cnt in sorted(by_subject.items(), key=lambda x: -x[1]):
        c19 = by_gen_subject.get("19기", {}).get(subj, 0)
        c20 = by_gen_subject.get("20기", {}).get(subj, 0)
        c21 = by_gen_subject.get("21기", {}).get(subj, 0)
        bar = "█" * (cnt // 5) + "░" * max(0, 10 - cnt // 5)
        lines.append(f"| {subj} | **{cnt}** {bar} | {c19} | {c20} | {c21} |")
    lines.append("")

    # ── 3. 기수별 비교 ──
    lines.append("## 3. 기수별 학습 진화 분석")
    lines.append("")
    for g in sorted(by_gen.keys()):
        lines.append(f"### {g} ({by_gen[g]}개 토픽, {len(by_gen_week[g])}주차)")
        sess_str = ", ".join(f"{s}:{c}" for s, c in sorted(by_gen_session[g].items()))
        lines.append(f"- 교시 분포: {sess_str}")
        subj_str = ", ".join(f"{s}:{c}" for s, c in sorted(by_gen_subject[g].items(), key=lambda x: -x[1]))
        lines.append(f"- 과목 분포: {subj_str}")
        lines.append("")

    # ── 4. 기출 적중률 분석 ──
    lines.append("## 4. 기출 적중률 분석")
    lines.append("")

    # Helper: render match table for an exam
    def render_match_table(match_result, exam_num):
        """적중률 테이블 렌더링 (3단계: ✅확실/🟡간접/❌미커버)"""
        direct = 0   # 제목에서 키워드 발견
        indirect = 0  # 본문에서만 키워드 발견
        missed = 0
        scorable = 0

        table_lines = []
        table_lines.append("| 교시 | 문번 | 기출 토픽 | 매칭 | FB반 토픽 (최고 매칭) |")
        table_lines.append("|---|---|---|---|---|")

        for qkey in sorted(match_result.keys()):
            v = match_result[qkey]
            exam, sess, qnum = qkey
            if v.get("skipped"):
                table_lines.append(f"| {sess}교시 | Q{qnum:02d} | {v['label']} | ⏭️ 미추출 | - |")
                continue

            scorable += 1
            if v["matches"]:
                best = v["matches"][0]
                ref_icon = "📌" if best.get("intent_ref") else ""
                if best.get("title_hits", 0) >= 1:
                    # 제목에서 키워드 발견 = 확실한 매칭
                    direct += 1
                    table_lines.append(f"| {sess}교시 | Q{qnum:02d} | {v['label']} | ✅ 확실 | [{best['gen']}] {best['title']} {ref_icon} |")
                else:
                    # 본문에서만 발견 = 간접 매칭 (content 번들링 가능성)
                    indirect += 1
                    table_lines.append(f"| {sess}교시 | Q{qnum:02d} | {v['label']} | 🟡 간접 | [{best['gen']}] {best['title']} {ref_icon} |")
            else:
                missed += 1
                table_lines.append(f"| {sess}교시 | Q{qnum:02d} | {v['label']} | ❌ | *미커버* |")

        return table_lines, direct, indirect, missed, scorable

    # 4-1. 137회
    match_137 = match_topic_to_exam(topics, EXAM_137_KEYWORDS, 137)
    tbl_137, d137, i137, m137, s137 = render_match_table(match_137, 137)
    lines.append(f"### 4-1. 137회 적중률")
    lines.append("")
    lines.append(f"| 구분 | 수 | 비율 |")
    lines.append(f"|---|---|---|")
    lines.append(f"| ✅ 확실 (제목 매칭) | {d137} | {d137*100//max(s137,1)}% |")
    lines.append(f"| 🟡 간접 (본문 매칭) | {i137} | {i137*100//max(s137,1)}% |")
    lines.append(f"| ❌ 미커버 | {m137} | {m137*100//max(s137,1)}% |")
    lines.append(f"| ⏭️ 미추출 | {sum(1 for v in match_137.values() if v.get('skipped'))} | - |")
    lines.append(f"| **총 적중** | **{d137+i137}/{s137}** | **{(d137+i137)*100//max(s137,1)}%** |")
    lines.append("")
    lines.extend(tbl_137)
    lines.append("")
    lines.append("> ✅ 확실 = FB 토픽 제목에서 키워드 직접 발견")
    lines.append("> 🟡 간접 = FB 토픽 본문에서만 발견 (같은 리뷰 세션에 포함된 다른 토픽일 수 있음)")
    lines.append("> 📌 = 출제의도에서 해당 회차 직접 언급")
    lines.append("")

    # 4-2. 138회
    match_138 = match_topic_to_exam(topics, EXAM_138_KEYWORDS, 138)
    tbl_138, d138, i138, m138, s138 = render_match_table(match_138, 138)
    lines.append(f"### 4-2. 138회 적중률")
    lines.append("")
    lines.append(f"| 구분 | 수 | 비율 |")
    lines.append(f"|---|---|---|")
    lines.append(f"| ✅ 확실 (제목 매칭) | {d138} | {d138*100//max(s138,1)}% |")
    lines.append(f"| 🟡 간접 (본문 매칭) | {i138} | {i138*100//max(s138,1)}% |")
    lines.append(f"| ❌ 미커버 | {m138} | {m138*100//max(s138,1)}% |")
    lines.append(f"| **총 적중** | **{d138+i138}/{s138}** | **{(d138+i138)*100//max(s138,1)}%** |")
    lines.append("")
    lines.extend(tbl_138)
    lines.append("")

    # ── 5. 학습 갭 분석 ──
    lines.append("## 5. 학습 갭 분석")
    lines.append("")
    lines.append("### 5-1. 137회 기출 중 FB반 미커버 토픽")
    lines.append("")
    gap_137 = [(k, v) for k, v in sorted(match_137.items())
               if not v["matches"] and not v.get("skipped")]
    if gap_137:
        for qkey, v in gap_137:
            lines.append(f"- **{qkey[1]}교시 Q{qkey[2]:02d}**: {v['label']}")
    else:
        lines.append("- 없음 (모든 추출 문제 커버)")
    lines.append("")

    lines.append("### 5-2. 138회 기출 중 FB반 미커버 토픽")
    lines.append("")
    gap_138 = [(k, v) for k, v in sorted(match_138.items())
               if not v["matches"] and not v.get("skipped")]
    if gap_138:
        for qkey, v in gap_138:
            lines.append(f"- **{qkey[1]}교시 Q{qkey[2]:02d}**: {v['label']}")
    else:
        lines.append("- 없음 (모든 추출 문제 커버)")
    lines.append("")

    # ── 6. 미출제 토픽 ──
    lines.append("## 6. 미출제 토픽 목록 (향후 출제 대비)")
    lines.append("")
    lines.append(f"> 출제의도에 '미출제'로 명시된 **{len(unexam)}개** 토픽")
    lines.append("")
    lines.append("| # | 기수 | 주차 | 과목 | 토픽명 |")
    lines.append("|---|---|---|---|---|")
    for i, u in enumerate(unexam, 1):
        lines.append(f"| {i} | {u['gen']} | {u['week']} | {u['subject']} | {u['title']} |")
    lines.append("")

    # ── 7. 기출 회차별 FB반 연관 토픽 ──
    lines.append("## 7. 기출 회차별 FB반 참조 현황")
    lines.append("")
    exam_refs = extract_exam_refs_from_intent(topics)
    lines.append("| 회차 | 참조 토픽 수 |")
    lines.append("|---|---|")
    for exam_num in sorted(exam_refs.keys(), reverse=True):
        if exam_num >= 100:
            refs = exam_refs[exam_num]
            lines.append(f"| {exam_num}회 | {len(refs)} |")
    lines.append("")

    # Detail for 137
    if 137 in exam_refs:
        lines.append("### 137회 직접 참조 토픽")
        lines.append("")
        for ref in exam_refs[137]:
            lines.append(f"- [{ref['gen']}] {ref['title']}")
        lines.append("")

    # ── 8. 학습 추천 ──
    lines.append("## 8. 학습 추천")
    lines.append("")

    # 미커버 갭 추천
    all_gaps = gap_137 + gap_138
    if all_gaps:
        lines.append("### 🔴 우선 보강 필요 (기출 미커버)")
        lines.append("")
        for qkey, v in all_gaps:
            lines.append(f"- {v['label']}")
        lines.append("")

    # 미출제 최신 트렌드
    lines.append("### 🟡 미출제 최신 토픽 (출제 예상)")
    lines.append("")
    trend_keywords = ["가트너", "AI", "클라우드", "보안", "양자", "블록체인", "6G"]
    for u in unexam:
        if any(kw in u["title"] or kw in u.get("intent", "") for kw in trend_keywords):
            lines.append(f"- [{u['gen']}] {u['title']}")
    lines.append("")

    # 고빈도 출제 과목
    lines.append("### 🟢 고빈도 과목 (충분한 학습량)")
    lines.append("")
    for subj, cnt in sorted(by_subject.items(), key=lambda x: -x[1])[:5]:
        lines.append(f"- {subj}: {cnt}개 토픽")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def main():
    print("FB반 자료 분석 시작...")
    topics, e137, e138 = load_all()
    print(f"  토픽: {len(topics)}개, 137회: {len(e137['results'])}개, 138회: {len(e138['results'])}개")

    report = generate_report(topics, e137, e138)

    out_path = os.path.join(DATA_DIR, "fb_analysis_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  리포트 생성: {out_path}")
    print(f"  파일 크기: {os.path.getsize(out_path):,} bytes")

    # Also print summary to console
    print("\n" + "=" * 60)
    print("요약")
    print("=" * 60)
    match_137 = match_topic_to_exam(topics, EXAM_137_KEYWORDS, 137)
    match_138 = match_topic_to_exam(topics, EXAM_138_KEYWORDS, 138)

    def count_matches(match_result):
        direct = indirect = missed = scorable = 0
        for v in match_result.values():
            if v.get("skipped"):
                continue
            scorable += 1
            if v["matches"]:
                if v["matches"][0].get("title_hits", 0) >= 1:
                    direct += 1
                else:
                    indirect += 1
            else:
                missed += 1
        return direct, indirect, missed, scorable

    d137, i137, m137, s137 = count_matches(match_137)
    d138, i138, m138, s138 = count_matches(match_138)
    print(f"  137회: 확실 {d137} + 간접 {i137} = {d137+i137}/{s137} ({(d137+i137)*100//max(s137,1)}%), 미커버 {m137}")
    print(f"  138회: 확실 {d138} + 간접 {i138} = {d138+i138}/{s138} ({(d138+i138)*100//max(s138,1)}%), 미커버 {m138}")
    print(f"  미출제 토픽: {len(unexamined_topics(topics))}개")
    print(f"  과목 수: {len(set(t.get('subject','') for t in topics))}개")


if __name__ == "__main__":
    main()
