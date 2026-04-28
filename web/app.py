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
import json
import time
import uuid
import sqlite3
import zipfile
import tempfile
import shutil
import traceback
import threading
from pathlib import Path

import hmac
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

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
from diagnose_itpe_mock import (  # noqa: E402
    is_itpe_mock_pdf, split_itpe_mock,
)
from diagnose_kpc_mock import (  # noqa: E402
    is_kpc_mock_pdf, split_kpc_mock,
)
from detect_boundaries_v2 import (  # noqa: E402
    detect_boundaries_v2, detect_sessions, analyze_quality,
)
from llm_verifier import (  # noqa: E402
    enhance_boundaries_sync,
    is_available as llm_available,
    detect_boundaries_llm,
)

# 업로드 크기 제한 (50MB)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
# 페이지 수 상한 (정상 답안지 최대 ~200p, 여유분 포함)
MAX_PDF_PAGES = 500
# PDF 매직 바이트 (파일 타입 검증)
_PDF_MAGIC = b"%PDF-"

# ─── Rate limiting ────────────────────────────────────────────────
# 단일 IP DoS 방어. 업로드는 엄격, 상태 조회는 폴링 고려해 관대.
#   /api/split:           5회/분
#   /api/status/{id}:    60회/분 (2초 폴링 × 30분)
#   /api/download/{id}:  20회/시간
limiter = Limiter(key_func=get_remote_address)


# ─── API 토큰 인증 ───────────────────────────────────────────────
# ITPE_API_TOKEN 환경변수 설정 시 활성화.
# 미설정 시 공개 모드 (기존 동작 유지).
# 설정 시 /api/split 호출에 `Authorization: Bearer <token>` 필수.
_API_TOKEN = os.environ.get("ITPE_API_TOKEN", "").strip()


def _require_token(authorization: Optional[str] = None) -> None:
    """토큰 설정됐으면 검증. 안됐으면 공개 모드로 허용."""
    if not _API_TOKEN:
        return  # 토큰 미설정 → 공개 모드
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization: Bearer <token> 헤더 필요")
    provided = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(provided, _API_TOKEN):
        raise HTTPException(403, "잘못된 API 토큰")

app = FastAPI(title="ITPE Topic Splitter", version="1.3")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.on_event("startup")
async def _on_startup():
    """서버 기동 시 SQLite 초기화 + 완료 Job 복원."""
    _db_init()

# 정적 파일 서빙 (index.html)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def add_no_cache_for_html(request: Request, call_next):
    """HTML(SPA 진입점)이 사용자 브라우저에 stale로 남지 않도록 매 요청 ETag 재검증.
    배포 직후 사용자가 강제 새로고침 없이도 최신 화면을 보게 한다."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# ─── 비동기 Job 관리 ─────────────────────────────────────────────
# job_id → { status, progress, result_path, topic_count, total_pages,
#             warnings, quality_report, zip_name, error, work_dir }
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL_SEC = 30 * 60  # 완료/실패 후 30분 경과 시 자동 정리

# ─── SQLite 영속화 레이어 ────────────────────────────────────────
# uvicorn 재시작 후에도 완료된 Job을 복원 가능.
# _jobs 딕셔너리는 여전히 truth source이고 DB는 백업.
_DB_PATH = (Path(os.environ.get("XDG_CACHE_HOME")
                 or str(Path.home() / ".cache"))
            / "itpe-splitter" / "jobs.db")
_db_conn: sqlite3.Connection | None = None


def _db_init() -> None:
    """DB 파일/스키마 초기화 + 기동 시점 복원."""
    global _db_conn
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = sqlite3.connect(
            str(_DB_PATH), check_same_thread=False,
            isolation_level=None)  # autocommit
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              progress TEXT,
              work_dir TEXT,
              result_path TEXT,
              zip_name TEXT,
              topic_count INTEGER,
              total_pages INTEGER,
              warnings_json TEXT,
              quality_report TEXT,
              error TEXT,
              created_at REAL,
              finished_at REAL
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_finished ON jobs(finished_at)")
    except Exception as e:
        print(f"[DB] 초기화 실패 — 영속화 비활성: {e}")
        _db_conn = None
        return

    # 기동 시 진행 중인 Job은 서버 재시작으로 중단 → error 전환
    # 완료된 Job(done)은 메모리에 복원하여 /api/download 재접근 가능
    try:
        now = time.time()
        _db_conn.execute(
            "UPDATE jobs SET status='error', "
            "error='서버 재시작으로 중단됨', finished_at=? "
            "WHERE status='processing'", (now,))
        cursor = _db_conn.execute(
            "SELECT job_id, status, progress, work_dir, result_path, "
            "zip_name, topic_count, total_pages, warnings_json, "
            "quality_report, error, created_at, finished_at "
            "FROM jobs WHERE status='done' AND finished_at > ?",
            (now - _JOB_TTL_SEC,))
        restored = 0
        for row in cursor.fetchall():
            (jid, status, progress, work_dir, result_path, zip_name,
             topic_count, total_pages, warnings_json, quality_report,
             error, created_at, finished_at) = row
            # result.zip 이 실제로 남아있어야만 복원 가치 있음
            if not result_path or not os.path.exists(result_path):
                continue
            _jobs[jid] = {
                "status": status, "progress": progress,
                "work_dir": work_dir, "result_path": result_path,
                "zip_name": zip_name, "topic_count": topic_count,
                "total_pages": total_pages,
                "warnings": json.loads(warnings_json or "[]"),
                "quality_report": quality_report or "",
                "error": error, "created_at": created_at,
                "finished_at": finished_at,
            }
            restored += 1
        if restored:
            print(f"[DB] 재시작 후 {restored}개 완료 Job 복원")
    except Exception as e:
        print(f"[DB] 복원 실패: {e}")


def _db_upsert_locked(job_id: str) -> None:
    """_jobs_lock 보유 상태에서 호출. 메모리 상태를 DB에 기록."""
    if _db_conn is None:
        return
    job = _jobs.get(job_id)
    if job is None:
        return
    try:
        _db_conn.execute(
            "INSERT INTO jobs "
            "(job_id, status, progress, work_dir, result_path, zip_name, "
            " topic_count, total_pages, warnings_json, quality_report, "
            " error, created_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "  status=excluded.status, progress=excluded.progress, "
            "  work_dir=excluded.work_dir, result_path=excluded.result_path, "
            "  zip_name=excluded.zip_name, topic_count=excluded.topic_count, "
            "  total_pages=excluded.total_pages, "
            "  warnings_json=excluded.warnings_json, "
            "  quality_report=excluded.quality_report, "
            "  error=excluded.error, finished_at=excluded.finished_at",
            (job_id, job.get("status"), job.get("progress"),
             job.get("work_dir"), job.get("result_path"),
             job.get("zip_name"), job.get("topic_count"),
             job.get("total_pages"),
             json.dumps(job.get("warnings", []), ensure_ascii=False),
             job.get("quality_report"), job.get("error"),
             job.get("created_at"), job.get("finished_at")))
    except Exception as e:
        # DB 오류는 조용히 무시 (메모리 상태는 이미 갱신됨)
        print(f"[DB] upsert 실패 {job_id}: {e}")


def _db_delete(job_id: str) -> None:
    if _db_conn is None:
        return
    try:
        _db_conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    except Exception:
        pass


def _cleanup_stale_jobs():
    """TTL 초과 Job의 임시 디렉토리 정리 및 메모리/DB 해제."""
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
            _db_delete(jid)


def _process_job(job_id: str, pdf_content: bytes, filename: str):
    """백그라운드에서 PDF 분할 처리."""
    work_dir = tempfile.mkdtemp(prefix="itpe_split_")
    try:
        with _jobs_lock:
            _jobs[job_id]["work_dir"] = work_dir
            _jobs[job_id]["progress"] = "PDF 파싱 중..."
            _db_upsert_locked(job_id)

        pdf_path = os.path.join(work_dir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_content)

        # 0. ITPE 모의고사 해설집은 결정적 파서 우선 시도 (v2 + LLM 우회)
        from pathlib import Path as _Path
        original_path = _Path(work_dir) / filename
        try:
            shutil.copyfile(pdf_path, original_path)
        except Exception:
            original_path = _Path(pdf_path)
        def _finalize_mock(mock_result: dict, label: str) -> bool:
            """모의고사 결정적 파서 결과를 ZIP으로 마무리. ok=True 반환 시 즉시 return 해야 함."""
            if not (mock_result["ok"] and mock_result["files"]):
                return False
            zip_path = os.path.join(work_dir, "result.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for r in mock_result["files"]:
                    zf.write(r["path"], r["filename"])
            base_name = _Path(filename).stem
            zip_name = f"{safe_filename(base_name, 40)}_split.zip"
            qr_lines = [
                f"탐지 방식: {label} 결정적 파서",
                mock_result["summary"],
            ]
            if mock_result["warnings"]:
                qr_lines.append("주의:")
                qr_lines.extend(f"  - {w}" for w in mock_result["warnings"])
            with _jobs_lock:
                _jobs[job_id].update({
                    "status": "done",
                    "progress": "완료!",
                    "result_path": zip_path,
                    "topic_count": len(mock_result["files"]),
                    "total_pages": 0,
                    "warnings": mock_result["warnings"][:5],
                    "quality_report": "\n".join(qr_lines),
                    "zip_name": zip_name,
                    "topics": mock_result.get("topics", []),
                    "finished_at": time.time(),
                })
                _db_upsert_locked(job_id)
            return True

        if is_itpe_mock_pdf(original_path):
            with _jobs_lock:
                _jobs[job_id]["progress"] = "ITPE 모의고사 결정적 분할 중..."
                _db_upsert_locked(job_id)
            mock_result = split_itpe_mock(original_path, _Path(work_dir) / "itpe_mock_split")
            if _finalize_mock(mock_result, "ITPE 모의고사"):
                return

        if is_kpc_mock_pdf(original_path):
            with _jobs_lock:
                _jobs[job_id]["progress"] = "KPC 모의고사 결정적 분할 중..."
                _db_upsert_locked(job_id)
            mock_result = split_kpc_mock(original_path, _Path(work_dir) / "kpc_mock_split")
            if _finalize_mock(mock_result, "KPC 모의고사"):
                return

        # 1. kordoc 파싱
        elements, total_pages = parse_kordoc(pdf_path)
        if not elements:
            raise Exception("PDF에서 텍스트를 추출할 수 없습니다.")

        with _jobs_lock:
            _jobs[job_id]["progress"] = "토픽 경계 탐지 중..."
            _db_upsert_locked(job_id)

        # 2. LLM-first 경계 탐지 시도 → 실패 시 규칙 기반 fallback
        boundaries = None
        warnings: list[str] = []
        quality_report = ""
        if llm_available():
            try:
                with _jobs_lock:
                    _jobs[job_id]["progress"] = "LLM 경계 탐지 중..."
                    _db_upsert_locked(job_id)
                # 파일명에서 단일 세션 힌트 추출:
                #   "<이름>_N교시_..." or "<이름>_N교시.pdf" → 단일 세션
                import re as _re
                single_hint = bool(_re.search(
                    r'[_\-\s]\d\s*교시(?:[_\-\s]|\.pdf$)', filename))
                llm_result = detect_boundaries_llm(
                    elements, total_pages,
                    single_session_hint=single_hint)
                if llm_result is not None:
                    boundaries, llm_warnings = llm_result
                    warnings.extend(llm_warnings)
                    quality_report = (
                        f"탐지 방식: LLM 우선\n"
                        f"총 {total_pages}p → {len(boundaries)}개 토픽\n"
                        f"세션 분포: " + ", ".join(
                            f"{s}교시={sum(1 for b in boundaries if b['session']==s)}개"
                            for s in sorted({b['session'] for b in boundaries}))
                    )
            except Exception as e:
                warnings.append(f"LLM-first 스킵: {str(e)[:100]}")

        # 규칙 기반 fallback (LLM 실패 또는 미사용)
        if not boundaries:
            boundaries_v2, rule_warnings = detect_boundaries_v2(
                elements, total_pages, "")
            warnings.extend(rule_warnings)
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
                _db_upsert_locked(job_id)
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
            _db_upsert_locked(job_id)

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

        # boundaries → topics 매핑 (v2 분기 공통)
        v2_topics = [
            {
                "session": b.get("session", 0),
                "num": b.get("num", 0),
                "title": b.get("title", ""),
                "page_start": b.get("page_start", 0),
                "page_end": b.get("page_end", 0),
                "pages": (b.get("page_end", 0) - b.get("page_start", 0) + 1)
                if b.get("page_end") and b.get("page_start") else 0,
            }
            for b in boundaries
        ]
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
                "topics": v2_topics,
                "finished_at": time.time(),
            })
            _db_upsert_locked(job_id)

    except Exception as e:
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "error",
                "progress": "실패",
                "error": str(e)[:300],
                "finished_at": time.time(),
            })
            _db_upsert_locked(job_id)
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
@limiter.limit("5/minute")
async def api_split(request: Request, file: UploadFile = File(...),
                    authorization: Optional[str] = Header(default=None)):
    """PDF 업로드 → job_id 즉시 반환 (비동기 처리).

    ITPE_API_TOKEN 환경변수 설정 시 Bearer 토큰 필수.
    """
    # 0. 토큰 검증 (활성화된 경우)
    _require_token(authorization)

    # 1. 확장자 검증
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    # 2. 크기 제한 (50MB)
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            413, f"파일이 너무 큽니다 (최대 {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    # 3. Magic bytes 검증 (확장자만 신뢰 금지)
    if not content.startswith(_PDF_MAGIC):
        raise HTTPException(
            400, "유효한 PDF 파일이 아닙니다 (PDF 시그니처 불일치).")

    # 4. 페이지 수 상한 검증 (DoS 방어)
    try:
        import fitz as _fitz
        with _fitz.open(stream=content, filetype="pdf") as _doc:
            _pages = _doc.page_count
        if _pages > MAX_PDF_PAGES:
            raise HTTPException(
                413, f"페이지 수 초과: {_pages}p (최대 {MAX_PDF_PAGES}p)")
        if _pages < 1:
            raise HTTPException(400, "빈 PDF입니다.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"PDF 파싱 실패: {str(e)[:100]}")

    _cleanup_stale_jobs()

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "processing",
            "progress": "업로드 완료, 처리 시작...",
            "work_dir": None,
            "created_at": time.time(),
        }
        _db_upsert_locked(job_id)

    thread = threading.Thread(
        target=_process_job, args=(job_id, content, file.filename),
        daemon=True)
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
@limiter.limit("60/minute")
async def api_status(request: Request, job_id: str):
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
        resp["topics"] = job.get("topics", [])
    elif job["status"] == "error":
        resp["error"] = job.get("error", "알 수 없는 오류")

    return JSONResponse(resp)


@app.get("/api/download/{job_id}")
@limiter.limit("20/hour")
async def api_download(request: Request, job_id: str):
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
        _db_delete(job_id)

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
