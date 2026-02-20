# 정보관리 기술사 PDF 처리 스크립트

FB반 학습 자료(리뷰 PDF, 600제 교본, 기출 해설)를 토픽별 개별 PDF로 분할하고, OCR 처리 및 텍스트 추출을 수행하는 파이프라인.

## 파이프라인 흐름

```
원본 PDF (통합본)
  │
  ├── split_and_ocr.py ──→ 리뷰 PDF → 토픽별 PDF
  ├── split_600.py ──────→ 600제 교본 → 문제별 PDF
  └── split_exam.py ─────→ 기출 해설 → 문제별 PDF
                              │
                              ▼
                     extract_topics.py ──→ topics.json (텍스트 데이터)
                              │
                              ▼
                     analyze_fb.py ──→ 적중률/커버리지 분석
```

## 스크립트 목록

### `split_and_ocr.py` — FB반 리뷰 PDF 분할

19기/20기/21기 주간 리뷰 PDF를 토픽별 개별 PDF로 분리.

```bash
python3 split_and_ocr.py              # 전체 처리 (분할만)
python3 split_and_ocr.py --ocr        # 분할 + OCR
python3 split_and_ocr.py --dry-run    # 미리보기
python3 split_and_ocr.py --single <path>  # 단일 PDF
```

**지원 포맷**: standard, inline, menti, bare, sparse, problem_only, merged

**출력**: `split_pdfs/{기수}/{주차}/` 에 토픽별 PDF
**리포트**: `data/split_report.json`

### `split_600.py` — 600제 교본 분할

8개 과목 통합본(4,547페이지)을 문제별 개별 PDF로 분할.

```bash
python3 split_600.py                  # dry-run (전체)
python3 split_600.py --run            # 실제 분할
python3 split_600.py --run --ocr      # 분할 + OCR
python3 split_600.py --subject 경영   # 특정 과목만
python3 split_600.py --single <path>  # 단일 PDF
```

**대상 과목**: 경영, 소공, DB, DS, NW, CAOS, 보안, 인알통
**탐지 패턴**: 독립 줄 `문제` + 12줄 이내 `도메인` 또는 `출제영역`
**출력**: `split_pdfs/600제/{과목}/`
**리포트**: `data/600je_report.json`

### `split_exam.py` — 기출 해설 PDF 분할

137회/138회 기출 해설 PDF를 문제별로 분할.

```bash
python3 split_exam.py                 # dry-run
python3 split_exam.py --run           # 실제 분할
python3 split_exam.py --run --ocr     # 분할 + OCR
python3 split_exam.py --exam 137      # 특정 회차만
```

**출력**: `split_pdfs/{N}회/`
**리포트**: `data/exam{N}_report.json`

### `extract_topics.py` — 텍스트 추출

분할된 PDF에서 텍스트를 추출하여 JSON으로 저장.

```bash
python3 extract_topics.py             # 전체 추출
python3 extract_topics.py --ocr       # OCR 포함
```

**출력**: `data/topics.json`

### `analyze_fb.py` — 분석

기출 적중률, 과목별 분포, 학습 갭 분석.

```bash
python3 analyze_fb.py
```

## 폴더 구조

```
itpe-topic-splitter/         ← 프로젝트 루트
├── scripts/                 ← 이 폴더 (처리 스크립트)
├── data/                    ← JSON 리포트/데이터
│   ├── split_report.json
│   ├── 600je_report.json
│   ├── exam137_report.json
│   ├── exam138_report.json
│   └── topics.json
└── split_pdfs/              ← 분할 결과 출력
    ├── 137회/
    ├── 138회/
    ├── 19기/
    ├── 20기/
    ├── 21기/
    └── 600제/
        ├── 경영/
        ├── 소공/
        ├── DB/
        ├── DS/
        ├── NW/
        ├── CAOS/
        ├── 보안/
        └── 인알통/

FB반 자료/ (iCloud)          ← 원본 PDF (입력만, 이동하지 않음)
├── 19기/
├── 20기/
├── 21기/
└── 교본 (600제)/
```

## 의존성

- Python 3.9+
- PyMuPDF (`pip install pymupdf`)
- ocrmypdf + tesseract (OCR 사용 시)

## 현재 처리 현황

| 소스 | PDF 수 | 추출 토픽/문제 | 실패 |
|------|--------|---------------|------|
| FB반 리뷰 | 69개 | 371개 토픽 | 6개 |
| 600제 교본 | 8권 | 816개 문제 | 0개 |
| 기출 해설 | 137회+138회 | - | - |
