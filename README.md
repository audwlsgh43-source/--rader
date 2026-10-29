# 공시레이더

평일 10분마다 DART 오픈API에서 오늘 나온 코스피·코스닥 공시를 받아 와서, 유형과 중요도를 붙여 보여주는 개인용 공시 대시보드예요.

```
index.html                  화면 (공시가 있는 최근 3일 날짜 탭, 데이터가 없으면 예시 데이터)
scripts/build_corps.py      종목 상세 → data/corps/종목코드.json (공시 기록, 관심종목 주가·재무)
scripts/weekly.py           주간 리포트 (금요일 18시 이후) → data/weekly/
scripts/notify.py           텔레그램 알림 (관심종목 새 공시, 평일 아침 브리핑)
watchlist.json              관심종목 목록 (사이트에서 ★로 저장)
scripts/fetch_market.py     시황 수집 → data/market.json (국내 지수·등락·순위, 해외·환율, AI 시황)
scripts/fetch_dart.py       DART 공시 수집 → data/days/YYYY-MM-DD.json (날짜별로 계속 누적)
data/events.json            이벤트 캘린더 (우선 직접 편집)
.github/workflows/update.yml  평일 07:00~20:59(KST) 10분마다 자동 실행
```

## 설정 (한 번만)

1. **GitHub 저장소 만들기**
   github.com에서 New repository를 눌러 저장소를 만들고(예: `gongsi-radar`), 이 폴더 안의 파일을 전부 올려요.
   웹에서 올릴 때 `.github` 폴더가 빠지기 쉬우니, 올린 뒤 저장소에 `.github/workflows/update.yml`이 있는지 꼭 확인하세요.

2. **DART 키 등록**
   저장소 화면에서 Settings → Secrets and variables → Actions → New repository secret을 눌러요.
   - Name: `DART_API_KEY`
   - Secret: 발급받은 40자리 키
   - (선택) `ANTHROPIC_API_KEY`도 등록하면 주요 공시에 AI 요약·핵심 수치가 붙고, 공시 속 일정이 캘린더에 자동으로 채워져요. 등록하지 않으면 요약 없이 동작해요.

3. **첫 수집 실행**
   Actions 탭 → "공시 수집" → Run workflow를 눌러요. 1분쯤 지나 `data/latest.json`이 생기면 성공이에요.

4. **사이트 열기**
   Settings → Pages → Branch를 `main` / `(root)`로 지정하고 Save를 눌러요.
   몇 분 뒤 `https://<아이디>.github.io/gongsi-radar/` 에서 사이트가 열려요.

## 참고

- 무료 플랜에서 GitHub Pages를 쓰려면 저장소가 **Public**이어야 하고, 그러면 사이트 주소를 아는 누구나 들어올 수 있어요. 키는 Secrets에만 저장되니 공개되지 않아요.
- GitHub 예약 실행은 몇 분씩 늦어질 수 있어요.
- 공시는 `data/days/날짜.json`에 날짜별로 계속 쌓이고 지워지지 않아요. 사이트에는 공시가 있는 최근 3일(주말 제외)이 탭으로 보이고, 이 3일치에 AI 분석이 붙어요(`SHOW_DAYS`로 조절).
- 공시 제목 키워드로 유형과 중요도를 나누는 기준은 `fetch_dart.py`의 `TYPE_RULES`, `HIGH`, `MID`에 있어요. 원하는 대로 고쳐서 쓰시면 돼요.

## 내 PC에서 테스트

```bash
pip install requests
export DART_API_KEY=발급받은키      # Windows PowerShell: $env:DART_API_KEY="발급받은키"
python scripts/fetch_dart.py
python -m http.server 8000          # 브라우저에서 http://localhost:8000
```

## 텔레그램 알림 설정 (선택)

1. 텔레그램에서 **@BotFather**를 찾아 `/newbot` → 봇 이름과 아이디(예: `gongsi_radar_bot`)를 정하면 **봇 토큰**을 줘요.
2. 방금 만든 봇과의 대화창을 열고 아무 메시지나 한 번 보내요.
3. GitHub Secrets에 `TELEGRAM_BOT_TOKEN`으로 봇 토큰을 등록하고 Actions에서 Run workflow를 한 번 실행해요.
4. Actions 기록의 '텔레그램 알림' 단계에 `TELEGRAM_CHAT_ID로 등록할 값: 123456789`가 찍혀요. 이 숫자를 `TELEGRAM_CHAT_ID`로 등록하면 끝이에요.

## 관심종목 기기 간 동기화

사이트 ↻ 설정에 넣는 GitHub 토큰에 **Contents: Read and write** 권한을 추가하면, ★를 누를 때 `watchlist.json`에 저장돼서 다른 기기와 텔레그램 알림에도 반영돼요.
기존 토큰은 GitHub → Settings → Developer settings → Fine-grained tokens에서 토큰을 눌러 권한만 추가하면 돼요(토큰 값은 그대로).
