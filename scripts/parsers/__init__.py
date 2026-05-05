"""ITPE/KPC 등 한국 IT 기술사 시험 PDF 분할용 파서 모음.

구조:
- base.py: 공통 ParseResult, 헬퍼 (sanitize_filename, strip_header_by_anchor 등)
- classifier.py: PDF의 (publisher, exam_type) 식별 (표시 라벨용)
- (향후) pts.py: PureTopicSegmenter — 학원/시험 종별 무관 토픽 경계 검출
"""
