# Railway 배포 가이드

구성: **서비스 1개**(이 저장소의 `Dockerfile`) · **레플리카 1개** · `/data`에 붙인 **Volume**에 SQLite · Railway가 주는 HTTPS 도메인.

> **레플리카는 반드시 1개로 유지한다.** 알림 예약·저녁 체크인·자정 포인트·DB 백업을 하는 APScheduler가 서버 프로세스 안에서 돈다.
> 프로세스가 2개면 모든 알림이 두 번 가고 자정 작업이 중복된다. 컨테이너 안에서도 uvicorn 워커는 1개로 고정되어 있다
> (`docker/entrypoint.sh`). Railway도 "Volume이 붙은 서비스는 레플리카를 쓸 수 없다"고 명시한다(출처 [1]).

표기: **[문서]** = Railway/OpenAI 공식 문서로 확인한 내용(번호는 맨 아래 출처), **[추정]** = 공식 문서로 확인하지 못한 내용.

---

## 0. 이 저장소가 배포를 위해 하는 일

| 항목 | 동작 |
|---|---|
| 포트 | `PORT` 환경변수로 `0.0.0.0`에서 뜬다(Railway가 넣어 준다, 없으면 8000). 헬스체크도 같은 포트로 온다 **[문서 4]** |
| 시작 순서 | `prepare_volume`(DB·백업 폴더 생성) → `alembic upgrade head` → uvicorn. 마이그레이션이 실패하면 서버를 띄우지 않고 컨테이너가 종료된다(배포 실패로 표시됨) |
| 워커 | uvicorn `--workers 1` 고정 |
| 볼륨 권한 | Railway 볼륨은 root 소유로 붙어 일반 사용자 이미지에서 권한 문제가 생긴다 **[문서 1]**. 이 이미지는 root로 시작해 `/data`를 만들고 소유자를 앱 사용자(uid 1000)로 바꾼 뒤, `setpriv`로 권한을 낮춰 서버를 실행한다. 그래서 Railway가 안내하는 `RAILWAY_RUN_UID=0`(컨테이너 전체를 root로 실행)은 **필요 없다**. 이 방식이 Railway 볼륨에서 그대로 동작하는지는 로컬 Docker 볼륨(root 소유)으로만 확인했다 **[추정]** — 첫 배포에서 권한 오류가 나면 `RAILWAY_RUN_UID=0`을 넣으면 된다 **[문서 1]** |
| 설정 파일 | `railway.json`: Dockerfile 빌드, 헬스체크 `/health`(120초), 실패 시 재시작. 코드의 설정이 대시보드 설정보다 우선한다 **[문서 5]** |
| 스케줄러 | `RUN_SCHEDULER`(기본 `true`). 끄면 알림·체크인·자정 포인트·백업이 돌지 않는다 |
| 시간대 | 스케줄러가 `APP_TIMEZONE` 기준으로 돈다(컨테이너 기본 시간대 UTC가 아니라) |
| DB 백업 | 매일 04:30(`APP_TIMEZONE`)에 SQLite backup API로 `/data/backups/schreduler-YYYY-MM-DD.db`를 만들고 최근 14개만 남긴다. 수동: `python -m app.scripts.backup_db` |
| 비밀값 | `.dockerignore`가 `.env`, `*.db`, `*.db.bak*`, `secrets/`, `tests/`(평가 결과 포함), 로컬 확인용 파일을 이미지에서 뺀다. Firebase 서비스 계정은 파일 대신 `FIREBASE_CREDENTIALS_JSON` 환경변수로 넣는다 |

---

## 1. Railway 화면에서 할 일 (순서대로)

### 1-1. 계정과 플랜
1. railway.com에서 GitHub 계정으로 가입한다.
2. **Hobby 플랜**($5/월, 월 $5 사용량 포함)으로 올린다 **[문서 6]**. Hobby의 볼륨 기본 용량은 5GB **[문서 1, 6]**.

### 1-2. 프로젝트와 서비스
1. **New Project → Deploy from GitHub repo** → 이 저장소를 고른다. Railway가 `railway.json`을 읽어 `Dockerfile`로 빌드한다 **[문서 5]**.
2. 첫 빌드는 환경변수가 없어 시작 직후 실패하거나 SQLite가 컨테이너 안(재배포 때 사라짐)에 생긴다. 아래 1-3, 1-4를 마치고 다시 배포한다.

### 1-3. Volume 추가
1. 서비스를 우클릭(또는 Command Palette) → **Add Volume** → 마운트 경로 **`/data`**.
2. 볼륨이 붙은 서비스는 재배포 때 짧은 중단이 생긴다(두 배포가 같은 볼륨을 동시에 마운트할 수 없음) **[문서 4]**.

### 1-4. 환경변수 (Variables 탭 → Raw Editor에 붙여 넣기 **[문서 7]**)

🔒 = 비밀값. Variables에서 ⋮ → **Seal**하면 UI·API에서 다시 볼 수 없다(되돌릴 수 없고, `railway run`/`railway variables`에서도 빠진다) **[문서 7]**.

| 변수 | 값 | 비고 |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/schreduler.db` | 슬래시 4개(절대 경로) |
| `LLM_API_KEY` 🔒 | OpenAI API 키 | |
| `LLM_MODEL` | `gpt-5.6-luna` | 가격표(`app/services/llm_pricing.py`)에 없는 모델이면 시작 실패 |
| `ASSISTANT_MODEL` | `gpt-5.6-luna` | |
| `ASSISTANT_REASONING_EFFORT` | `medium` | |
| `LLM_DAILY_BUDGET_PER_USER_USD` | `1.00` | |
| `LLM_DAILY_BUDGET_TOTAL_USD` | `5.00` | |
| `LLM_DAILY_BUDGET_ADMIN_USD` | (선택) | |
| `APP_TIMEZONE` | `America/Toronto` | |
| `SIGNUP_MODE` | `invite` | 처음엔 `closed`로 두고 관리자만 만든 뒤 바꿔도 된다 |
| `INVITE_CODE` 🔒 | 길고 추측하기 어려운 값 | 비어 있으면 아무도 가입 못 함 |
| `SESSION_COOKIE_SECURE` | `true` | |
| `ALLOWED_ORIGINS` | `https://<도메인>` | 1-5에서 도메인을 만든 뒤 채운다. 비어 있는데 `SESSION_COOKIE_SECURE=true`면 시작 로그에 경고가 남는다 |
| `FORWARDED_ALLOW_IPS` | `*` | uvicorn이 Railway 프록시의 `X-Forwarded-Proto: https`를 믿게 한다(Railway는 이 헤더를 항상 `https`로 보낸다 **[문서 2]**). 앱은 보안 판단에 소켓 주소를 쓰지 않으므로 `*`여도 로그인 제한에는 영향이 없다 |
| `TRUST_PROXY_HEADERS` | `false` (기본) | 아래 2절 참고. 확인 전까지 끈 채로 둔다 |
| `RUN_SCHEDULER` | `true` (기본) | |
| `FIREBASE_CREDENTIALS_JSON` 🔒 | 서비스 계정 JSON 파일 내용 전체 | 없으면 푸시는 로그만 남긴다 |
| `TELEGRAM_BOT_TOKEN` 🔒 | (선택) | |
| `ENVIRONMENT` | `production` | |
| `LOG_LEVEL` | `INFO` | |

`PORT`는 Railway가 넣으므로 설정하지 않는다 **[문서 4]**. 변수를 바꾸면 "staged changes"가 생기고, **Deploy**를 눌러야 적용된다 **[문서 7]**.

### 1-5. 도메인
1. 서비스 → **Settings → Networking → Generate Domain** → `<이름>.up.railway.app` 형태의 HTTPS 도메인이 생긴다. TLS는 Railway 엣지에서 끝난다 **[문서 2]**.
2. `ALLOWED_ORIGINS=https://<그 도메인>`을 넣고 배포한다(앞에 `https://`, 끝에 `/` 없이).

### 1-6. 레플리카 1, 헬스체크
1. **Settings → Deploy → Replicas: 1** 그대로 둔다(볼륨이 붙어 있으면 늘릴 수도 없다 **[문서 1]**).
2. 헬스체크는 `railway.json`의 `/health`가 쓰인다. 배포 시작 때만 확인하고 계속 감시하지는 않는다 **[문서 4]**. 요청은 `healthcheck.railway.app` 호스트에서 온다 **[문서 4]**(GET이라 Origin 확인과 무관).

### 1-7. 첫 배포 후 관리자 만들기 (Railway 안에서 명령 실행)

SQLite가 Railway 볼륨 안에 있으므로, 로컬에서 실행하는 `railway run`이 아니라 **배포된 컨테이너 안에서** 실행해야 한다. Railway CLI의 `railway ssh`가 컨테이너 안에 셸을 연다 **[문서 8]**.

```bash
npm i -g @railway/cli        # 또는 공식 문서의 다른 설치 방법
railway login
railway link                 # 이 프로젝트·서비스 선택
railway ssh                  # 처음엔 SSH 키 등록을 안내한다 [문서 8]
# --- 컨테이너 안 ---
python -m app.scripts.seed_personas                          # 기본 페르소나 (새로 시작한 경우)
python -m app.scripts.create_admin --email you@example.com --new   # 빈 DB: 첫 관리자 만들기
#   기존 데이터를 옮긴 경우: --new 없이 → 기존 사용자 1번에 이메일·비밀번호를 붙인다
#   비밀번호를 잊으면: --reset
```

비밀번호는 터미널에서 두 번 입력한다(명령어 인자로 받지 않는다 — 셸 기록·프로세스 목록에 남지 않게). 한 줄 실행은 `railway ssh -- <명령>`도 되지만 **[문서 8]**, 비밀번호 입력이 필요하므로 대화형 셸에서 실행한다.

### 1-8. 로그 보기
- 대시보드: 서비스 → **Deployments** → 배포를 눌러 Build/Deploy 로그. HTTP 로그도 있다 **[문서 9]**.
- CLI: `railway logs`(실시간), `railway logs --lines 100`, `railway logs --build`, `railway logs --lines 50 --filter "@level:error"` **[문서 10]**.
- 이 앱이 남기는 주요 로그: `[알림]`(예약·발송), `[하루 체크인]`, `[포인트]`, `[backup]`, `[LLM budget]`, `[auth]`.

### 1-9. 재배포 · 롤백
- **재배포**: `main`에 push하면 자동 배포된다. 같은 커밋을 다시: Deployments → ⋮ → **Redeploy**.
- **롤백**: Deployments에서 이전 배포의 ⋮ → **Rollback**. 이전 배포의 이미지와 변수로 새 배포가 만들어지고 다시 빌드하지 않는다 **[문서 11]**.
- **주의**: 롤백은 **볼륨 데이터를 되돌리지 않는다**. DB 마이그레이션(`alembic/versions`)이 들어간 배포를 롤백하면 새 스키마 DB에 옛 코드가 붙는다. 스키마를 바꾸는 배포 직전에는 `python -m app.scripts.backup_db`를 실행하거나 Railway 볼륨 백업을 먼저 만든다.

---

## 2. 프록시 뒤의 클라이언트 IP (로그인 실패 제한)

로그인 실패 제한은 "같은 이메일" 또는 "같은 IP"로 10분에 10번 실패하면 잠시 막는다. Railway 프록시 뒤에서는 모든 연결이
프록시 주소에서 오므로, 소켓 주소로 IP를 세면 **누군가 10번 틀릴 때 모두가 막힌다**.

- 기본값 `TRUST_PROXY_HEADERS=false`: **IP 기준 제한을 끄고 이메일 기준만** 적용한다. 앱이 IP를 판단에 쓰지 않는다.
- `TRUST_PROXY_HEADERS=true`: `X-Forwarded-For`의 첫 값(없으면 `X-Real-IP`)을 클라이언트 IP로 보고 IP 기준 제한도 켠다.

공식 문서로 확인한 것과 못 한 것:

| 내용 | 근거 |
|---|---|
| Railway는 요청에 `X-Real-IP`(클라이언트 IP), `X-Forwarded-Proto`(항상 `https`), `X-Forwarded-Host`, `X-Railway-Edge`, `X-Request-Start`, `X-Railway-Request-Id`를 붙인다 | **[문서 2]** |
| 클라이언트가 직접 보낸 `X-Real-IP`·`X-Forwarded-For`를 엣지가 **지우거나 덮어쓰는지** | 공식 문서에 없음 — **확인 안 됨** |
| `X-Forwarded-For`는 엣지가 받은 값을 지우고 첫 값이 실제 접속 IP이며, `X-Real-IP`는 CDN이 켜진 경로에서 맞지 않을 수 있다 | Railway 커뮤니티(Central Station)의 답변 **[추정, 출처 12]** — 공식 문서 아님 |

그래서 **기본값은 끈 상태**로 둔다. 켜려면 먼저 아래처럼 위조가 막히는지 직접 확인한다:
1. 배포 후 임시로 `TRUST_PROXY_HEADERS=true`, `LOG_LEVEL=INFO`로 배포한다.
2. `curl -X POST https://<도메인>/auth/login -H "X-Forwarded-For: 1.2.3.4" -H "Content-Type: application/json" -d '{"email":"x@example.com","password":"wrong-password"}'`
3. `railway logs`의 `[auth] sign-in failed ip=...`에 `1.2.3.4`가 아니라 **내 실제 IP**가 찍히면 엣지가 헤더를 정리하는 것이므로 켜 둬도 된다. `1.2.3.4`가 찍히면 다시 `false`로 돌린다.

---

## 3. DB 백업

| 방법 | 내용 |
|---|---|
| 앱 자체 백업 | 매일 04:30(`APP_TIMEZONE`) `/data/backups/`에 날짜별 파일, 최근 14개 유지. 수동: `railway ssh -- python -m app.scripts.backup_db`. 같은 볼륨에 있으므로 볼륨 자체를 잃으면 함께 사라진다 |
| Railway 볼륨 백업 | 서비스 설정의 **Backups** 탭에서 일정 설정. 매일(6일 보관) / 매주(27일 보관) / 매월(89일 보관), 수동 백업도 가능. 증분·copy-on-write라 고유 데이터만 볼륨과 같은 단가로 과금 **[문서 3]**. 복원은 백업을 골라 Restore → 변경 사항 확인 후 Deploy, 새 볼륨이 만들어지고 기존 볼륨은 분리된다 **[문서 3]**. 수동 백업은 볼륨 크기의 50%까지, 볼륨을 wipe하면 백업도 지워진다 **[문서 3]**. Hobby 플랜 포함 여부는 문서에 명시되지 않았다 **[추정: 대시보드에서 확인]** |
| 로컬로 내려받기 | `scp <서비스 도메인>@ssh.railway.com:/data/backups/schreduler-YYYY-MM-DD.db ./` **[문서 8]** |

권장: 둘 다 켠다(앱 백업은 하루 단위 되돌리기용, Railway 백업은 볼륨 사고 대비).

---

## 4. 처음 데이터: "새로 시작" / "기존 데이터 옮기기"

### 4-1. 새로 시작
1. 1절대로 배포하면 시작할 때 마이그레이션이 빈 DB를 만든다.
2. `railway ssh` → `python -m app.scripts.seed_personas` → `python -m app.scripts.create_admin --email you@example.com --new`.
3. 다른 사람은 `SIGNUP_MODE=invite` + `INVITE_CODE`로 가입한다.

### 4-2. 기존 데이터 옮기기 (로컬 `schreduler.db` → `/data/schreduler.db`)

Railway SSH는 SFTP를 지원해서 `scp`로 파일을 올릴 수 있다 **[문서 8]**. 실행 중인 서버가 쓰고 있는 파일을 그냥 덮어쓰면 깨질 수 있으므로 아래 순서로 한다.

1. **로컬 준비**
   ```bash
   alembic upgrade head                                  # 배포할 코드와 같은 스키마로
   python -m app.scripts.backup_db --dir ./to-railway    # 일관된 스냅샷 (서버가 돌고 있어도 안전)
   ```
   이 파일에는 개인 데이터가 들어 있으니 저장소에 커밋하지 않는다(`.gitignore`, `.dockerignore` 대상).
2. **배포 쪽 준비**: 1절대로 한 번 배포해 볼륨이 생긴 상태에서, 덮어쓰기 전 안전을 위해 Railway 볼륨 백업을 하나 만든다 **[문서 3]**.
3. **업로드** (`<서비스 도메인>`은 `xxx.up.railway.app`) **[문서 8]**:
   ```bash
   scp ./to-railway/schreduler-YYYY-MM-DD.db <서비스 도메인>@ssh.railway.com:/data/incoming.db
   ```
   OpenSSH 9.0 미만이면 `scp -s` **[문서 8]**.
4. **교체** (`railway ssh`로 컨테이너 안에서). 서버가 열어 둔 파일을 `mv`로 바꾸지 말고, SQLite backup API로 내용을 덮어쓴다:
   ```bash
   python -m app.scripts.backup_db                       # 지금 배포 DB도 한 번 더 백업
   python -c "import sqlite3; s=sqlite3.connect('/data/incoming.db'); d=sqlite3.connect('/data/schreduler.db'); s.backup(d); d.close(); s.close()"
   chown 1000:1000 /data/schreduler.db 2>/dev/null; rm /data/incoming.db
   ```
5. **재시작**: Deployments → ⋮ → **Restart**(또는 Redeploy). 시작할 때 마이그레이션과 알림 예약이 새 데이터 기준으로 다시 돈다.
6. 기존 사용자 1번에는 이메일이 없으므로 `python -m app.scripts.create_admin --email you@example.com`(`--new` 없이)으로 로그인 정보를 붙인다.

4번의 `chown`과 SSH 세션 사용자(root인지)는 문서에서 확인하지 못했다 **[추정]**. 권한 오류가 나면 재시작만 해도 시작 단계(`prepare_volume`)가 소유자를 다시 맞춘다.

---

## 5. 배포 후 확인 목록

- [ ] `https://<도메인>/health` → `{"status":"ok"}`
- [ ] `https://<도메인>/app/` → 로그인 화면, 관리자 계정으로 로그인
- [ ] 할 일 탭에서 일정 하나 추가 → 캘린더에 보임
- [ ] 알림 로그: `railway logs --filter "알림"` 또는 대시보드 로그에 `[알림] 대기 중인 회차 N개의 알림 job을 등록했습니다`, 시각이 되면 `[알림][start]`
- [ ] 로컬에서 화면 자동 점검:
  ```bash
  pip install -r requirements-dev.txt
  SMOKE_EMAIL=you@example.com SMOKE_PASSWORD='...' python -m app.scripts.smoke_ui --base-url https://<도메인>
  ```
  (배포 서버에는 데이터를 만들지 않고 읽기만 한다. LLM 요청은 브라우저에서 막는다)
- [ ] 쿠키 Secure: 브라우저 개발자 도구 → Application → Cookies → `schreduler_session`에 **Secure**, **HttpOnly**, SameSite **Lax**. 또는
  `curl -si -X POST https://<도메인>/auth/login -H "Content-Type: application/json" -d '{"email":"...","password":"..."}' | grep -i set-cookie` 에 `Secure`
- [ ] 다른 사이트 Origin 거부:
  `curl -s -X POST https://<도메인>/auth/login -H "Origin: https://evil.example" -H "Content-Type: application/json" -d '{}'` → `403`
- [ ] 시작 로그에 `SESSION_COOKIE_SECURE=true but ALLOWED_ORIGINS is empty` 경고가 **없는지**
- [ ] 다음 날 `/data/backups/`에 백업 파일이 생겼는지: `railway ssh -- ls -l /data/backups`

---

## 6. 권장: OpenAI 프로젝트 월 예산 한도

앱의 하루 한도(`LLM_DAILY_BUDGET_*`)와 별도로, OpenAI 쪽에도 상한을 둔다.
OpenAI 플랫폼에서 조직 설정 → 이 앱의 **프로젝트** → **Limits** → Monthly spend limit과 알림 기준을 설정한다.
**hard limit**으로 두면 한도에 닿은 뒤 API 요청이 429로 실패한다(이 앱에서는 "AI 서버와 연결이 원활하지 않아요" 502로 보인다).
반영이 즉시가 아니라 조금 넘을 수 있다 **[문서 13]**. 이 앱 전용 프로젝트와 키를 따로 만들어 쓰는 것을 권장한다.

---

## 출처

1. Railway — Volumes: https://docs.railway.com/reference/volumes
2. Railway — Public Networking Specs & Limits (Request Headers): https://docs.railway.com/networking/public-networking/specs-and-limits
3. Railway — Volume Backups: https://docs.railway.com/volumes/backups
4. Railway — Healthchecks: https://docs.railway.com/reference/healthchecks
5. Railway — Config as Code reference: https://docs.railway.com/config-as-code/reference
6. Railway — Pricing Plans: https://docs.railway.com/pricing/plans
7. Railway — Variables (Raw Editor, Sealed variables, staged changes): https://docs.railway.com/variables
8. Railway — CLI `railway ssh` (scp/sftp 포함): https://docs.railway.com/cli/ssh
9. Railway — Debug a Production Incident with Logs, Metrics, and Traces: https://docs.railway.com/guides/debug-production-incident
10. Railway — CLI `railway logs`: https://docs.railway.com/cli/logs
11. Railway — Roll Back a Bad Deploy Fast: https://docs.railway.com/guides/roll-back-bad-deploy
12. Railway Central Station(커뮤니티, 공식 문서 아님) — Which header should I rely on for real client IP?: https://station.railway.com/questions/which-header-should-i-rely-on-for-real-c-d78a6f96
13. OpenAI Help — Troubleshooting API usage and spend limits: https://help.openai.com/en/articles/6614457-troubleshooting-api-usage-and-spend-limits

(2026-10-01 확인)
