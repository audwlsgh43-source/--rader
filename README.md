# 공시레이더

평일 10분마다 DART 오픈API에서 오늘 나온 코스피·코스닥 공시를 받아 와서, 유형과 중요도를 붙여 보여주는 개인용 공시 대시보드예요.

```
index.html                  화면 (data/latest.json을 읽어서 표시, 파일이 없으면 예시 데이터)
scripts/fetch_dart.py       DART 공시 수집 → data/latest.json
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
- 날짜가 바뀌면 전날 데이터는 `data/archive/날짜.json`에 보관돼요.
- 공시 제목 키워드로 유형과 중요도를 나누는 기준은 `fetch_dart.py`의 `TYPE_RULES`, `HIGH`, `MID`에 있어요. 원하는 대로 고쳐서 쓰시면 돼요.

## 내 PC에서 테스트

```bash
pip install requests
export DART_API_KEY=발급받은키      # Windows PowerShell: $env:DART_API_KEY="발급받은키"
python scripts/fetch_dart.py
python -m http.server 8000          # 브라우저에서 http://localhost:8000
```
