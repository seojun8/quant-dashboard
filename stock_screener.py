"""
국내 주식 스크리너
- 지난 5년 주봉 기준 최고가·최저가 4등분 중 최저 구간 종목 선별
- 그 중 지난 3년 평균성장률(CAGR) 10% 이상 종목 필터
(FinanceDataReader 사용 - Python 3.14 / numpy 2.x 호환)
"""

from datetime import datetime, timedelta
import FinanceDataReader as fdr
import pandas as pd
import time


def _to_fdr_date(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def get_all_tickers() -> list[tuple[str, str]]:
    """KOSPI + KOSDAQ 전체 종목코드 및 이름 반환"""
    krx = fdr.StockListing("KRX")
    # KONEX 제외, Code/Name 컬럼
    krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])]
    return list(zip(krx["Code"].astype(str).str.zfill(6), krx["Name"].fillna("").astype(str)))


def _get_weekly_ohlcv(ticker: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """일봉을 가져와 주봉으로 리샘플링"""
    try:
        df = fdr.DataReader(ticker, start_date, end_date)
    except Exception:
        return None
    if df is None or len(df) < 50:
        return None
    # 단일 종목이면 컬럼이 그대로, 다중이면 MultiIndex 가능
    if "Close" not in df.columns:
        return None
    ohlc = df[["Open", "High", "Low", "Close"]].copy()
    ohlc.columns = ["Open", "High", "Low", "Close"]
    weekly = ohlc.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
    weekly = weekly.dropna()
    return weekly if len(weekly) >= 20 else None


def is_in_lowest_quartile(ticker: str, end_date: str) -> tuple[bool, dict] | None:
    """
    5년 주봉 기준 최고가·최저가 4등분 후, 현재가가 최저 1/4 구간에 있는지 확인
    """
    start_dt = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=365 * 5)
    start_date = start_dt.strftime("%Y%m%d")
    start_fdr = _to_fdr_date(start_date)
    end_fdr = _to_fdr_date(end_date)

    df = _get_weekly_ohlcv(ticker, start_fdr, end_fdr)
    if df is None or len(df) < 20:
        return None

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return None

    high_5y = df["High"].max()
    low_5y = df["Low"].min()
    current_price = df["Close"].iloc[-1]

    if high_5y <= low_5y or high_5y == 0:
        return None

    range_price = high_5y - low_5y
    q1_bound = low_5y + range_price / 4

    in_lowest = current_price <= q1_bound
    info = {
        "current_price": float(current_price),
        "high_5y": float(high_5y),
        "low_5y": float(low_5y),
        "q1_bound": float(q1_bound),
        "position_pct": (current_price - low_5y) / range_price * 100 if range_price > 0 else 0,
    }
    return (in_lowest, info)


def get_3year_cagr(ticker: str, end_date: str) -> float | None:
    """지난 3년 CAGR(연평균 복리 성장률) 계산"""
    start_dt = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=365 * 3)
    start_date = start_dt.strftime("%Y%m%d")
    start_fdr = _to_fdr_date(start_date)
    end_fdr = _to_fdr_date(end_date)

    try:
        df = fdr.DataReader(ticker, start_fdr, end_fdr)
    except Exception:
        return None

    if df is None or len(df) < 10:
        return None
    if "Close" not in df.columns:
        return None

    df = df.dropna(subset=["Close"])
    if df.empty:
        return None

    start_price = df["Close"].iloc[0]
    end_price = df["Close"].iloc[-1]

    if start_price <= 0:
        return None

    years = 3.0
    cagr = (end_price / start_price) ** (1 / years) - 1
    return float(cagr)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="국내 주식 스크리너: 5년 최저 1/4 구간 + 3년 CAGR 10% 이상")
    parser.add_argument("--limit", type=int, default=0, help="테스트용: 처리할 종목 수 제한 (0=전체)")
    parser.add_argument("--date", type=str, default="", help="기준일 YYYYMMDD (미입력=오늘)")
    args = parser.parse_args()

    end_date = args.date or datetime.now().strftime("%Y%m%d")
    min_cagr = 0.10

    print("=== 국내 주식 스크리너 ===\n")
    print("1. 전체 종목 로드 중...")
    tickers = get_all_tickers()
    if args.limit > 0:
        tickers = tickers[: args.limit]
        print(f"   (테스트 모드: 상위 {args.limit}종목만 처리)")
    print(f"   KOSPI + KOSDAQ 총 {len(tickers)} 종목\n")

    print("2. 5년 주봉 기준 최저 1/4 구간 종목 선별 중...")
    lowest_quartile = []
    for i, (ticker, name) in enumerate(tickers):
        if (i + 1) % 200 == 0:
            print(f"   진행: {i + 1}/{len(tickers)}")
        r = is_in_lowest_quartile(ticker, end_date)
        if r is not None and r[0]:
            lowest_quartile.append((ticker, name, r[1]))
        time.sleep(0.05)

    print(f"   최저 구간 종목: {len(lowest_quartile)}개\n")

    print("3. 3년 CAGR 10% 이상 종목 필터링 중...")
    results = []
    for ticker, name, info in lowest_quartile:
        cagr = get_3year_cagr(ticker, end_date)
        if cagr is not None and cagr >= min_cagr:
            results.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "current_price": info["current_price"],
                    "high_5y": info["high_5y"],
                    "low_5y": info["low_5y"],
                    "position_pct": round(info["position_pct"], 2),
                    "cagr_3y": round(cagr * 100, 2),
                }
            )
        time.sleep(0.05)

    print(f"\n=== 결과: {len(results)}개 종목 ===\n")
    if not results:
        print("조건에 맞는 종목이 없습니다.")
        return

    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values("position_pct", ascending=True)

    for _, row in df_result.iterrows():
        print(f"  {row['ticker']} | {row['name']}")
        print(f"    현재가: {row['current_price']:,.0f} | 5년 범위 내 위치: {row['position_pct']}% | 3년 CAGR: {row['cagr_3y']}%")
        print()

    out_path = "screener_result.csv"
    df_result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"결과가 {out_path}에 저장되었습니다.")


if __name__ == "__main__":
    main()
