# itpe-topic-splitter

정보관리 기술사 시험 대비 학습 자료(PDF)를 **토픽별 개별 PDF**로 자동 분할하는 파이프라인.

FB반 리뷰, 600제 교본, 기출 해설 등 대용량 통합 PDF를 문제/토픽 단위로 쪼개어 Obsidian 등 노트 앱과 연동하거나 임베딩 벡터화에 활용한다.

---

## 처리 현황

| 소스 | 원본 PDF | 분할 결과 | 비고 |
|------|---------|----------|------|
| FB반 리뷰 (19기/20기/21기) | 69개 | **371개 토픽 PDF** | 6개 구조적 실패 |
| 600제 교본 (8과목) | 8권 (4,547p) | **816개 문제 PDF** | |
| 기출 해설 (137회/138회) | 복수 | split_pdfs/137회·138회/ | |

---

## 파이프라인

```
원본 PDF (iCloud)
    │
    ├─ split_and_ocr.py ──→ FB반 리뷰 → 토픽별 PDF   (371개)
    ├─ split_600.py ───────→ 600제 교본 → 문제별 PDF  (816개)
    └─ split_exam.py ──────→ 기출 해설 → 문제별 PDF
                                  │
                                  ▼
                        extract_topics.py ──→ data/topics.json
                                  │
                                  ▼
                        analyze_fb.py ──→ 기출 적중률 분석
```

---

## 빠른 시작

```bash
# 의존성 설치
pip install pymupdf
# OCR 사용 시: brew install tesseract ocrmypdf

# FB반 리뷰 분할 (dry-run)
python3 scripts/split_and_ocr.py --dry-run

# 실제 분할
python3 scripts/split_and_ocr.py

# 600제 교본 분할 (dry-run)
python3 scripts/split_600.py

# 실제 분할
python3 scripts/split_600.py --run

# 기출 해설 분할
python3 scripts/split_exam.py --exam 137 --run

# 텍스트 추출 → topics.json
python3 scripts/extract_topics.py

# 기출 적중률 분석
python3 scripts/analyze_fb.py
```

---

## 폴더 구조

```
itpe-topic-splitter/
├── scripts/              ← 처리 스크립트
│   ├── split_and_ocr.py  ← FB반 리뷰 PDF 분할
│   ├── split_600.py      ← 600제 교본 분할
│   ├── split_exam.py     ← 기출 해설 분할
│   ├── extract_topics.py ← 텍스트 추출
│   ├── analyze_fb.py     ← 적중률 분석
│   └── README.md         ← 스크립트 상세 문서
├── data/                 ← 처리 결과 JSON
│   ├── topics.json
│   ├── split_report.json
│   ├── 600je_report.json
│   ├── exam137_report.json
│   └── exam138_report.json
└── split_pdfs/           ← 분할된 PDF 출력 (gitignore)
    ├── 19기/ 20기/ 21기/
    ├── 137회/ 138회/
    └── 600제/
        └── 경영/ 소공/ DB/ DS/ NW/ CAOS/ 보안/ 인알통/
```

> **원본 PDF**는 iCloud(`공부/4_FB반 자료/`)에 보관. 이 repo에는 포함하지 않음.

---

## 지원 포맷

`split_and_ocr.py`가 자동 감지하는 FB반 리뷰 PDF 포맷:

| 포맷 | 특징 |
|------|------|
| `standard` | 문제 목록 + 번호별 답안 (일반형) |
| `inline` | 문제·답안 인라인 혼합 |
| `menti` | 멘티출제 카드 형식 (출제영역/난이도 포함) |
| `bare` | 목록 없이 번호만 |
| `sparse` | 이미지 전용 (OCR 필요) |
| `merged` | FB + 아이리포 합본 (자동 분리) |

---

## 의존성

- Python 3.9+
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF 파싱 및 분할
- [ocrmypdf](https://ocrmypdf.readthedocs.io/) + [Tesseract](https://github.com/tesseract-ocr/tesseract) — OCR (선택)
