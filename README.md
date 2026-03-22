# 📚 ITPE Topic Splitter

> 정보처리기술사(정보관리/컴퓨터시스템응용) 시험 대비 학습 자료(PDF)를 **토픽별 개별 PDF**로 자동 분할하는 파이프라인

FB반 리뷰, 600제 교본, 기출 해설, 모의고사, 합숙 등 대용량 통합 PDF를 문제/토픽 단위로 쪼개어 **Obsidian**, **Notion** 등 노트 앱과 연동하거나 RAG 임베딩에 활용할 수 있습니다.

---

## ✨ 주요 기능

| 기능 | 설명 |
|------|------|
| **다중 학원 PDF 지원** | ITPE, KPC, 인포레버 등 서로 다른 레이아웃의 PDF를 하나의 파이프라인으로 처리 |
| **다중 신호 경계 탐지 (v2)** | "끝" 마커, 로마숫자(I.), 문제번호, 소제목 리셋, 밀도 변화 등 6개 신호의 가중합으로 토픽 경계를 판단 |
| **교시 자동 분리** | 표지 페이지 감지 → 1교시(13문) + 2·3·4교시(6문) 구조 자동 파악 |
| **OCR 보강** | 이미지 전용 페이지를 Tesseract로 OCR 처리하여 텍스트 신호 보완 |
| **자기 교정 가중치** | 문서 내 신호 분포를 분석하여 학원별로 가중치를 자동 조정 |
| **FB반 리뷰 6종 포맷 감지** | standard, inline, menti, bare, sparse, merged 자동 분류 |
| **건조 실행(dry-run)** | 실제 PDF 분할 없이 탐지 결과만 미리 확인 |

---

## 📊 처리 현황

### 기출 해설 (138회 기준, v2 파이프라인)

| 파일 | v1 | v2 | 목표(/31) |
|------|:---:|:---:|:---------:|
| ITPE 138응 | 23 | **31** ✅ | 31 |
| ITPE 138관 | 27 | **31** ✅ | 31 |
| KPC 138응 | 7 | **31** ✅ | 31 |
| KPC 138관 | 18 | **31** ✅ | 31 |
| 인포레버 138응 | 24 | **26** | 31 |
| 인포레버 138관 | 32 | **31** ✅ | 31 |
| **합계** | **131** | **181** | **186** |

> **v1 → v2 정확도: 70.4% → 97.3%** (5/6 문서 완벽 분할)

### 전체 소스별 현황

| 소스 | 원본 PDF | 분할 결과 | 비고 |
|------|:--------:|:---------:|------|
| FB반 리뷰 (19기/20기/21기) | 69개 | **371개** 토픽 | 구조적 실패 6개 |
| 600제 교본 (8과목) | 8권 (4,547p) | **816개** 문제 | — |
| 기출 해설 (117–138회) | 22회차 | 회차별 25–110개 | 130회 PDF 손상 |
| 모의고사 (ITPE) | 19개 | ~477개 문제 | KPC 포맷 상이 |
| 합숙 (ITPE) | 35개 | ~715개 문제 | KPC 포맷 상이 |

---

## 🏗 아키텍처

```
원본 PDF (iCloud)
    │
    ├── split_odl.py ──────────────→ 기출 해설 (ODL+v2) → 토픽별 PDF
    │       └── detect_boundaries_v2.py  ← 다중 신호 경계 탐지 엔진
    │
    ├── split_and_ocr.py ──────────→ FB반 리뷰 → 토픽별 PDF
    ├── split_600.py ──────────────→ 600제 교본 → 문제별 PDF
    ├── split_exam.py ─────────────→ 기출 해설 (레거시) → 문제별 PDF
    └── split_materials.py ────────→ 모의고사/합숙 → 문제별 PDF
                                          │
                                          ▼
                                 extract_topics.py → topics.json
                                          │
                                          ▼
                                 analyze_fb.py → 기출 적중률 분석
```

### v2 경계 탐지 엔진 (`detect_boundaries_v2.py`)

```
입력: ODL JSON elements + total_pages
    │
    ├── Phase 1 — 교시 분리
    │   └── 표지 페이지("국가기술자격 기술사 시험문제") → 4개 교시 블록
    │
    ├── Phase 2 — 다중 신호 점수 산정
    │   ├── 끝 마커 (score 10.0)
    │   ├── I. 로마숫자 시작 (score 8.0)
    │   ├── 문제 N. 번호 (score 9.0)
    │   ├── 번호 리스타트 (score 5.0)
    │   ├── 한글 소제목 "가." 리셋 (score 6.0)
    │   └── Q 키워드 (score 3.0)
    │
    ├── Phase 3 — 자기 교정
    │   └── 문서 내 신호 빈도 분석 → 가중치 동적 조정
    │
    └── Phase 4 — 후처리
        ├── 노이즈 페이지 필터링
        ├── 긴 섹션 자동 서브 분할 (max_span 위반 시)
        └── 교시별 기대 토픽 수 검증
```

---

## 🚀 빠른 시작

### 사전 요구사항

```bash
# Python 3.9+ (개발 환경: Python 3.14)
# macOS Homebrew 기준
brew install openjdk          # ODL(opendataloader) 실행에 필요
brew install tesseract        # OCR (선택)
brew install ocrmypdf         # OCR (선택)

# 환경 변수 (쉘 프로필에 추가 권장)
export PATH="/opt/homebrew/opt/openjdk/bin:/opt/homebrew/bin:$PATH"
```

### 설치

```bash
git clone https://github.com/turtlesoup0/itpe_topic_splitter.git
cd itpe_topic_splitter

# 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate

# 의존성 설치
pip install pymupdf opendataloader-pdf
```

### 실행 예시

```bash
# 기출 해설 분할 (ODL + v2 엔진) — 가장 정확
venv/bin/python3 scripts/split_odl.py --single "기출해설.pdf"
venv/bin/python3 scripts/split_odl.py --dry-run          # 전체 dry-run
venv/bin/python3 scripts/split_odl.py                    # 전체 실행

# FB반 리뷰 분할
python3 scripts/split_and_ocr.py --dry-run
python3 scripts/split_and_ocr.py

# 600제 교본 분할
python3 scripts/split_600.py --run

# 기출 해설 분할 (레거시)
python3 scripts/split_exam.py --exam 138 --run

# 모의고사/합숙 분할
python3 scripts/split_materials.py --run

# 텍스트 추출 → topics.json
python3 scripts/extract_topics.py

# 기출 적중률 분석
python3 scripts/analyze_fb.py
```

---

## 📁 프로젝트 구조

```
itpe-topic-splitter/
├── scripts/                           ← 처리 스크립트
│   ├── split_odl.py                   ← ODL 기반 PDF 분할 (v2 엔진 사용)
│   ├── detect_boundaries_v2.py        ← 다중 신호 경계 탐지 엔진
│   ├── split_and_ocr.py               ← FB반 리뷰 PDF 분할
│   ├── split_600.py                   ← 600제 교본 분할
│   ├── split_exam.py                  ← 기출 해설 분할 (레거시)
│   ├── split_materials.py             ← 모의고사/합숙 분할
│   ├── extract_topics.py              ← 분할된 PDF 텍스트 추출
│   ├── analyze_fb.py                  ← 기출 적중률 분석
│   ├── diagnose_boundary.py           ← 경계 탐지 디버그 도구
│   ├── compare_extractors.py          ← 추출기 비교 도구
│   ├── test_opendataloader.py         ← ODL 테스트
│   └── README.md                      ← 스크립트별 상세 문서
│
├── data/                              ← 처리 결과 JSON
│   ├── topics.json                    ← 추출된 토픽 텍스트
│   ├── split_report.json              ← FB반 분할 리포트
│   ├── 600je_report.json              ← 600제 분할 리포트
│   ├── exam{N}_report.json            ← 회차별 기출 분할 리포트
│   ├── split_odl_report.json          ← ODL 분할 리포트
│   ├── moui_report.json               ← 모의고사 리포트
│   ├── habsuk_report.json             ← 합숙 리포트
│   └── fb_analysis_report.md          ← 적중률 분석 결과
│
├── split_pdfs/                        ← 분할 결과 (gitignore)
│   ├── 19기/ 20기/ 21기/              ← FB반 리뷰
│   ├── 600제/                         ← 교본 (경영/소공/DB/DS/NW/CAOS/보안/인알통)
│   ├── {N}회/                         ← 기출 해설 (117–138회)
│   ├── 모의고사/                      ← 모의고사
│   └── 합숙/                          ← 합숙
│
├── split_pdfs_odl/                    ← ODL 분할 결과 (gitignore)
│   ├── 19기/                          ← FB반 (ODL 버전)
│   └── 138회/                         ← 기출 해설
│       ├── ITPE_138응/
│       ├── ITPE_138관/
│       ├── KPC_138응/
│       ├── KPC_138관/
│       ├── 인포레버_138응/
│       └── 인포레버_138관/
│
├── venv/                              ← Python 가상환경 (gitignore)
├── HANDOFF_CONTEXT.md                 ← 개발 인계 문서
├── CLAUDE.md                          ← AI 페어 프로그래밍 규칙
└── .gitignore
```

> **원본 PDF**는 iCloud(`~/Library/Mobile Documents/com~apple~CloudDocs/공부/`)에 보관되며, 이 저장소에는 포함하지 않습니다.

---

## 🔍 지원 PDF 포맷

### 기출 해설 (split_odl.py + detect_boundaries_v2.py)

| 학원 | 특징 | v2 정확도 |
|------|------|:---------:|
| **ITPE** | 교시 표지 + "끝" 마커 + I. 로마숫자 | 31/31 ✅ |
| **KPC** | 교시 표지 + 번호 리스타트 중심 | 31/31 ✅ |
| **인포레버** | 표지 없음, 혼합 신호, OCR 의존 | 26–31/31 |

### FB반 리뷰 (split_and_ocr.py)

| 포맷 | 특징 |
|------|------|
| `standard` | 문제 목록 + 번호별 답안 (일반형) |
| `inline` | 문제·답안 인라인 혼합 |
| `menti` | 멘티출제 카드 형식 (출제영역/난이도 포함) |
| `bare` | 목록 없이 번호만 |
| `sparse` | 이미지 전용 (OCR 필요) |
| `merged` | FB + 아이리포 합본 (자동 분리) |

---

## 🧪 시험 구조 참고

정보처리기술사 시험은 다음과 같은 구조입니다:

```
┌─────────┬────────────┬──────────────────────────────┐
│  교시   │ 문항 수    │ 형식                         │
├─────────┼────────────┼──────────────────────────────┤
│ 1교시   │ 13문       │ 단답형 (10점 × 13)           │
│ 2교시   │ 6문 중 4선택│ 서술형 (25점 × 4)            │
│ 3교시   │ 6문 중 4선택│ 서술형 (25점 × 4)            │
│ 4교시   │ 6문 중 4선택│ 서술형 (25점 × 4)            │
├─────────┼────────────┼──────────────────────────────┤
│ 합계    │ 31문       │ 1교시 130점 + 2~4교시 300점  │
└─────────┴────────────┴──────────────────────────────┘
```

따라서 **하나의 기출 해설 합본 PDF에서 31개의 토픽 PDF가 분리**되어야 합니다.

---

## ⚙️ 의존성

| 패키지 | 용도 | 필수 |
|--------|------|:----:|
| [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) | PDF 파싱 및 분할 | ✅ |
| [opendataloader-pdf](https://pypi.org/project/opendataloader-pdf/) | PDF → 구조화된 JSON 변환 | ✅ |
| [OpenJDK](https://openjdk.org/) | opendataloader 런타임 | ✅ |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) | OCR 엔진 | 선택 |
| [ocrmypdf](https://ocrmypdf.readthedocs.io/) | OCR 통합 도구 | 선택 |

---

## 📝 개발 노트

### v1 → v2 주요 개선사항

| 항목 | v1 | v2 |
|------|----|----|
| 경계 탐지 | 단일 마커 의존 ("끝" or "I.") | 6개 신호 가중합 |
| 학원 호환성 | ITPE 위주 | ITPE + KPC + 인포레버 |
| 교시 분리 | 교시 표지 고정 패턴 | 표지 + 텍스트 멘션 + 폴백 |
| 데이터 모델 | dict 기반 | TopicBoundary dataclass |
| 후처리 | roman I. 서브 분할만 | 서브 분할 + 노이즈 필터 + 헤딩 기반 분할 |
| 138회 정확도 | 131/186 (70.4%) | 181/186 (97.3%) |

### 알려진 제한사항

- **인포레버 138응** (5개 미탐지): 토픽 시작에 "I.", "끝", 번호 리스타트 등 어떤 신호도 없는 페이지가 존재
- **KPC 모의고사/합숙**: ITPE와 포맷이 상이하여 현재 미지원
- **130회 기출 해설**: PDF 파일 손상으로 처리 불가

---

## 📄 라이선스

이 프로젝트는 개인 학습 목적으로 제작되었습니다. 원본 PDF 자료의 저작권은 각 학원(ITPE, KPC, 인포레버)에 있습니다.
