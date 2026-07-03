"""
코스피/코스닥 종목의 '내일 오를 가능성' 재미용 순위를 계산해 docs/data.json 으로 저장한다.

주의: 이것은 투자 조언이 아니며, 단순 기술적 지표(이동평균/RSI/MACD/거래량/모멘텀)를
조합한 heuristic 점수일 뿐이다. 실제 예측 정확도를 보장하지 않는다.

데이터 출처: 네이버 금융 (finance.naver.com) 공개 페이지/API.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tomorrow-fun-app/1.0)"}

# 시가총액 상위 몇 페이지(1페이지=50종목)까지 각 시장에서 볼지. 너무 작은 종목은 제외해
# 데이터 품질과 실행 시간을 관리한다.
PAGES_PER_MARKET = 8  # KOSPI/KOSDAQ 각각 최대 400종목
HISTORY_DAYS = 70  # 지표 계산에 쓸 과거 캘린더 일수 (거래일 기준 약 45~48일)
MAX_WORKERS = 12
TOP_N_PER_MARKET = 25  # 시장별로 상위 N개씩 뽑아 합친다 (한쪽 쏠림 방지)
STALE_DAYS = 5  # 마지막 캔들이 이보다 오래됐으면 거래정지 등으로 보고 제외
MIN_SUCCESS_RATIO = 0.5  # 수집 성공률이 이 미만이면 데이터를 저장하지 않고 실패 처리

# ETF/ETN/스팩 등은 "종목"이 아니므로 순위에서 제외한다. 흔한 운용사 브랜드 접두어 목록.
NON_STOCK_KEYWORDS = (
    "TIGER", "KODEX", "ACE", "KBSTAR", "SOL", "ARIRANG", "HANARO", "KOSEF",
    "KINDEX", "TIMEFOLIO", "WOORI", "마이다스", "파워", "트러스톤", "네비게이터",
    "FOCUS", "히어로즈", "1Q", "코리아블록체인", "TIMEFOLIO", "ETN", "레버리지",
    "인버스", "선물", "합성", "스팩",
)


def is_tradeable_stock(code: str, name: str) -> bool:
    # 보통주는 종목코드가 0으로 끝난다. 우선주(…우, …우B)는 5/7/K 등으로 끝나므로 제외.
    if not code.endswith("0"):
        return False
    upper = name.upper()
    return not any(kw.upper() in upper for kw in NON_STOCK_KEYWORDS)


def http_get(url: str, timeout: int = 10, retries: int = 3) -> bytes:
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"GET failed: {url}") from last_err


def fetch_market_list(sosok: int, pages: int) -> list[dict]:
    """sosok=0 KOSPI, sosok=1 KOSDAQ. 네이버 시가총액 순위 페이지를 파싱."""
    out = []
    seen = set()
    for page in range(1, pages + 1):
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
        raw = http_get(url)
        text = raw.decode("euc-kr", errors="ignore")
        # 각 종목 행: <a href="/item/main.naver?code=005930">삼성전자</a> ... 뒤에 여러 <td> 값
        rows = re.findall(
            r'code=(\d{6})[^>]*>([^<]+)</a>.*?</tr>',
            text,
            re.S,
        )
        if not rows:
            break
        for code, name in rows:
            if code in seen:
                continue
            seen.add(code)
            name = name.strip()
            if not is_tradeable_stock(code, name):
                continue
            out.append({"code": code, "name": name, "market": "KOSPI" if sosok == 0 else "KOSDAQ"})
    return out


def fetch_history(code: str) -> list[dict]:
    end = datetime.now()
    start = end - timedelta(days=HISTORY_DAYS)
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        f"?symbol={code}&requestType=1&startTime={start:%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day"
    )
    raw = http_get(url, timeout=8).decode("utf-8", errors="ignore")
    rows = re.findall(
        r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)',
        raw,
    )
    candles = []
    for date, o, h, l, c, v in rows:
        candles.append(
            {"date": date, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}
        )
    return candles


def sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd_hist(closes: list[float]) -> tuple[float, float] | None:
    if len(closes) < 35:
        return None
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal = ema_series(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line[-len(signal):], signal)]
    if len(hist) < 2:
        return None
    return hist[-1], hist[-2]


@dataclass
class Scored:
    code: str
    name: str
    market: str
    price: float
    change_pct: float
    score: float
    volume_ratio: float | None = None
    reasons: list[str] = field(default_factory=list)


def score_stock(meta: dict, candles: list[dict]) -> Scored | None:
    if len(candles) < 26:
        return None
    # 거래정지·상장폐지 등으로 최근 시세가 없는 종목 제외
    last_date = datetime.strptime(candles[-1]["date"], "%Y%m%d")
    if (datetime.now() - last_date).days > STALE_DAYS:
        return None
    if candles[-1]["volume"] <= 0:
        return None
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    price = closes[-1]
    if price <= 0:
        return None
    change_pct = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 and closes[-2] > 0 else 0.0

    s5 = sma(closes, 5)
    s20 = sma(closes, 20)
    s5_prev = sma(closes[:-1], 5)
    s20_prev = sma(closes[:-1], 20)
    r = rsi(closes, 14)
    macd = macd_hist(closes)
    vol_avg20 = sma(vols[:-1], 20) if len(vols) >= 21 else None
    vol_ratio = (vols[-1] / vol_avg20) if vol_avg20 and vol_avg20 > 0 else None
    mom5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else None

    score = 0.0
    reasons = []

    if s5 and s20:
        if price > s5 > s20:
            score += 20
            reasons.append("단기·중기 이평선 위 정배열")
        elif price > s20:
            score += 8
            reasons.append("중기 이평선(20일) 위에서 거래")

    if s5 and s20 and s5_prev and s20_prev and s5_prev <= s20_prev and s5 > s20:
        score += 20
        reasons.append("골든크로스 발생 직후")

    if r is not None:
        if 50 <= r <= 68:
            score += 20
            reasons.append(f"RSI {r:.0f} (상승 모멘텀, 과열 아님)")
        elif 40 <= r < 50:
            score += 10
            reasons.append(f"RSI {r:.0f} (반등 초기 구간)")
        elif r > 75:
            score -= 10
            reasons.append(f"RSI {r:.0f} 과열 주의")

    if macd is not None:
        last_h, prev_h = macd
        if last_h > 0 and last_h > prev_h:
            score += 15
            reasons.append("MACD 히스토그램 상승 전환")
        elif last_h > 0:
            score += 6

    if vol_ratio is not None:
        if vol_ratio >= 2.5:
            score += 15
            reasons.append(f"거래량 급증 (평균 대비 {vol_ratio:.1f}배)")
        elif vol_ratio >= 1.5:
            score += 8
            reasons.append(f"거래량 증가 (평균 대비 {vol_ratio:.1f}배)")

    if mom5 is not None:
        if 0 < mom5 <= 15:
            score += 10
            reasons.append(f"최근 5일 +{mom5:.1f}% 완만한 상승")
        elif mom5 > 25:
            score -= 8
            reasons.append("단기 급등 이후 되돌림 주의")

    if change_pct >= 27:
        score -= 15
        reasons.append("상한가 근접 (추격매수 위험)")

    score = max(0.0, min(100.0, score))

    return Scored(
        code=meta["code"],
        name=meta["name"],
        market=meta["market"],
        price=price,
        change_pct=round(change_pct, 2),
        score=round(score, 1),
        volume_ratio=round(vol_ratio, 2) if vol_ratio is not None else None,
        reasons=reasons,
    )


def main():
    print("종목 리스트 수집 중...", file=sys.stderr)
    universe = fetch_market_list(0, PAGES_PER_MARKET) + fetch_market_list(1, PAGES_PER_MARKET)
    print(f"대상 종목 수: {len(universe)}", file=sys.stderr)

    results: list[Scored] = []
    errors = 0

    def worker(meta):
        candles = fetch_history(meta["code"])
        return score_stock(meta, candles)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(worker, m): m for m in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  진행: {done}/{len(universe)}", file=sys.stderr)
            try:
                r = fut.result()
            except Exception:
                errors += 1
                continue
            if r is not None:
                results.append(r)

    print(f"수집 완료. 성공 {len(results)}, 실패 {errors}", file=sys.stderr)

    # 수집 실패가 절반을 넘으면 (네이버 차단 등) 깨진 순위를 배포하지 않도록 실패 처리
    if universe and len(results) + errors > 0 and len(results) < len(universe) * MIN_SUCCESS_RATIO:
        print(f"수집 성공률이 너무 낮아 중단합니다 ({len(results)}/{len(universe)})", file=sys.stderr)
        sys.exit(1)

    # 동점이면 거래량 급증 정도로 순서를 정한다 (실행마다 순위가 흔들리지 않도록)
    sort_key = lambda x: (x.score, x.volume_ratio or 0.0, x.code)
    results.sort(key=sort_key, reverse=True)
    # 시장별 상위 N개씩 뽑아 합친다 — 한쪽 시장 쏠림 방지
    top = [s for s in results if s.market == "KOSPI"][:TOP_N_PER_MARKET] + \
          [s for s in results if s.market == "KOSDAQ"][:TOP_N_PER_MARKET]
    top.sort(key=sort_key, reverse=True)

    kst = timezone(timedelta(hours=9))
    payload = {
        "generated_at": datetime.now(kst).isoformat(),
        "universe_count": len(universe),
        "scored_count": len(results),
        "disclaimer": "재미로 보는 참고용 순위입니다. 투자 조언이 아니며 실제 등락을 보장하지 않습니다. 투자 판단과 책임은 본인에게 있습니다.",
        "items": [
            {
                "rank": i + 1,
                "code": s.code,
                "name": s.name,
                "market": s.market,
                "price": s.price,
                "change_pct": s.change_pct,
                "score": s.score,
                "volume_ratio": s.volume_ratio,
                "reasons": s.reasons,
            }
            for i, s in enumerate(top)
        ],
    }

    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("docs/data.json 저장 완료", file=sys.stderr)


if __name__ == "__main__":
    main()
