# Cloudflare Access OAuth 통합

공개 엔드포인트를 **이메일/OAuth 로그인** 뒤에 배치하여 익명 접근을 차단.
Phase 2 API 토큰보다 상위 계층 — 브라우저 UI 접근 자체를 인증 뒤로 숨김.

## 필요 조건

- Cloudflare 계정 (Zero Trust 모듈 활성화, 개인용 **무료** 50 사용자까지)
- tech-insight.org 도메인 Cloudflare 관리 (Phase 4 전제는 이미 충족)
- topic-splitter.tech-insight.org Named Tunnel 가동 중

## 설정 절차 (대시보드, 약 5분)

### 1. Zero Trust 활성화
https://one.dash.cloudflare.com/ 접속 → "Zero Trust" 대시보드 이동.
최초 접속 시 team name 요구 (예: `turtlesoup0-team`) → URL은 `https://<team>.cloudflareaccess.com`.

### 2. Application 등록
**Zero Trust → Access → Applications → Add an application → Self-hosted**

| 필드 | 값 |
|---|---|
| Application name | ITPE Topic Splitter |
| Session duration | 24 hours |
| Application domain | `topic-splitter.tech-insight.org` |

### 3. Identity Provider 설정
**Settings → Authentication → Login methods → Add new**

권장 (하나 이상):
- **One-time PIN** — 이메일로 코드 전송 (설정 불필요, 즉시 사용 가능)
- **Google** — 무료, 가장 편함
- **GitHub** — 개발자 친화

One-time PIN 만 사용해도 충분. 추가 OAuth는 선택.

### 4. Access Policy 작성
Application 상세 → **Policies → Add a policy**

**Include** (허용 조건):
| 유형 | 값 | 설명 |
|---|---|---|
| Emails | `turtlesoup0@gmail.com` | 본인 |
| Emails ending in | `@company.com` | 허용 도메인 |
| Everyone | — | **절대 사용 금지** (공개 무인증 == 미설정과 동일) |

**Require** (필수 조건, 선택):
- Country: Korea — 지역 잠금 (선택)

**Exclude** (차단):
- Country: RU, CN, KP 등 (선택)

### 5. (선택) Service Token — 자동 업로드용
프로그램이 Cloudflare Access 뒤에서 POST 하려면 Service Token:

**Access → Service Auth → Service Tokens → Create**
- Name: `itpe-splitter-bot`
- Duration: 1 year

생성된 `CF-Access-Client-Id`, `CF-Access-Client-Secret`을 스크립트에 사용:

```python
import httpx

resp = httpx.post(
    "https://topic-splitter.tech-insight.org/api/split",
    headers={
        "CF-Access-Client-Id": "xxx.access",
        "CF-Access-Client-Secret": "secret_xxx",
        "Authorization": f"Bearer {ITPE_API_TOKEN}",  # Phase 2 토큰 유지
    },
    files={"file": open("my.pdf", "rb")},
)
```

Application의 Service Auth policy에서 해당 Service Token 허용 필요.

## 검증

### 브라우저 접근
https://topic-splitter.tech-insight.org 접속 →  
→ Cloudflare Access 로그인 화면으로 리다이렉트  
→ 이메일 입력 → OTP 코드 수신 → 로그인 → 원래 앱 화면 도달

### API 자동화 접근
Service Token 없이 curl → 302/401 (Cloudflare Access 로그인 페이지).  
Service Token 있으면 Phase 2 토큰과 조합해 정상 POST.

## 보안 계층 정리

**요청이 앱에 도달하기까지:**

1. **Cloudflare Edge** (WAF, DDoS 1차 방어)
2. **Cloudflare Access** (이메일/OAuth 로그인 또는 Service Token) ← Phase 4
3. **Cloudflare Tunnel** (QUIC, 공개 IP 없음)
4. **FastAPI Rate Limit** (IP별 분당 제한) ← Phase 1
5. **API Token** (`Authorization: Bearer`) ← Phase 2
6. **PDF Magic Bytes + 페이지 상한** ← Phase 1
7. **Prompt Injection Guard** ← Phase 2
8. **Sandbox-exec 프로세스 격리** ← Phase 3

## 대안: Application-layer OAuth (Authlib)

Cloudflare Access를 안 쓰고 FastAPI 자체에 OAuth 구현 가능:
- `authlib` + Google OAuth 2.0
- 세션 관리 복잡, 소규모에 과잉
- Cloudflare Access가 훨씬 단순 (설정 10분)

따라서 Cloudflare Access 권장.

## 비용

- **무료 플랜**: 50 사용자까지 Zero Trust Access 무료
- 추가 사용자 필요 시 Pay-as-you-go 과금 ($3/user/month)
- 개인 + 소수 팀 사용이라면 무료 한도로 충분
