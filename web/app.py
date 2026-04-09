#!/usr/bin/env python3
"""
ITPE Topic Splitter — 경량 웹 서비스

PDF 업로드 → kordoc 파싱 → 다중 신호 경계 탐지 → 토픽별 분할 → ZIP 다운로드

사용법:
  uvicorn web.app:app --host 0.0.0.0 --port 8080
"""

import os
import sys
import io
import zipfile
import tempfile
import shutil
import traceback
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse

# 프로젝트 루트의 scripts를 import 경로에 추가
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from split_odl import parse_kordoc, split_pdf, safe_filename  # noqa: E402
from detect_boundaries_v2 import (  # noqa: E402
    detect_boundaries_v2, detect_sessions, analyze_quality,
)

app = FastAPI(title="ITPE Topic Splitter", version="1.0")

# 정적 파일 서빙 (index.html)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 업로드 크기 제한 (50MB)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/split")
async def api_split(file: UploadFile = File(...)):
    """
    PDF 업로드 → 토픽별 분할 → ZIP 반환

    Returns:
      - 성공: ZIP 파일 (application/zip)
      - 실패: JSON 에러
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    # 파일 크기 체크
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"파일이 너무 큽니다 (최대 {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    # 임시 디렉토리에서 작업
    work_dir = tempfile.mkdtemp(prefix="itpe_split_")
    try:
        # 업로드 PDF 저장
        pdf_path = os.path.join(work_dir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(content)

        # 1. kordoc 파싱
        try:
            elements, total_pages = parse_kordoc(pdf_path)
        except Exception as e:
            raise HTTPException(422, f"PDF 파싱 실패: {str(e)[:200]}")

        if not elements:
            raise HTTPException(422, "PDF에서 텍스트를 추출할 수 없습니다.")

        # 2. 경계 탐지
        boundaries_v2, warnings = detect_boundaries_v2(
            elements, total_pages, "")
        sessions = detect_sessions(elements, total_pages)
        quality_report = analyze_quality(
            boundaries_v2, sessions, elements, total_pages, warnings)

        boundaries = [
            {
                "num": b.num,
                "title": b.title,
                "page": b.page_start,
                "page_start": b.page_start,
                "page_end": b.page_end,
                "fmt": b.fmt,
                "session": b.session,
                "confidence": b.confidence,
            }
            for b in boundaries_v2
        ]

        if not boundaries:
            raise HTTPException(
                422,
                f"토픽 경계를 탐지하지 못했습니다. "
                f"(pages={total_pages}, elements={len(elements)})"
            )

        # 3. PDF 분할
        split_dir = os.path.join(work_dir, "split")
        os.makedirs(split_dir, exist_ok=True)

        # 파일명에서 메타 추출 (간소화)
        base_name = Path(file.filename).stem
        results = split_pdf(
            source_path=pdf_path,
            boundaries=boundaries,
            output_dir=split_dir,
            gen="",
            week="",
            subject="",
            session="",
        )

        if not results:
            raise HTTPException(500, "PDF 분할 결과가 없습니다.")

        # 4. ZIP 생성
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in results:
                file_path = r["path"]
                arcname = r["filename"]
                zf.write(file_path, arcname)

        zip_buffer.seek(0)
        zip_name = f"{safe_filename(base_name, 40)}_split.zip"

        # HTTP 헤더는 ASCII만 허용 → 한글 URL-encode
        from urllib.parse import quote
        warn_str = quote("; ".join(warnings[:5]), safe="")

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name)}",
                "X-Topic-Count": str(len(results)),
                "X-Total-Pages": str(total_pages),
                "X-Warnings": warn_str,
                "X-Quality-Report": quote(quality_report, safe=""),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"서버 오류: {str(e)[:200]}")
    finally:
        # 임시 파일 정리
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
