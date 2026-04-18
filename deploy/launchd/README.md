# Mac launchd 자동 기동 설정

재부팅 후 3개 서비스(uvicorn, mlx-lm, cloudflared)를 자동 기동.

## 구성

| Plist | 역할 | 포트 |
|---|---|---|
| `com.itpe.splitter.mlx.plist` | MLX-LM 서버 (SuperGemma4) | 8090 |
| `com.itpe.splitter.uvicorn.plist` | FastAPI 웹 서비스 | 8080 |
| `com.itpe.splitter.cloudflared.plist` | Cloudflare 임시 터널 | — |

Crash 시 자동 재기동(`KeepAlive.SuccessfulExit=false`), 기동 시 10초 throttle.

## 설치

```bash
# 1. LaunchAgents 디렉토리로 복사
cp deploy/launchd/*.plist ~/Library/LaunchAgents/

# 2. 로그 디렉토리 준비
mkdir -p ~/Library/Logs/itpe-splitter

# 3. 수동 프로세스 종료 (이미 돌고 있다면)
pkill -f "uvicorn web.app"
pkill -f "mlx_lm server"
pkill -f "cloudflared tunnel --url http://127.0.0.1:8080"

# 4. launchd 등록 + 즉시 기동
launchctl load ~/Library/LaunchAgents/com.itpe.splitter.mlx.plist
launchctl load ~/Library/LaunchAgents/com.itpe.splitter.uvicorn.plist
launchctl load ~/Library/LaunchAgents/com.itpe.splitter.cloudflared.plist

# 5. 확인
launchctl list | grep itpe
curl -s http://localhost:8080/health
```

## 상태 확인

```bash
# 기동 상태 (PID/ExitCode)
launchctl list | grep itpe

# 로그
tail -f ~/Library/Logs/itpe-splitter/uvicorn.log
tail -f ~/Library/Logs/itpe-splitter/mlx.log
tail -f ~/Library/Logs/itpe-splitter/cloudflared.err.log

# 현재 cloudflared 터널 URL (기동마다 변경됨)
grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" \
  ~/Library/Logs/itpe-splitter/cloudflared.err.log | tail -1
```

## 해제

```bash
launchctl unload ~/Library/LaunchAgents/com.itpe.splitter.mlx.plist
launchctl unload ~/Library/LaunchAgents/com.itpe.splitter.uvicorn.plist
launchctl unload ~/Library/LaunchAgents/com.itpe.splitter.cloudflared.plist

# 완전 제거 시
rm ~/Library/LaunchAgents/com.itpe.splitter.*.plist
```

## 경로 변경 시

세 plist 모두 **절대 경로**를 사용합니다 (`/Users/turtlesoup0-macmini/Projects/itpe-topic-splitter`).
프로젝트 위치가 바뀌면 plist를 편집한 뒤 `launchctl unload` + `load` 재실행.

## Provider 전환

`com.itpe.splitter.uvicorn.plist`의 `EnvironmentVariables.LLM_PROVIDER`를 변경:

- `mlx` (기본): 로컬 SuperGemma4 사용 (MLX 서버 필요)
- `anthropic`: Haiku API 사용 (`.env`에 `ANTHROPIC_API_KEY` 필요)

변경 후 `launchctl unload ... && launchctl load ...` 필수.
