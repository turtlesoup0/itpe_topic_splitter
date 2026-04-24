# Sandbox 격리

PDF 파서(PyMuPDF/Tesseract/kordoc) 취약점으로 임의 코드 실행이 발생해도
피해를 최소화하기 위한 macOS sandbox-exec 프로파일.

## 프로파일

| 파일 | 용도 |
|---|---|
| `uvicorn.sb` | FastAPI 웹 서비스 격리 — 네트워크/파일 접근 화이트리스트 |

## 사용법

**수동 실행 테스트:**
```bash
sandbox-exec -f deploy/sandbox/uvicorn.sb \
  python3 -m uvicorn web.app:app --host 127.0.0.1 --port 8080
```

**launchd plist에 적용:**
`com.itpe.splitter.uvicorn.plist`의 ProgramArguments를 다음으로 교체:
```xml
<array>
    <string>/usr/bin/sandbox-exec</string>
    <string>-f</string>
    <string>/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter/deploy/sandbox/uvicorn.sb</string>
    <string>/opt/homebrew/bin/python3</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>web.app:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8080</string>
</array>
```

## 허용된 리소스

**파일 쓰기** (다음 경로만):
- `~/Projects/itpe-topic-splitter/` (코드, 로그)
- `~/.cache/itpe-splitter/` (OCR 캐시, Job DB)
- `~/Library/Logs/itpe-splitter/` (로그)
- `/tmp`, `/var/folders` (tempfile)

**네트워크**:
- 127.0.0.1:8080 (listen)
- 127.0.0.1:8090 (MLX 서버)
- HTTPS 443 (Anthropic API, HuggingFace)
- DNS 53

**프로세스 실행**:
- `/opt/homebrew/bin/node` (kordoc CLI)
- `/usr/bin/env`

## 거부된 리소스

- `~/.ssh/`, `~/Library/Keychains/` 등 민감 디렉토리
- 임의 네트워크 포트 (포트 스캔, C2 통신 차단)
- 기타 바이너리 exec

## 디버깅

프로파일 변경 후 테스트 시 sandbox 거부 로그:
```bash
log stream --predicate 'process == "uvicorn" or process == "Python"' --style compact | grep -i sandbox
```

특정 작업이 차단되면 거부 로그를 보고 `allow` 룰 추가.

## 주의

- `sandbox-exec`는 Apple에서 **deprecated** 표시됐으나 여전히 동작함
- 대안: App Sandbox (requires signed app) — 과한 비용
- 현재 버전은 **defense in depth**: 1차 방어 (입력 검증) 실패 시 피해 확산 차단
