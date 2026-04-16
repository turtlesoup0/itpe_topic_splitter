#!/usr/bin/env python3
"""
ITPE Topic Splitter — 경량 웹 서비스

PDF 업로드 → kordoc 파싱 → 다중 신호 경계 탐지 → 토픽별 분할 → ZIP 다운로드

비동기 처리: 업로드 → job_id 즉시 반환 → 백그라운드 처리 → 폴링으로 결과 수신
(Cloudflare Tunnel 100초 타임아웃 대응)

사용법:
  uvicorn web.app:app --host 127.0.0.1 --port 8080
"""

import os
import sys
import time
import uuid
import zipfile
import tempfile
import shutil
import traceback
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse

# 프로젝트 루트의 scripts를 import 경로에 추가
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

def _load_dotenv(path: Path = PROJECT_DIR / ".env"):
    """최소 .env 로더 — 외부 의존 없이 KEY=VALUE 파싱.

    빈 문자열("")로 설정된 환경변수도 .env 값으로 덮어씀.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not os.environ.get(k):  # 미설정 또는 빈 문자열이면 덮어씀
            os.environ[k] = v

_load_dotenv()

from split_odl import parse_kordoc, split_pdf, safe_filename  # noqa: E402
from detect_boundaries_v2 import (  # noqa: E402
    detect_boundaries_v2, detect_sessions, analyze_quality,
)
from llm_verifier import enhance_boundaries_sync, is_available as llm_available  # noqa: E402

app = FastAPI(title="ITPE Topic Splitter", version="1.1")

# 정적 파일 서빙 (index.html)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 업로드 크기 제한 (50MB)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# ─── 비동기 Job 관리 ─────────────────────────────────────────────
# job_id → { status, progress, result_path, topic_count, total_pages,
#             warnings, quality_report, zip_name, error, work_dir }
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL_SEC = 30 * 60  # 완료/실패 후 30분 경과 시 자동 정리


def _cleanup_stale_jobs():
    """TTL 초과 Job의 임시 디렉토리 정리 및 메모리 해제."""
    now = time.time()
    stale_ids = []
    with _jobs_lock:
        for jid, job in _jobs.items():
            ts = job.get("finished_at")
            if ts and now - ts > _JOB_TTL_SEC:
                stale_ids.append(jid)
        for jid in stale_ids:
            job = _jobs.pop(jid)
            work_dir = job.get("work_dir")
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)


def _process_job(job_id: str, pdf_content: bytes, filename: str):
    """백그라운드에서 PDF 분할 처리."""
    work_dir = tempfile.mkdtemp(prefix="itpe_split_")
    try:
        with _jobs_lock:
            _jobs[job_id]["work_dir"] = work_dir
            _jobs[job_id]["progress"] = "PDF 파싱 중..."

        pdf_path = os.path.join(work_dir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_content)

        # 1. kordoc 파싱
        elements, total_pages = parse_kordoc(pdf_path)
        if not elements:
            raise Exception("PDF에서 텍스트를 추출할 수 없습니다.")

        with _jobs_lock:
            _jobs[job_id]["progress"] = "토픽 경계 탐지 중..."

        # 2. 경계 탐지
        boundaries_v2, warnings = detect_boundaries_v2(
            elements, total_pages, "")
        sessions = detect_sessions(elements, total_pages)
        quality_report = analyze_quality(
            boundaries_v2, sessions, elements, total_pages, warnings)

        boundaries = [
            {
                "num": b.num, "title": b.title,
                "page": b.page_start, "page_start": b.page_start,
                "page_end": b.page_end, "fmt": b.fmt,
                "session": b.session, "session_q": b.session_q,
                "confidence": b.confidence,
            }
            for b in boundaries_v2
        ]

        if not boundaries:
            raise Exception(
                f"토픽 경계를 탐지하지 못했습니다. "
                f"(pages={total_pages}, elements={len(elements)})")

        # 2.5. LLM 검증 (API 키 있을 때만)
        if llm_available():
            with _jobs_lock:
                _jobs[job_id]["progress"] = "LLM 검증 중..."
            try:
                result = enhance_boundaries_sync(
                    boundaries, elements, total_pages)
                boundaries = result.boundaries
                if result.titles_updated or result.boundaries_removed or result.boundaries_added:
                    warnings.append(
                        f"LLM 보정: 제목 {result.titles_updated}건 개선, "
                        f"경계 {result.boundaries_removed}건 제거, "
                        f"{result.boundaries_added}건 추가")
            except Exception as e:
                warnings.append(f"LLM 검증 스킵: {str(e)[:100]}")

        with _jobs_lock:
            _jobs[job_id]["progress"] = "PDF 분할 중..."

        # 3. PDF 분할
        split_dir = os.path.join(work_dir, "split")
        os.makedirs(split_dir, exist_ok=True)
        base_name = Path(filename).stem
        results = split_pdf(
            source_path=pdf_path,
            boundaries=boundaries,
            output_dir=split_dir,
            gen="", week="", subject="", session="",
        )

        if not results:
            raise Exception("PDF 분할 결과가 없습니다.")

        # 4. ZIP 생성
        zip_path = os.path.join(work_dir, "result.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in results:
                zf.write(r["path"], r["filename"])

        zip_name = f"{safe_filename(base_name, 40)}_split.zip"

        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "progress": "완료!",
                "result_path": zip_path,
                "topic_count": len(results),
                "total_pages": total_pages,
                "warnings": warnings[:5],
                "quality_report": quality_report,
                "zip_name": zip_name,
                "finished_at": time.time(),
            })

    except Exception as e:
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "error",
                "progress": "실패",
                "error": str(e)[:300],
                "finished_at": time.time(),
            })
        # 에러 시 임시 디렉토리 정리
        shutil.rmtree(work_dir, ignore_errors=True)


# ─── API 엔드포인트 ─────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/split")
async def api_split(file: UploadFile = File(...)):
    """PDF 업로드 → job_id 즉시 반환 (비동기 처리)."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            413, f"파일이 너무 큽니다 (최대 {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    _cleanup_stale_jobs()

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "processing",
            "progress": "업로드 완료, 처리 시작...",
            "work_dir": None,
        }

    thread = threading.Thread(
        target=_process_job, args=(job_id, content, file.filename),
        daemon=True)
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    """Job 상태 폴링."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job을 찾을 수 없습니다.")

    resp = {
        "status": job["status"],
        "progress": job.get("progress", ""),
    }
    if job["status"] == "done":
        resp["topic_count"] = job["topic_count"]
        resp["total_pages"] = job["total_pages"]
        resp["warnings"] = "; ".join(job.get("warnings", []))
        resp["quality_report"] = job.get("quality_report", "")
    elif job["status"] == "error":
        resp["error"] = job.get("error", "알 수 없는 오류")

    return JSONResponse(resp)


@app.get("/api/download/{job_id}")
async def api_download(job_id: str):
    """완료된 Job의 ZIP 다운로드."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job을 찾을 수 없습니다.")
    if job["status"] != "done":
        raise HTTPException(400, "아직 처리 중이거나 실패한 작업입니다.")

    zip_path = job["result_path"]
    if not os.path.exists(zip_path):
        raise HTTPException(500, "결과 파일이 삭제되었습니다.")

    from urllib.parse import quote
    zip_name = job["zip_name"]

    def iter_file():
        with open(zip_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk
        # 다운로드 후 정리
        work_dir = job.get("work_dir")
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)

    return StreamingResponse(
        iter_file(),
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename*=UTF-8''{quote(zip_name)}",
            "X-Topic-Count": str(job["topic_count"]),
            "X-Total-Pages": str(job["total_pages"]),
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
