# 내일 오를까? 📈 (재미용)

코스피·코스닥 상장 종목을 기술적 지표(이동평균 정배열, 골든크로스, RSI, MACD, 거래량, 모멘텀)로
점수를 매겨 "내일 오를 가능성이 높아 보이는" 순위를 보여주는 **재미용** 웹앱입니다.

**투자 조언이 아닙니다.** 실제 주가 등락을 예측하거나 보장하지 않으며, 투자 판단과 책임은
본인에게 있습니다.

## 보기

GitHub Pages: `https://<username>.github.io/<repo>/`

휴대전화 브라우저에서 위 주소로 접속하면 됩니다. 홈 화면에 추가하면 앱처럼 쓸 수 있습니다.

## 구조

- `scripts/rank.py` — 네이버 금융 공개 시세를 수집해 지표를 계산하고 `docs/data.json` 생성
- `docs/index.html` — `data.json`을 읽어 순위를 보여주는 정적 페이지 (GitHub Pages가 서빙)
- `.github/workflows/update.yml` — 평일 개장 전/마감 후 자동으로 스크립트를 실행해 데이터 갱신

## 로컬 실행

```bash
python scripts/rank.py
python -m http.server -d docs 8080
# http://localhost:8080 접속
```

## 점수 산정 방식 (heuristic)

실시간 AI 예측이 아니라 아래와 같은 전통적 기술적 지표를 단순 가중합한 점수입니다(0~100):

- 이동평균(5일/20일) 정배열, 골든크로스 발생 여부
- RSI(14) — 과매수/과매도가 아닌 상승 모멘텀 구간 가점
- MACD 히스토그램 상승 전환
- 거래량이 20일 평균 대비 급증했는지
- 최근 5일 모멘텀(단, 이미 과도하게 급등했으면 감점)
- 당일 상한가 근접 시 감점(추격매수 위험)

## GitHub Pages 활성화 방법

1. 저장소 Settings → Pages
2. Source: `Deploy from a branch`
3. Branch: `main` / `docs` 폴더 선택 → Save
4. 몇 분 후 `https://<username>.github.io/<repo>/` 에서 접속 가능
5. https://github.com/totoro00joon-svg/super-duper-meme
