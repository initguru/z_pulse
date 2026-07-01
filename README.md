# Z-Pulse

외부 봇(2oolkit-bot) 및 내부 봇 프로세스를 감시·재시작하고 Telegram으로 운영하는 독립 모니터링 봇.

---

## 🔎 특징

- **프로세스 감시 / 자동 재시작** — `TARGET_DIR` 하위 봇 디렉토리를 주기적으로 스캔하고 중단된 프로세스를 자동 재시작
- **Telegram 대시보드** — 인라인 버튼 UI로 상태 확인·제어 (재시작 / 종료 / 상세 보기)
- **ReplyKeyboard 퀵 메뉴** — 채팅 하단 고정 버튼 (대시보드 / 터미널 정렬 / 스크린샷 / 봇 업데이트)
- **경제지표 캘린더** (선택) — `ECONOMIC_CALENDAR_ENABLED=true` 시 `/economic` 명령어 활성화
- **메모리 경고 알림** — 메모리 임계치 초과 시 Telegram 알림 (`MEMORY_ALERT_ENABLED`)
- **Z-Flow 연동 레이어** (선택 주입) — `Z_FLOW_ENABLED=true` 시 sibling `z_flow`를 자동 감지해 `ZFlowBridge`를 통해 Z-Flow 슬롯 상태와 명령 표면을 노출

---

## 요구사항

- Python 3.11 이상 (`run_all.sh` 자동 확인) + pip
- 가상환경(uv venv)

---

## ⚙️ 설치 및 설정

> **중요:** 배포 압축 파일은 애플리케이션 파일이 `z_pulse` 디렉토리 안에 위치하도록 압축 해제하고, 설치와 실행 명령은 반드시 해당 `z_pulse` 디렉토리에서 실행합니다.

Z-Pulse는 `uv`로 Python 가상환경을 생성·관리합니다. 설치 전에 `uv` 사용 가능 여부를 확인합니다.

```bash
uv --version
```

`uv`가 없으면 아래 공식 명령으로 설치합니다.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
cd z_pulse
bash setup_bot.sh

# Windows
cd z_pulse
setup_bot.bat
```

스크립트가 대화형으로 아래 항목을 설정하고 `setting.env`를 생성합니다.

| 섹션 | 내용 |
|---|---|
| Section 1/4: 필수 설정 | 텔레그램 봇 토큰·채팅 ID, `TARGET_DIR`, `PROCESS_NAME` |
| Section 2/4: 기능 토글 | 경제지표 캘린더(업데이트 시간 포함), 메모리 경고 |
| Section 3/4: Z-Flow 연동 설정 (선택) | 같은 상위 디렉토리의 sibling `z_flow` 자동 감지 → `Z_FLOW_PATH`·`Z_FLOW_ENABLED` 자동 설정. Enter 시 단독 모드 |
| Section 4/4: Python 환경 | 가상환경(`uv venv`) 생성 및 `requirements.txt` 패키지 설치 |

> **참고:** Z-Flow 연동을 사용하려면 `z_pulse`와 `z_flow`를 같은 상위 디렉토리에 배치합니다. Section 3에서 활성화하면 setup이 sibling `z_flow`를 자동 감지해 `Z_FLOW_PATH`를 기록합니다. sibling `z_flow`가 없으면 연동은 비활성화됩니다. (기본값: Z-Pulse 단독 모드)

수동 설정은 `setting.env.example`을 `setting.env`로 복사한 뒤 편집합니다.

---

## ▶️ 실행

```bash
# macOS / Linux
cd z_pulse
./run_all.sh
```

```bat
:: Windows
cd z_pulse
run_all.bat
```

`run_all.sh` / `run_all.bat`는 다음을 자동 처리합니다:
- Python 3.11+ 가상환경 확인·생성 및 의존성 설치
- `setting.env` 로드
- 기존 Z-Pulse 프로세스 정리
- `Z_FLOW_ENABLED=true` 시 `ZFlowBridge` 기반 Z-Flow 연동 활성화
- Z-Pulse 실행 (`__main__.py`)

직접 디버깅이 필요한 경우에만 `python app.py`를 사용합니다.

---

## 💬 텔레그램 명령어

### 코어 명령어 (항상 활성)

| 명령어 | 설명 |
|---|---|
| `/start` | 봇 시작 및 환영 메시지 |
| `/status` | 대시보드 열기 |
| `/restart <디렉토리>` | 특정 프로세스 재시작 (DB유지) |
| `/restart_all` | 모든 프로세스 재시작 |
| `/restart_clean <디렉토리>` | 초기화 후 재시작 (DB삭제) |
| `/restart_running` | 실행 중인 봇만 재시작 |
| `/restart_main` | 메인 봇(Z-Pulse 자체) 전체 재시작 |
| `/kill <디렉토리>` | 특정 프로세스 종료 |
| `/screenshot` | 화면 스크린샷 전송 |
| `/log` | 봇 로그 보기 |
| `/arrange_windows` | 터미널 창 정렬 |
| `/update_bot` | 2oolkit-bot 바이너리 업데이트 |
| `/rename <old> <new>` | 디렉토리명 변경 |
| `/help` | 전체 명령어 도움말 |

### /update_bot 동작 방식

`/update_bot` 명령어(= ReplyKeyboard "봇 업데이트")는 감시 대상 프로세스(2oolkit-bot)의 바이너리를 업데이트합니다.

**바이너리 배치**: 최초 1회 `.update` 경로를 생성하고, 2oolkit-bot 바이너리를 미리 두어야 합니다.

동작 순서:
1. `.update/<2oolkit-bot*>` 파일 존재 확인 (없으면 경고 후 중단)
2. `.update/` 디렉토리에서 바이너리 실행 후 출력 파싱
   - `Already up to date.` → 최신 버전 안내
   - `Updated to ...` → 업데이트 확인
3. 업데이트 확인 시: 전체 봇 정지·정리 후 각 봇 디렉토리로 바이너리 배포

### 경제지표 활성 시 추가 명령어 (`ECONOMIC_CALENDAR_ENABLED=true`)

| 명령어 | 설명 |
|---|---|
| `/economic` | 경제지표 확인 |

---

## 📊 대시보드 (버튼 UI)

### ReplyKeyboard (채팅 하단 고정 버튼)

`/start` 또는 `/help` 실행 시 하단에 고정됩니다.

| 버튼 | 기능 |
|---|---|
| 대시보드 | `/status` 와 동일 |
| 터미널 정렬 | `/arrange_windows` 와 동일 |
| 스크린샷 | `/screenshot` 와 동일 |
| 봇 업데이트 | `/update_bot` 와 동일 |

### 메인 대시보드 (`/status`)

각 봇 디렉토리 행에 **상태 버튼** + **액션 버튼** 표시:

| 상태 아이콘 | 의미 |
|---|---|
| `🟢 <dir>` | 실행 중 — 클릭 시 상세 보기 |
| `🔴 <dir>` | 중단됨 — 클릭 시 상세 보기 |
| `⚪ <dir>` | 무시(ignore) 상태 — 클릭 시 상세 보기 |

액션 버튼 (상태에 따라 동적 표시):

| 버튼 | 조건 |
|---|---|
| `종료` | 실행 중인 봇 |
| `시작` | 중단된 봇 |

하단 제어 버튼:

| 버튼 | 기능 |
|---|---|
| `🔥 재시작(전체)` | 모든 프로세스 재시작 확인 |
| `▶️ 재시작(실행중)` | 실행 중인 프로세스만 재시작 |
| `📜 운영봇 로그(전체)` | Z-Pulse 전체 로그 출력 |
| `📄 운영봇 로그(100줄)` | Z-Pulse 최근 100줄 출력 |
| `🔄 새로고침` | 대시보드 갱신 |

### 상세 보기 (봇 디렉토리 선택 시)

Ignore 상태가 아닌 봇 선택 시 아래 버튼이 표시됩니다:

| 버튼 | 기능 |
|---|---|
| `⚙️ 설정 변경` | 개별 봇 설정 편집 메뉴 |
| `🔔 키워드 알림 설정` | 봇별 키워드 알림 메뉴 |
| `✨ 재시작(DB삭제)` | JSON/DB 초기화 후 재시작 |
| `🔄 재시작(DB유지)` | DB를 유지하고 재시작 |
| `📜 로그(전체)` | 해당 봇 전체 로그 출력 |
| `📄 로그(100줄)` | 해당 봇 최근 100줄 출력 |
| `🔙 돌아가기` | 메인 대시보드로 복귀 |

---

## 🔧 설정 (`setting.env` 주요 키)

| 키 | 기본값 | 반영 시점 | 설명 |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | 재시작 | 텔레그램 봇 토큰 (`@BotFather`에서 생성) |
| `TELEGRAM_CHAT_ID` | — | 재시작 | 허용할 텔레그램 채팅 ID |
| `TARGET_DIR` | `~/Documents/toolkit` | 재시작 | 모니터링 대상 봇 디렉토리 상위 경로 |
| `PROCESS_NAME` | `2oolkit-bot-macos-arm64` | 재시작 | 감시할 프로세스 이름 |
| `ECONOMIC_CALENDAR_ENABLED` | `true` | 재시작 | 경제지표 캘린더 활성화 |
| `ECONOMIC_UPDATE_HOUR` | `06` | 재시작 | 경제지표 일일 업데이트 시간 (0-23) |
| `Z_FLOW_PATH` | — | 재시작 | sibling `z_flow` 루트 경로 (setup_bot Section 3에서 자동 기록, 일반 운영 수동 설정 불필요) |
| `Z_FLOW_ENABLED` | `false` | 재시작 | Z-Flow 연동 활성화 |
| `MEMORY_ALERT_ENABLED` | `true` | 즉시 | 메모리 임계치 초과 시 알림 |

---

## 🔗 Z-Flow 통합 레이어 (선택)

`z_pulse`와 `z_flow`를 같은 상위 디렉토리에 둔 뒤 setup에서 `Z_FLOW_ENABLED=true`를 선택하면, setup이 sibling `z_flow`를 감지해 `Z_FLOW_PATH`를 기록합니다. Z-Flow 런타임이 존재하는 경우 Z-Pulse가 `ZFlowBridge` 단일 주입점을 통해 Z-Flow를 오케스트레이션합니다. Z-Pulse는 Z-Flow 내부 전략 키나 market-data 스크립트를 직접 소유하지 않습니다.

활성화 시 추가되는 기능:

**텔레그램 명령어 추가:**
| 명령어 | 설명 |
|---|---|
| `/pair_trading` | 페어 매매 현황 확인 |
| `/transfer <FROM> <TO> <금액>` | 자산 이전 (현재는 GRVT 거래소만 지원) |

**대시보드 액션 버튼 추가 (메인):**
| 버튼 | 조건 |
|---|---|
| `🔄 종료` | 실행 중인 Z-Flow 슬롯 봇 |
| `🔄 시작` | 중단된 Z-Flow 슬롯 봇 |
| `⚠️ 재개 필요` | Z-Flow 슬롯 — 비정상 상태 |
| `🔄 시그널 대기` | Z-Flow 슬롯 — 신호 대기 중 |

**대시보드 상세 보기 버튼 추가:**
| 버튼 | 기능 |
|---|---|
| `🤖 자동 배정 ON (...)` / `⏸️ 자동 배정 OFF` | 페어 로테이션 토글 |
| `⚡ 자동 배정 재개` | 비정상 종료 후 자동 배정 재개 (조건부) |

**시장 데이터 데몬:**
- Z-Pulse는 bridge와 런타임 DI를 통해 필요한 상태와 제어 표면만 사용합니다.

Z-Flow가 없는 환경에서는 관련 UI가 완전히 숨겨지며, Z-Pulse는 단독으로 동작합니다.

---

## 🗂️ 파일 구조

```
z_pulse/
├── app.py                      # 진입점
├── __main__.py                 # run_all.sh 실행 진입점
├── run_all.sh                  # 통합 실행 스크립트 (권장)
├── run_all.bat                 # 통합 실행 스크립트 (Windows)
├── stop_all.sh                 # 프로세스 일괄 종료 (macOS/Linux)
├── stop_all.bat                # 프로세스 일괄 종료 (Windows)
├── setup_bot.sh                # 설정 스크립트 (macOS/Linux)
├── setup_bot.bat               # 설정 스크립트 (Windows)
├── economic_scheduler.py       # 경제지표 스케줄러
├── constants.py                # 전역 상수
├── setting.env.example         # 환경변수 예시
├── platforms/                  # 플랫폼별 구현
├── bot/
│   ├── factory.py              # Application 생성 및 핸들러 등록
│   ├── keyboard_helper.py      # ReplyKeyboardMarkup 헬퍼
│   └── handlers/
│       ├── commands.py         # 슬래시 명령어 처리
│       ├── dashboard.py        # 대시보드 UI
│       ├── process_actions.py  # 버튼 콜백 (시작/종료/재시작)
│       ├── callback_router.py  # 콜백 라우팅
│       ├── settings.py         # 설정 메뉴
│       └── keywords.py         # 키워드 모니터링 메뉴
├── config/                     # 환경변수 로드 및 런타임 설정
├── features/                   # 프로세스 제어, 경제지표, 창 관리 등
├── integration/
│   ├── z_flow_bridge.py        # Z-Flow 단일 주입점
│   ├── telegram_extensions.py  # Z-Flow 활성 시 추가 명령어/콜백
│   ├── strategy_registry.py    # 전략 타입 해석
│   ├── uptime_restart_scheduler.py  # 업타임 기반 재시작 스케줄러
│   └── z_flow_runtime_di.py    # Z-Flow 런타임 DI
├── monitoring/                 # 프로세스 감시, 로그 키워드 모니터
├── scripts/                    # Z-Pulse 소유 보조 유틸리티
│   ├── stop_processes.py       # 기존 프로세스 정리
│   ├── daemon_watchdog.py      # 데몬 워치독
│   ├── log_wrapper.sh
│   └── 기타 Z-Pulse 운영 유틸리티
└── utils/                      # 공통 유틸리티
```
