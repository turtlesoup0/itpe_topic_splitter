# 보안 아키텍처

이 프로젝트는 외부 PDF를 로컬 머신에서 처리하므로 복합적 위협 벡터를 방어합니다.
MECE 관점에서 입력·처리·저장·통신·출력·실행환경·공급망 7축으로 분류하여
다층 방어(Defense in Depth)를 적용.

## 위협 모델 vs 방어 계층

| 위협 범주 | 주요 위협 | 방어 계층 |
|---|---|---|
| **A. 입력** | 악성 PDF, 프롬프트 인젝션, DoS | Magic bytes, 페이지 상한, Rate limit, `<DOCUMENT>` delimiter |
| **B. 처리** | 파서 CVE, 경로 탈출 | sandbox-exec, `safe_filename`, subprocess 배열 호출 |
| **C. LLM** | API 키 유출, 외부 전송 | `.env` gitignore, MLX 로컬 기본, prompt guard |
| **D. 저장** | 직렬화 공격, SQL 인젝션 | JSON 캐시 (pickle 금지), parameterized SQL |
| **E. 네트워크** | 익명 접근, DDoS | Cloudflare Access OAuth, Rate limit, API 토큰 |
| **F. 출력** | Zip slip, XSS | `safe_filename`, JSON 응답만 |
| **G. 실행환경** | 권한 상승 | 사용자 권한 실행, sandbox-exec |
| **H. 공급망** | 의존성 CVE | Dependabot, pip-audit 주간 |

## Phase별 적용 내역

### Phase 1: 즉시 적용 (커밋 `b727ea8`)

- **Rate limiting** (slowapi, IP 기반)
  - `/api/split` 5/분, `/api/status` 60/분, `/api/download` 20/시간
- **PDF magic bytes 검증** — 첫 5바이트 `%PDF-` 확인
- **페이지 수 상한** — `MAX_PDF_PAGES=500`

### Phase 2: 단기 (커밋 `cc72128`)

- **프롬프트 인젝션 방어**
  - `_INJECTION_GUARD` 4개 시스템 프롬프트에 부착
  - `<DOCUMENT>...</DOCUMENT>` delimiter로 untrusted 영역 표시
  - 역할 변경/출력 형식 변경 시도 명시적 거부
- **API 토큰 인증**
  - `ITPE_API_TOKEN` 환경변수 → `Authorization: Bearer <token>`
  - `hmac.compare_digest` 상수시간 비교
  - 미설정 시 공개 모드 (점진 도입)

### Phase 3: 중기 (커밋 `5c7602b`)

- **macOS sandbox-exec**
  - `deploy/sandbox/uvicorn.sb` — 파일 쓰기/네트워크 화이트리스트
  - 취약점으로 RCE 발생 시 피해 확산 차단
- **Dependabot** (`.github/dependabot.yml`)
  - 주간 pip/github-actions CVE 자동 PR
- **Security Audit 워크플로** (`.github/workflows/security-audit.yml`)
  - pip-audit + bandit 주간/PR 자동 실행

### Phase 4: 장기

- **Cloudflare Access OAuth** (`deploy/cloudflare-access/README.md`)
  - 앱 진입 자체를 이메일/OAuth 로그인 뒤로 이동
  - 대시보드 설정 5분, 무료 플랜 50 사용자

## 방어 깊이 (Layer)

```
Request → Cloudflare WAF/DDoS (L1)
       → Cloudflare Access OAuth (L2, Phase 4)
       → Cloudflare Tunnel (QUIC, 공개 IP 없음) (L3)
       → FastAPI Rate Limit (L4, Phase 1)
       → API Token Bearer (L5, Phase 2)
       → Magic Bytes + 페이지 상한 (L6, Phase 1)
       → Prompt Injection Guard (L7, Phase 2)
       → Sandbox-exec 프로세스 격리 (L8, Phase 3)
       → App Logic (파싱/LLM/분할)
```

## 운영 체크리스트

### 초기 설정 (1회)
- [ ] `ITPE_API_TOKEN` 생성 (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)
- [ ] `.env`에 토큰 기록 (gitignored)
- [ ] Cloudflare Access Application 등록 (Phase 4)
- [ ] launchd plist에 `sandbox-exec` 경로 적용 (선택)

### 정기 운영
- [ ] 주간 Dependabot PR 리뷰 & 머지
- [ ] 월간 `pip-audit -r requirements.txt` 수동 확인
- [ ] 분기별 의존성 minor 버전 업데이트
- [ ] 분기별 Access Policy 이메일 목록 감사

### 사고 대응
1. 악성 업로드 의심 → `_jobs` DB에서 job_id 특정 → work_dir 격리 보관
2. Rate limit 돌파 → Cloudflare WAF 규칙 수동 추가
3. MLX 비정상 → `pgrep mlx_lm | xargs kill -9` + launchd 재기동
4. API 토큰 유출 의심 → `.env` 토큰 재생성 + 재배포

## 보안 취약점 신고

프로덕션 배포가 아니라면 GitHub Issue에 비공개로 보고.
프로덕션 시 `turtlesoup0@gmail.com`으로 직접 이메일.
