# 후속 모델 인계 맥락 문서
작성일: 2026-03-20

---

## 프로젝트 개요

**목적**: 정보처리기술사 FB반 학원의 PDF 해설지들을 토픽(문제)별로 자동 분할
**핵심 파일**: `/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter/scripts/split_odl.py`
**출력 디렉토리**: `split_pdfs_odl/` (FB반 리뷰) 및 `split_pdfs_odl/138회/` (기출풀이)

---

## 현재 작업 상황

### 완료된 작업
1. **ODL(opendataloader-pdf) 기반 파이프라인** 구축 완료
   - ODL JSON 파싱 → elements 추출 → 경계 탐지 → PyMuPDF 분할
   - 이미지 페이지에 Tesseract OCR 보강 (`_ocr_sparse_pages()`)

2. **FB반 19기 리뷰 문서** 분할 완료 (204개 토픽, 1개 실패)
   - 포맷: menti / standard / 끝 / dash 자동 감지
   - 경로: `split_pdfs_odl/19기/`

3. **22기 DS 1교시/2교시 리뷰** 분할 완료
   - DS 1교시: 12개 (standard 포맷)
   - DS 2교시: 4개 (끝 포맷 — 학생이 4문제 선택)

4. **138회 기출풀이 6개 PDF** 부분 분할 완료
   - 경로: `split_pdfs_odl/138회/{ITPE_138응, ITPE_138관, KPC_138응, KPC_138관, 인포레버_138응, 인포레버_138관}/`

---

## 현재 핵심 문제: 138회 기출풀이 토픽 수 불일치

### 목표 구조
각 기출풀이 합본은 **1교시 13개 + 2교시 6개 + 3교시 6개 + 4교시 6개 = 총 31개** 분리되어야 함

### 현재 탐지 현황
| 파일 | 탐지 | 목표 | 달성률 |
|------|------|------|--------|
| ITPE 138응-합.pdf | 23 | 31 | 74% |
| ITPE 138관-합.pdf | 27 | 31 | 87% |
| KPC 138응-합.pdf | 7 | 31 | 23% |
| KPC 138관-합.pdf | 18 | 31 | 58% |
| 인포레버 138응.pdf | 24 | 31 | 77% |
| 인포레버 138관.pdf | 32 | 31 | 103% (1개 초과) |

---

## 근본 원인 분석

### 원인 1: 1교시 단답형 토픽들이 묶여 하나의 "끝" 공유
- 1교시 13개 단답형(10점 각)의 답안이 2-3개씩 하나의 "끝" 마커를 공유
- p1-p6 같은 초기 섹션에 여러 단답형 토픽이 압축됨

### 원인 2: 교시 경계에 "끝" 마커 없음
- 1교시 → 2교시 전환 시 "끝" 없이 바로 연결됨
- 예: ITPE 138응에서 p25-38이 1교시 FRAM + System Call + 2교시 첫 토픽을 모두 합침
- 교시 표지("국가기술자격 기술사 시험문제") 페이지가 구분자 역할을 해야 하나 잘 감지 안 됨

### 원인 3: KPC 문서는 "끝" 마커가 극소
- KPC 138응: 103페이지에 "끝" 마커 4개뿐 → 5개 섹션만 탐지
- KPC 문서 형식이 ITPE/인포레버와 근본적으로 다름
- Q01이 1-29페이지(29페이지), Q02가 30-61페이지(32페이지) 같은 비정상적 분할

### 원인 4: "시험문제" 표지 이후 섹션에 "I." 없음
- 교시 표지 페이지 이후 2교시+ 문제들이 "I." 형식이 아닌 다른 형식으로 시작
- 예: ITPE 138응 Q10 "시험문제" p30-38 (8페이지) — 내부에 여러 2교시 답안이 있으나 탐지 못함

---

## 현재 구현된 알고리즘 (split_odl.py 주요 함수)

### `detect_boundaries(elements, total_pages, session="")` 흐름
1. **끝 포맷** (`_끝_boundaries()`):
   - `_끝_PAT` 매칭으로 "끝" 마커 페이지 탐지
   - 반복 헤더 필터링 (Counter 기반, 15% 이상 등장 heading 제외)
   - **`_교시_cover_pages()`**: "시험문제/시험시간" 페이지를 교시 경계로 강제 분리 (직전 표지와 10페이지 이상 간격인 것만)
   - **`_sub_split_by_roman_I()`**: 각 섹션 내 `^I\.\s+.{8,}` 패턴 = 새 토픽 시작, 복수이면 분리

2. **menti 포맷**: `문 제 N. 토픽명` 형식 3개 이상
3. **standard 포맷**: `N. 토픽명` 형식, 우선순위 점수 기반
4. **dash 폴백**: `- 섹션명` 형식

### 핵심 패턴/상수
```python
_끝_PAT = re.compile(r'^[\u201c\u201d"]?끝[\u201c\u201d"]?\s*$')  # Unicode 스마트 따옴표 포함
_ROMAN_I_PAT  = re.compile(r'^I\.\s+.{8,}')   # 서브분리: 새 토픽 시작
_ROMAN_II_PAT = re.compile(r'^(II|III|IV|V)\.\s+')  # 현재 사용 안 함 (단순화됨)
_IGNORE_PAT = re.compile(r'^\d+$|Copyright|FB\d{2}|주간모의|교시|^※|^다음 문제|문제를 선택')
```

### OCR 통합
- ODL element가 없는 이미지 페이지 → PyMuPDF `get_textpage_ocr(language="kor+eng")`
- OCR로 찾은 "끝" → `source: "ocr"` 태그, `native_끝_count` 계산에서 제외

---

## 남은 과제 (우선순위 순)

### 1순위: KPC 문서 형식 분석
- KPC 138응: Q01 p1-29 (1교시 전체?) + Q02 p30-61 (교시 표지 32페이지?)
- **문제**: KPC가 "끝" 마커를 거의 사용 안 하고 다른 구분 방식 사용
- **조사 필요**: KPC PDF의 실제 ODL elements를 보고 어떤 패턴으로 구분되는지 파악
  ```python
  # 디버그용 예시 코드
  elements, total_pages = parse_odl_json('KPC 138응-합.pdf')
  for e in elements[50:150]:  # 가운데 부분
      print(f'p{e["page"]:02d} [{e["type"]}]: {e["content"][:80]}')
  ```
- KPC는 페이지 번호나 문제 번호 형식이 다를 가능성 높음

### 2순위: ITPE "시험문제" 섹션 내 2교시 답안 탐지
- ITPE 138응 Q10 p30-38 "시험문제" (8페이지): 실제로 2교시 Q1~Q2 답안 포함
- **문제**: 교시 표지 이후 2교시 답안이 "I." 형식이 아닐 수 있음
- **조사 필요**: p31-38의 실제 element 내용 확인

### 3순위: 1교시 단답형 누락 (6개 정도)
- p01-p06 섹션에 2개 이상의 단답형 토픽이 묶여있으나 "I." 없이 시작
- 인포레버 138응 p01-p08: 목차/도입부가 첫 토픽에 흡수됨
- **접근법**: 1교시 단답형은 짧으므로 페이지 패턴(`1. 토픽명`, 줄바꿈 패턴) 기반 탐지 시도

### 4순위: 인포레버 138관 1개 초과 (32→31)
- 어떤 섹션에서 false positive가 발생했는지 확인 필요

---

## 환경 설정

```bash
cd /Users/turtlesoup0-macmini/Projects/itpe-topic-splitter
export PATH="/opt/homebrew/opt/openjdk/bin:/opt/homebrew/bin:$PATH"
# Java 25 (openjdk) 필요
venv/bin/python3 scripts/split_odl.py --single "<PDF경로>"
venv/bin/python3 scripts/split_odl.py --dry-run   # 전체 dry-run
```

### 디버그용 스니펫
```python
import sys; sys.path.insert(0, 'scripts')
from split_odl import parse_odl_json, detect_boundaries, _끝_PAT, _교시_cover_pages

pdf = '...'
elements, total_pages = parse_odl_json(pdf)

# 끝 마커 위치
끝_pages = [e['page'] for e in elements if _끝_PAT.match(e['content'].strip())]
print('끝 pages:', 끝_pages)

# 교시 표지 위치
covers = _교시_cover_pages(elements)
print('교시 covers:', covers)

# 특정 페이지 구간 elements 보기
for e in elements:
    if 30 <= e['page'] <= 45:
        print(f'p{e["page"]:02d} [{e["type"]:8s}]: {e["content"][:80]}')

# 경계 탐지 최종 결과
bounds = detect_boundaries(elements, total_pages, '기출')
print(f'{len(bounds)}개 탐지')
for b in bounds:
    print(f'  Q{b["num"]:02d} p{b["page_start"]}-{b["page_end"]}: {b["title"][:60]}')
```

---

## 파일 구조
```
itpe-topic-splitter/
├── scripts/
│   ├── split_odl.py          # 핵심 분할 스크립트 (이번 세션 주요 작업)
│   ├── split_and_ocr.py      # 구형 스크립트 (사용 안 함)
│   └── diagnose_boundary.py  # 경계 탐지 디버그용
├── split_pdfs_odl/
│   ├── 19기/                 # FB반 19기 분할 결과 (완료)
│   ├── single/single/        # --single 모드 출력 (22기 DS 파일들)
│   └── 138회/               # 기출풀이 분할 결과 (현재 131/186)
│       ├── ITPE_138응/       # 23개
│       ├── ITPE_138관/       # 27개
│       ├── KPC_138응/        # 7개  ← 주요 문제
│       ├── KPC_138관/        # 18개
│       ├── 인포레버_138응/    # 24개
│       └── 인포레버_138관/   # 32개
├── data/
│   └── split_odl_report.json
└── venv/                     # Python 3.14 가상환경 (openjdk 필요)
```

---

## CLAUDE.md 준수 사항
- 코드 작성 전 방법론 설명 및 승인 대기
- 3개 파일 초과 변경 시 태스크 분리
- 커밋은 명시적 요청 시에만
- 사용자가 "프로젝트 진행을 신뢰하니 일일히 승낙을 묻지 마"라고 했으므로 기술적 판단은 자율적으로 진행 가능
