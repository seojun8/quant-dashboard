# -*- coding: utf-8 -*-
"""
퀀트 투자 웹 대시보드 - 4단계 필터링 (수급→가격→적자→실적폭발)
streamlit run app.py
"""
# curl_cffi: libcurl-impersonate 로드 시점 (다른 패키지보다 먼저 import해야 invalid library 오류 방지)
try:
    from curl_cffi import requests as _curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _curl_requests = None
    _CURL_CFFI_AVAILABLE = False

_curl_cffi_disabled = False  # invalid library 등으로 실패 시 requests로 폴백

import json
import random
import re
import time
from datetime import datetime, timedelta
from io import StringIO
import gspread
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

# pykrx: 수급/시총 | FinanceDataReader: 장기 가격 | 네이버: 재무제표
try:
    from pykrx import stock as pykrx_stock
    PYKRX_AVAILABLE = True
except ImportError:
    PYKRX_AVAILABLE = False

import FinanceDataReader as fdr
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# SSL 인증서 검증 우회 시 InsecureRequestWarning 경고 숨김
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 모의 투자: Google Sheets 영구 저장
# - Streamlit Cloud: Secrets 에 SPREADSHEET_URL, GOOGLE_CREDENTIALS(JSON 문자열) [, WORKSHEET_NAME]
# - 로컬(레거시): .streamlit/secrets.toml 의 [mock_portfolio_gsheets] 테이블
_MOCK_PF_COLS = ["매수일자", "종목코드", "종목명", "매수단가", "매수수량"]
# 모바일 WebView에서 st.fragment 내부 Plotly가 비는 이슈 대응: fragment가 payload만 쌓고, 차트는 탭 본문에서 그림.
_MOCK_ETF_CHART_PAYLOAD_KEY = "_mock_etf_return_chart_payload"
_MOCK_GSHEETS_SECRET_SECTION = "mock_portfolio_gsheets"
_SECRET_SPREADSHEET_URL = "SPREADSHEET_URL"
_SECRET_GOOGLE_CREDENTIALS = "GOOGLE_CREDENTIALS"
_SECRET_WORKSHEET_NAME = "WORKSHEET_NAME"
_GSPREAD_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


def _streamlit_secrets_or_none():
    """secrets 파일이 없으면 None (접근 시 StreamlitSecretNotFoundError 방지)."""
    try:
        return st.secrets
    except StreamlitSecretNotFoundError:
        return None


def _mock_gsheets_configured() -> bool:
    """Cloud 플랫 키 또는 레거시 mock_portfolio_gsheets 블록이 있으면 True."""
    sec = _streamlit_secrets_or_none()
    if sec is None:
        return False
    try:
        url = str(sec.get(_SECRET_SPREADSHEET_URL, "") or "").strip()
        cred = sec.get(_SECRET_GOOGLE_CREDENTIALS)
        if url and cred is not None and str(cred).strip():
            return True
    except Exception:
        pass
    try:
        return _MOCK_GSHEETS_SECRET_SECTION in sec
    except Exception:
        return False


def _parse_spreadsheet_id_from_url(url_or_id: str) -> str | None:
    """스프레드시트 공유 URL에서 ID 추출. ID만 넣은 경우도 허용."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]+", s) and len(s) >= 10:
        return s
    return None


def _parse_gid_from_spreadsheet_url(url: str) -> int | None:
    """공유 URL의 gid= (탭별 시트 ID). 브라우저에서 연 탭과 동일한 시트를 연다."""
    s = (url or "").strip()
    if not s:
        return None
    m = re.search(r"gid=(\d+)", s)
    if m:
        return int(m.group(1))
    return None


def _parse_service_account_json(raw) -> dict | None:
    """GOOGLE_CREDENTIALS: 삼중따옴표/백틱/Code fence 제거 후 JSON 파싱."""
    if raw is None:
        return None
    t = str(raw).strip()
    for _ in range(3):
        if t.startswith("```"):
            t = t[3:]
            if t.lstrip().lower().startswith("json"):
                t = t[4:].lstrip()
            t = t.strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    if t.startswith("'''"):
        t = t[3:]
    if t.endswith("'''"):
        t = t[:-3]
    t = t.strip()
    if t.startswith('"""'):
        t = t[3:]
    if t.endswith('"""'):
        t = t[:-3]
    t = t.strip().strip("`").strip()
    try:
        d = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("type") == "service_account":
        return d
    if "private_key" in d and "client_email" in d:
        return d
    return None


def _empty_mock_portfolio_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_MOCK_PF_COLS)


def _toml_like_to_dict(obj) -> dict:
    """Streamlit secrets의 dict/AttrDict를 서비스 계정용 일반 dict로 재귀 변환."""

    def conv(x):
        if x is None:
            return None
        if isinstance(x, dict):
            return {str(k): conv(v) for k, v in x.items()}
        if hasattr(x, "keys") and callable(x.keys) and not isinstance(x, (str, bytes, int, float, bool)):
            try:
                return {str(k): conv(x[k]) for k in x.keys()}
            except Exception:
                return x
        return x

    r = conv(obj)
    return r if isinstance(r, dict) else {}


def _mock_gsheets_settings() -> tuple[str, str, dict, int | None] | None:
    """
    우선순위: (1) SPREADSHEET_URL + GOOGLE_CREDENTIALS (2) [mock_portfolio_gsheets] 테이블.
    반환: (spreadsheet_id, worksheet_name, service_account_dict, worksheet_gid_or_none)
    worksheet_gid: URL의 gid= 값이 있으면 해당 탭을 우선 연다(예전 데이터가 첫 탭일 때 필수).
    """
    sec = _streamlit_secrets_or_none()
    if sec is None:
        return None
    # --- Streamlit Cloud 등 플랫 Secrets ---
    try:
        url = str(sec.get(_SECRET_SPREADSHEET_URL, "") or "").strip()
        cred_raw = sec.get(_SECRET_GOOGLE_CREDENTIALS)
        if url and cred_raw is not None and str(cred_raw).strip():
            sid = _parse_spreadsheet_id_from_url(url)
            sa = _parse_service_account_json(cred_raw)
            wn = str(sec.get(_SECRET_WORKSHEET_NAME, "") or "").strip() or "mock_portfolio"
            gid = _parse_gid_from_spreadsheet_url(url)
            if sid and sa:
                return sid, wn, sa, gid
    except Exception:
        pass
    # --- 로컬 레거시 TOML 블록 ---
    try:
        if _MOCK_GSHEETS_SECRET_SECTION not in sec:
            return None
        raw = sec[_MOCK_GSHEETS_SECRET_SECTION]
        sid_raw = str(raw.get("spreadsheet_id", "")).strip()
        sid = _parse_spreadsheet_id_from_url(sid_raw) or sid_raw
        gid = _parse_gid_from_spreadsheet_url(sid_raw) if "/" in sid_raw else None
        wname = str(raw.get("worksheet_name", "mock_portfolio")).strip() or "mock_portfolio"
        sa: dict | None = None
        j = raw.get("service_account_json")
        if j is not None and str(j).strip():
            try:
                sa = json.loads(str(j).strip())
            except json.JSONDecodeError:
                sa = None
        if sa is None and raw.get("credentials") is not None:
            sa = _toml_like_to_dict(raw["credentials"])
        if not sid or not sa:
            return None
        if sa.get("type") != "service_account" and "private_key" not in sa:
            return None
        return sid, wname, sa, gid
    except Exception:
        return None


def _get_mock_portfolio_worksheet():
    """서비스 계정으로 스프레드시트를 연 뒤 워크시트 반환. 설정 오류 시 None."""
    parsed = _mock_gsheets_settings()
    if parsed is None:
        return None
    sid, wname, sa_info, sheet_gid = parsed
    try:
        gc = gspread.service_account_from_dict(sa_info, scopes=_GSPREAD_SCOPES)
        sh = gc.open_by_key(sid)
    except Exception:
        return None
    # URL에 gid가 있으면 그 탭만 쓴다. (gid 조회가 잠깐 실패했을 때 worksheet("mock_portfolio")로
    # 빈 자동생탭만 잡으면, 잠깐 데이터가 보였다가 비는 증상이 난다.)
    if sheet_gid is not None:
        for attempt in range(2):
            try:
                return sh.get_worksheet_by_id(sheet_gid)
            except Exception:
                if attempt == 0:
                    time.sleep(0.45)
        try:
            return sh.sheet1
        except Exception:
            return None
    try:
        return sh.worksheet(wname)
    except gspread.WorksheetNotFound:
        pass
    try:
        return sh.sheet1
    except Exception:
        pass
    try:
        ws = sh.add_worksheet(title=wname, rows=2000, cols=len(_MOCK_PF_COLS))
        ws.append_row(_MOCK_PF_COLS, value_input_option=gspread.utils.ValueInputOption.user_entered)
        return ws
    except Exception:
        return None


def _mock_portfolio_sheet_cache_key() -> str | None:
    """시트 읽기 캐시 키 (스프레드시트·탭 단위)."""
    p = _mock_gsheets_settings()
    if not p:
        return None
    sid, wname, _, gid = p
    return f"{sid}\x1f{wname}\x1f{gid if gid is not None else 'x'}"


@st.cache_data(ttl=12, max_entries=12, show_spinner=False)
def _cached_mock_portfolio_sheet_values(cache_key: str) -> list[list[str]]:
    """Google 시트 전체 셀 문자열 (캐시로 반복 조회·fragment 재실행 부담 감소)."""
    ws = _get_mock_portfolio_worksheet()
    if ws is None:
        raise RuntimeError("mock worksheet unavailable")
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return ws.get_all_values()
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("get_all_values failed")


def _invalidate_mock_portfolio_sheet_cache() -> None:
    try:
        _cached_mock_portfolio_sheet_values.clear()
    except Exception:
        pass


def _invalidate_mock_price_caches() -> None:
    """모바일·PC 간 시세가 다르게 보일 때 함께 비울 캐시."""
    try:
        _fetch_current_price.clear()
    except Exception:
        pass
    try:
        _fetch_close_series_range.clear()
    except Exception:
        pass


# ============== 날짜 유틸 ==============
def _to_ymd(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else d


def _get_end_date() -> str:
    return datetime.now().strftime("%Y%m%d")


# 특수 업종 제외용 키워드 (크롤링 에러 방지·정확한 퀀트 투자)
_EXCLUDE_NAME_KEYWORDS = (
    "스팩", "우선주", "우", "우B", "리츠", "신탁",
    "증권", "투자", "인베스트", "홀딩스", "지주",
)


def _is_special_sector(name: str) -> bool:
    """종목명에 특수 업종 키워드 포함 시 True (제외 대상)"""
    if not name or pd.isna(name):
        return False
    n = str(name).strip()
    return any(kw in n for kw in _EXCLUDE_NAME_KEYWORDS)


# ============== 1단계: 수급 필터링 (pykrx) ==============
@st.cache_data(ttl=3600)
def _fetch_supply_filter(_end_date: str) -> pd.DataFrame:
    """
    최근 1개월 '(외국인 누적 순매수 금액 / 시가총액) * 100' 상위 1000종목 (KOSPI·KOSDAQ 전체 대상)
    """
    if not PYKRX_AVAILABLE:
        return pd.DataFrame()

    start = (datetime.strptime(_end_date, "%Y%m%d") - timedelta(days=35)).strftime("%Y%m%d")
    rows = []

    for market in ("KOSPI", "KOSDAQ"):
        try:
            tickers = pykrx_stock.get_market_ticker_list(_end_date, market=market)
        except Exception:
            tickers = []
        for t in tickers:
            try:
                tv = pykrx_stock.get_market_trading_value_by_investor(start, _end_date, t)
                cap_df = pykrx_stock.get_market_cap(_end_date, _end_date, t)
                if tv is None or len(tv) == 0 or cap_df is None or len(cap_df) == 0:
                    continue
                # 외국인 순매수 금액 합산 (pykrx 구조: 컬럼 또는 인덱스)
                net_buy = 0
                for label in ("외국인합계", "외국인"):
                    if label in tv.columns:
                        net_buy = tv[label].sum()
                        break
                    if label in tv.index:
                        net_buy = tv.loc[label].sum()
                        break
                if net_buy == 0:
                    continue
                cap = float(cap_df["시가총액"].iloc[-1]) if "시가총액" in cap_df.columns else 0
                if cap <= 0:
                    continue
                ratio = (net_buy / cap) * 100
                name = pykrx_stock.get_market_ticker_name(t)
                rows.append({"ticker": t, "name": name, "ratio": ratio, "net_buy": net_buy, "cap": cap})
            except Exception:
                continue
            time.sleep(0.5)
        time.sleep(0.5)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.nlargest(1000, "ratio").reset_index(drop=True)
    return df


# ============== 2단계: 가격(낙폭과대) 필터링 ==============
@st.cache_data(ttl=3600)
def _check_single_price(ticker: str, start_fdr: str, end_fdr: str) -> dict | None:
    """단일 종목 가격 필터 (순차 처리에서 호출)"""
    try:
        df = fdr.DataReader(ticker, start_fdr, end_fdr)
    except Exception:
        return None
    if df is None or len(df) < 50 or "Close" not in df.columns:
        return None
    ohlc = df[["Open", "High", "Low", "Close"]].copy()
    weekly = ohlc.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(weekly) < 20:
        return None
    high_5y = weekly["High"].max()
    low_5y = weekly["Low"].min()
    current = weekly["Close"].iloc[-1]
    rng = high_5y - low_5y
    if rng <= 0 or high_5y <= 0:
        return None
    q1 = low_5y + rng / 3
    if current <= q1:
        pos_pct = (current - low_5y) / rng * 100
        return {"ticker": ticker, "current_price": current, "high_5y": high_5y, "low_5y": low_5y, "position_pct": round(pos_pct, 2)}
    return None


def _fetch_price_filter(tickers: list, _end_date: str, progress_callback=None) -> pd.DataFrame:
    """
    5년 주봉 기준, 현재가 <= 최저가 + (최고가-최저가)/3 인 종목만 (하위 33% 구간)
    Streamlit Cloud 등 저사양 환경을 위해 순차 처리 + 요청 간 딜레이.
    """
    start_dt = datetime.strptime(_end_date, "%Y%m%d") - timedelta(days=365 * 5)
    start_fdr = _to_ymd(start_dt.strftime("%Y%m%d"))
    end_fdr = _to_ymd(_end_date)
    result = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        if progress_callback:
            progress_callback(2, i + 1, total, f"2단계 가격 필터: {i + 1}/{total} 종목 처리 중...")
        row = _check_single_price(t, start_fdr, end_fdr)
        if row:
            result.append(row)
        time.sleep(0.5)

    return pd.DataFrame(result)


# ============== 3·4단계: 재무제표 (네이버 → FnGuide 대체) + 적자/실적 필터 ==============
# 완벽한 크롬 브라우저 헤더 위장 (SSL/TLS 지문·헤더 검사 우회)
CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://finance.naver.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Pragma": "no-cache",
}

# 429, 500, 502, 503, 504 시 최대 3회 백오프 재시도 (backoff_factor=1)
_retry_adapter = HTTPAdapter(
    max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
)
# requests fallback 세션 (curl_cffi 미설치 시)
_requests_session = requests.Session()
_requests_session.headers.update(CHROME_HEADERS)
_requests_session.mount("https://", _retry_adapter)
_requests_session.mount("http://", _retry_adapter)


def _fetch_finance_html(url: str, headers: dict) -> requests.Response | None:
    """
    재무제표 URL fetch. curl_cffi 사용 시 Chrome TLS 지문 위장으로 SSLEOFError 우회.
    invalid library 오류 시 requests로 자동 폴백.
    """
    global _curl_cffi_disabled
    for attempt in range(3):
        try:
            use_curl = _CURL_CFFI_AVAILABLE and not _curl_cffi_disabled
            if use_curl:
                try:
                    # Chrome TLS/JA3 지문 위장 → 네이버(에프앤가이드) 봇 차단 우회
                    with _curl_requests.Session() as s:
                        r = s.get(
                            url,
                            headers=headers,
                            impersonate="chrome120",
                            timeout=15,
                            verify=False,
                        )
                except Exception as curl_err:
                    err_str = str(curl_err).lower()
                    if "invalid library" in err_str or "tls connect error" in err_str or "curl: (35)" in err_str:
                        _curl_cffi_disabled = True
                        r = _requests_session.get(url, headers=headers, timeout=15, verify=False)
                    else:
                        raise curl_err
            else:
                r = _requests_session.get(url, headers=headers, timeout=15, verify=False)
            r.raise_for_status()
            time.sleep(0.5)
            return r
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, ConnectionError, OSError) as e:
            if attempt < 2:
                time.sleep(max(0.5, float(2**attempt)))
            else:
                raise e
    return None


def _safe_float(val) -> float | None:
    """문자열/NaN/결측치 → float 변환. 콤마, 공백, 괄호 제거."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().replace(",", "").replace(" ", "").replace("(", "-").replace(")", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return None
    try:
        f = float(s)
        return f if pd.notna(f) else None
    except (ValueError, TypeError):
        return None


def _clean_fin_df(df: pd.DataFrame) -> pd.DataFrame:
    """매출액·영업이익 컬럼 숫자 변환, NaN/결측/문자열 정제 후 모두 NaN인 행 제거"""
    if df is None or df.empty:
        return df
    df = df.copy()
    for col in ("매출액", "영업이익"):
        if col not in df.columns:
            continue
        df[col] = df[col].apply(lambda x: _safe_float(x))
    subset = [c for c in ("매출액", "영업이익") if c in df.columns]
    if subset:
        df = df.dropna(how="all", subset=subset)
    return df


def _flatten_column(c) -> str:
    """MultiIndex 컬럼을 단일 문자열로. 동적 컬럼 처리용."""
    if isinstance(c, tuple):
        return " ".join(str(x) for x in c if str(x) and str(x) != "nan")
    return str(c)


def _parse_year_col(col_str: str) -> tuple[int, int] | None:
    """컬럼명에서 연도·월 추출. (연도, 월) 또는 None. 정렬용."""
    s = _flatten_column(col_str)
    for pat in [r"(\d{4})[/.\s\-](\d{1,2})", r"(\d{4})", r"(\d{4})년"]:
        m = re.search(pat, s)
        if m:
            y = int(m.group(1))
            mo = int(m.group(2)) if len(m.groups()) >= 2 and m.group(2) else 12
            return (y, mo)
    return None


@st.cache_data(ttl=3600)
def _parse_fin_naver(code: str) -> pd.DataFrame | None:
    """
    네이버 파이낸스 cF1001 연간 재무제표 (매출액, 영업이익)
    - 딜레이·Session·Retry로 봇 차단 완화
    - match='영업이익': 표 정확 타겟팅
    """
    time.sleep(random.uniform(0.12, 0.35))
    url = (
        "https://companyinfo.stock.naver.com/v1/company/ajax/cF1001.aspx"
        f"?cmp_cd={code}&fin_typ=4&freq_typ=Y"
    )
    headers = {**CHROME_HEADERS, "Referer": f"https://finance.naver.com/item/main.naver?code={code}"}
    try:
        r = _fetch_finance_html(url, headers)
        if r is None:
            return None
        dfs = pd.read_html(StringIO(r.text), encoding="utf-8", match="영업이익")
        if not dfs:
            dfs = pd.read_html(StringIO(r.text), encoding="utf-8")
            dfs = [t for t in dfs if not t.empty and t.astype(str).apply(lambda row: row.str.contains("영업이익", na=False)).any().any()]
    except Exception as e:
        print(f"[{code}] 재무 데이터 수집 에러 (네이버): {e}", flush=True)
        return None
    if not dfs:
        return None
    df = dfs[0]
    if df.empty:
        return None
    first_cell = str(df.iloc[0, 0]) if len(df) > 0 else ""
    if "해당 데이터가 존재하지 않습니다" in first_cell:
        return None
    try:
        first_col = df.columns[0] if len(df.columns) > 0 else None
        if first_col is not None and "주요재무정보" not in str(first_col):
            df = df.rename(columns={df.columns[0]: "주요재무정보"})
        df = df.set_index("주요재무정보")
        all_cols = list(df.columns)
        year_cols = []
        for c in all_cols:
            key = _parse_year_col(c)
            if key and c not in ("연간", "분기"):
                year_cols.append((key, c))
        year_cols.sort(key=lambda x: x[0], reverse=True)
        if not year_cols:
            return None
        keep_cols = [x[1] for x in year_cols]
        df = df[keep_cols].copy()
        df.columns = [f"{y}-{m:02d}" for (y, m), _ in year_cols]
        df = df.T
        df.index = pd.to_datetime(df.index, format="%Y-%m", errors="coerce")
        df = df[df.index.notna()].copy()
        df = df.dropna(how="all", axis=0)
        rev_col = next((c for c in df.columns if "매출액" in str(c)), None)
        op_col = next((c for c in df.columns if re.search(r"영업이익\b", str(c))), None)
        if not rev_col or not op_col:
            return None
        df = df[[rev_col, op_col]].rename(columns={rev_col: "매출액", op_col: "영업이익"})
        df = _clean_fin_df(df)
        if len(df) >= 1 and "영업이익" in df.columns:
            return df
    except Exception as e:
        print(f"[{code}] 재무 데이터 수집 에러 (네이버 파싱): {e}", flush=True)
        return None
    return None


@st.cache_data(ttl=3600)
def _parse_fin_fnguide(code: str) -> pd.DataFrame | None:
    """
    FnGuide 재무제표 (네이버 실패 시 대체). gicode=A+종목코드
    - 딜레이·Session·Retry로 봇 차단 완화
    """
    time.sleep(random.uniform(0.12, 0.35))
    gicode = f"A{code}"
    url = f"https://comp.fnguide.com/SVO2/asp/SVD_Finance.asp?pGB=1&gicode={gicode}&ReportGB=D"
    try:
        r = _fetch_finance_html(url, CHROME_HEADERS)
        if r is None:
            return None
        dfs = pd.read_html(StringIO(r.text), encoding="utf-8", match="영업이익")
        if not dfs:
            dfs = pd.read_html(StringIO(r.text), encoding="utf-8")
            dfs = [t for t in dfs if not t.empty and t.astype(str).apply(lambda row: row.str.contains("영업이익", na=False)).any().any()]
    except Exception as e:
        print(f"[{code}] 재무 데이터 수집 에러 (FnGuide): {e}", flush=True)
        return None
    if not dfs:
        return None
    for tbl in dfs:
        if tbl.empty or len(tbl) < 5:
            continue
        first_col = tbl.iloc[:, 0].astype(str)
        rev_idx = first_col[first_col.str.contains("매출액", na=False)].index
        op_idx = first_col[first_col.str.contains("영업이익\\b", regex=True, na=False)].index
        if len(rev_idx) == 0 or len(op_idx) == 0:
            continue
        rev_row = tbl.iloc[rev_idx[0]]
        op_row = tbl.iloc[op_idx[0]]
        all_cols = [c for i, c in enumerate(tbl.columns) if i >= 1]
        year_cols = []
        for i, c in enumerate(all_cols):
            key = _parse_year_col(c)
            if key:
                col_idx = i + 1
                year_cols.append((key, c, col_idx))
        year_cols.sort(key=lambda x: x[0], reverse=True)
        if not year_cols:
            continue
        years_list = []
        data_list = []
        for key, orig_col, col_idx in year_cols[:10]:
            try:
                rev_val = rev_row.iloc[col_idx] if col_idx < len(rev_row) else rev_row.get(orig_col, None)
                op_val = op_row.iloc[col_idx] if col_idx < len(op_row) else op_row.get(orig_col, None)
                if pd.isna(rev_val) and orig_col in tbl.columns:
                    rev_val = rev_row.get(orig_col, rev_row.iloc[-1] if len(rev_row) > 0 else None)
                if pd.isna(op_val) and orig_col in tbl.columns:
                    op_val = op_row.get(orig_col, op_row.iloc[-1] if len(op_row) > 0 else None)
                rev_num = _safe_float(rev_val)
                op_num = _safe_float(op_val)
                if rev_num is not None or op_num is not None:
                    y, m = key
                    years_list.append(f"{y}-{m:02d}")
                    data_list.append({"매출액": rev_num, "영업이익": op_num})
            except (ValueError, TypeError, IndexError, KeyError):
                continue
        if len(data_list) >= 1:
            out = pd.DataFrame(data_list)
            out.index = pd.to_datetime(years_list, format="%Y-%m", errors="coerce")
            out = out[out.index.notna()].copy()
            out = _clean_fin_df(out) if not out.empty else out
            return out.dropna(how="all") if not out.empty else None
    return None


def _parse_finance(code: str) -> pd.DataFrame | None:
    """FnGuide 먼저 시도 → 실패 시 네이버 (FnGuide가 requests로 성공 가능성 높음)"""
    df = _parse_fin_fnguide(code)
    if df is not None and not df.empty and "매출액" in df.columns and "영업이익" in df.columns:
        return df
    return _parse_fin_naver(code)


def _cagr_3y(v0: float, v1: float) -> float | None:
    """3년 연평균 성장률. (최근연도/3년전)**(1/2) - 1"""
    if v0 is None or v1 is None or v0 <= 0:
        return None
    try:
        return (v1 / v0) ** 0.5 - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _process_single_finance(
    ticker: str,
    names: dict,
) -> tuple[str, bool, pd.DataFrame | None, dict | None, dict | None]:
    """
    강력한 성장 가치주 필터. Returns: (ticker, passed, fin_df, cagr_dict, error_info)
    조건A: 최근 3년 모두 영업이익 >= 0 (무적자)
    조건B: 매출액·영업이익 CAGR 둘 다 10% 이상
    """
    name = names.get(ticker, ticker)
    error_info = None
    try:
        df = _parse_finance(ticker)
        if df is None:
            error_info = {"종목코드": ticker, "종목명": name, "에러유형": "데이터없음", "에러내용": "재무 데이터 조회 실패 (NaN/빈 응답)", "영업이익": "NaN"}
            return (ticker, False, None, None, error_info)
        if "매출액" not in df.columns or "영업이익" not in df.columns:
            error_info = {"종목코드": ticker, "종목명": name, "에러유형": "KeyError", "에러내용": "'매출액' 또는 '영업이익' 컬럼 없음", "영업이익": "NaN"}
            return (ticker, False, None, None, error_info)
        recent3 = df.tail(3)
        if len(recent3) < 3:
            error_info = {"종목코드": ticker, "종목명": name, "에러유형": "데이터없음", "에러내용": f"최근 3개년 데이터 부족 (현재 {len(recent3)}개)", "영업이익": "NaN"}
            return (ticker, False, df, None, error_info)
        op_vals = [_safe_float(recent3["영업이익"].iloc[i]) for i in range(3)]
        rev_vals = [_safe_float(recent3["매출액"].iloc[i]) for i in range(3)]
        if any(v is None or v < 0 for v in op_vals):
            for i, v in enumerate(op_vals):
                if v is None:
                    error_info = {"종목코드": ticker, "종목명": name, "에러유형": "ValueError", "에러내용": f"{i+1}년차 영업이익 결측/변환실패", "영업이익": "NaN"}
                    return (ticker, False, df, None, error_info)
                if v < 0:
                    error_info = {"종목코드": ticker, "종목명": name, "에러유형": "조건미충족", "에러내용": f"조건A 위반: {i+1}년차 적자 (영업이익 {v:,.0f})", "영업이익": str(v)}
                    return (ticker, False, df, None, error_info)
        rev_cagr = _cagr_3y(rev_vals[0], rev_vals[-1])
        op_cagr = _cagr_3y(op_vals[0], op_vals[-1])
        if rev_cagr is None or op_cagr is None:
            error_info = {"종목코드": ticker, "종목명": name, "에러유형": "ValueError", "에러내용": "CAGR 계산 불가 (3년전 매출/영업이익 0 이하)", "영업이익": "NaN"}
            return (ticker, False, df, None, error_info)
        cagr_dict = {"매출CAGR(%)": round(rev_cagr * 100, 2), "영업CAGR(%)": round(op_cagr * 100, 2)}
        if rev_cagr < 0.10 or op_cagr < 0.10:
            error_info = {"종목코드": ticker, "종목명": name, "에러유형": "조건미충족", "에러내용": f"조건B 위반: 매출CAGR {rev_cagr*100:.1f}%, 영업CAGR {op_cagr*100:.1f}% (둘 다 10% 이상 필요)", "영업이익": "NaN"}
            return (ticker, False, df, None, error_info)
        return (ticker, True, df, cagr_dict, None)
    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)
        error_info = {"종목코드": ticker, "종목명": name, "에러유형": err_type, "에러내용": err_msg, "영업이익": "NaN"}
        return (ticker, False, None, None, error_info)


def _fetch_pbr_batch(tickers: list, end_date: str) -> dict[str, float | None]:
    """pykrx로 일괄 PBR 조회. 주말/공휴일이면 최근 거래일로 시도. Returns: {ticker: pbr or None}"""
    out = {}
    if not PYKRX_AVAILABLE or not tickers:
        return out
    dt = datetime.strptime(end_date, "%Y%m%d")
    for _ in range(8):  # 최대 7일 전까지 역순 탐색 (주말·공휴 대응)
        try:
            d_str = dt.strftime("%Y%m%d")
            df = pykrx_stock.get_market_fundamental_by_ticker(d_str, market="ALL")
            if df is not None and not df.empty and "PBR" in df.columns:
                for t in tickers:
                    t6 = str(t).zfill(6)
                    if t6 in df.index:
                        try:
                            v = float(df.loc[t6, "PBR"])
                            out[t] = v if pd.notna(v) else None
                        except (ValueError, TypeError):
                            out[t] = None
                    else:
                        out[t] = None
                time.sleep(0.5)
                return out  # 성공 시 즉시 반환
        except Exception:
            pass
        dt -= timedelta(days=1)
        time.sleep(0.5)
    return out


# ============== 5단계: 수급 및 거래량(모멘텀) 필터링 ==============
def _check_single_supply_volume(ticker: str, end_date: str) -> tuple[bool, bool, str, str]:
    """
    단일 종목: 외국인 순매수 + 거래량 급증 체크.
    Returns: (foreign_net_buy_ok, volume_surge_ok, foreign_str, volume_str)
    - foreign_net_buy_ok: 최근 20거래일 외국인 누적 순매수 > 0
    - volume_surge_ok: 최근 5거래일 평균 거래량 >= 1.5 × 그 이전 20거래일 평균
    """
    foreign_ok, volume_ok = False, False
    t6 = str(ticker).zfill(6)
    # ~40거래일 확보용 (약 60일)
    start_dt = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=60)
    start_ymd = start_dt.strftime("%Y%m%d")
    start_fdr = _to_ymd(start_ymd)
    end_fdr = _to_ymd(end_date)

    # 1) 외국인 순매수 (pykrx)
    if PYKRX_AVAILABLE:
        try:
            tv = pykrx_stock.get_market_trading_value_by_investor(start_ymd, end_date, t6)
            if tv is not None and len(tv) >= 1:
                net_buy = 0
                for label in ("외국인합계", "외국인"):
                    if label in tv.columns:
                        net_buy = float(tv[label].sum())
                        break
                    if hasattr(tv, "index") and label in tv.index:
                        net_buy = float(tv.loc[label].sum())
                        break
                foreign_ok = net_buy > 0
        except Exception:
            pass

    # 2) 거래량 급증: 최근 5일 평균 >= 1.5 × 이전 20일 평균
    try:
        # pykrx 우선, 없으면 fdr
        vol_series = None
        if PYKRX_AVAILABLE:
            try:
                ohlc = pykrx_stock.get_market_ohlcv_by_date(start_ymd, end_date, t6)
                if ohlc is not None and not ohlc.empty and "거래량" in ohlc.columns:
                    vol_series = ohlc["거래량"]
            except Exception:
                pass
        if vol_series is None or len(vol_series) < 25:
            df = fdr.DataReader(ticker, start_fdr, end_fdr)
            if df is not None and len(df) >= 25 and "Volume" in df.columns:
                vol_series = df["Volume"]
        if vol_series is not None and len(vol_series) >= 25:
            vol = vol_series.iloc[-25:]  # 최근 25거래일
            avg_last5 = vol.iloc[-5:].mean()
            avg_prev20 = vol.iloc[:-5].mean()
            if avg_prev20 and avg_prev20 > 0:
                volume_ok = avg_last5 >= 1.5 * avg_prev20
    except Exception:
        pass

    return foreign_ok, volume_ok, ("O" if foreign_ok else "X"), ("O" if volume_ok else "X")


def _filter_supply_volume(
    tickers: list, end_date: str, progress_callback=None
) -> tuple[list, dict[str, dict[str, str]]]:
    """
    5단계: 수급·거래량(모멘텀) — 외국인 순매수 OR 거래량 급증(1.5배) 중 하나라도 만족하면 통과.
    Returns: (passed_tickers, {ticker: {"외국인 매수": "O"|"X", "거래량 급증": "O"|"X"}})
    """
    passed = []
    info = {}
    if not tickers:
        return passed, info
    total = len(tickers)
    for i, t in enumerate(tickers):
        if progress_callback and total > 0:
            progress_callback(5, i + 1, total, f"5단계 수급·거래량 필터: {i + 1}/{total} 종목 처리 중...")
        try:
            foreign_ok, volume_ok, f_str, v_str = _check_single_supply_volume(t, end_date)
            info[t] = {"외국인 매수(O/X)": f_str, "거래량 급증(O/X)": v_str}
            if foreign_ok or volume_ok:
                passed.append(t)
        except Exception:
            info[t] = {"외국인 매수(O/X)": "X", "거래량 급증(O/X)": "X"}
        time.sleep(0.5)
    return passed, info


def _filter_finance(tickers: list, names: dict, end_date: str, progress_callback=None) -> tuple[list, dict, list]:
    """
    3·4단계: 강력한 성장 가치주 — 조건A(3년 무적자) + 조건B(매출·영업 CAGR 둘 다 10% 이상)
    Returns: (passed_tickers, {ticker: {영업이익, PBR, fin_df, cagr_dict}}, error_log)
    순차 처리(재무 크롤링 종목당 요청 간 딜레이).
    """
    passed = []
    fin_info = {}
    error_log = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        if progress_callback and total > 0:
            progress_callback(3, i + 1, total, f"3·4단계 재무 필터: {i + 1}/{total} 종목 처리 중...")
        t0, is_passed, fin_df, cagr_dict, err = _process_single_finance(t, names)
        if err is not None:
            error_log.append(err)
        op_val = fin_df["영업이익"].iloc[-1] if fin_df is not None and not fin_df.empty and "영업이익" in fin_df.columns else None
        fin_info[t0] = {"영업이익": op_val, "PBR": None, "fin_df": fin_df, "cagr_dict": cagr_dict or {}}
        if is_passed and cagr_dict is not None:
            passed.append(t0)
        time.sleep(0.5)
    pbr_map = _fetch_pbr_batch(passed, end_date)
    for t in passed:
        if t in pbr_map:
            fin_info[t]["PBR"] = pbr_map[t]
    return passed, fin_info, error_log


# ============== 차트 및 테이블 ==============
def _build_price_chart(ticker: str, name: str, end_date: str, current_price: float | None = None) -> go.Figure:
    start_dt = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=365 * 5)
    start_fdr = _to_ymd(start_dt.strftime("%Y%m%d"))
    end_fdr = _to_ymd(end_date)
    df = fdr.DataReader(ticker, start_fdr, end_fdr)
    time.sleep(0.5)
    if df is None or len(df) < 10 or "Close" not in df.columns:
        fig = go.Figure()
        fig.add_annotation(text="데이터 없음", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        return fig
    high_5y = df["High"].max()
    low_5y = df["Low"].min()
    fig = go.Figure(data=[go.Scatter(x=df.index, y=df["Close"], name="종가", line=dict(color="#1f77b4"))])
    fig.add_hline(y=high_5y, line_dash="dash", line_color="red", annotation_text="5년 최고가")
    fig.add_hline(y=low_5y, line_dash="dash", line_color="green", annotation_text="5년 최저가")
    if current_price is not None and current_price > 0:
        fig.add_hline(y=current_price, line_dash="dash", line_color="#FF8C00", annotation_text="현재가")
    fig.update_layout(
        title=f"{ticker} {name} - 최근 5년 주가",
        xaxis_title="날짜",
        yaxis_title="주가(원)",
        height=400,
        template="plotly_white",
    )
    return fig


def _build_fin_table(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    cols = ["매출액", "영업이익"]
    avail = [c for c in cols if c in df.columns]
    if not avail:
        return pd.DataFrame()
    out = df[avail].tail(3).copy()
    # 연도 컬럼: 인덱스(YYYY-MM) → YYYY.MM 형식으로 첫 번째 열에 표시
    if hasattr(out.index, "strftime"):
        out.insert(0, "연도", [x.strftime("%Y.%m") for x in out.index])
    else:
        out.insert(0, "연도", [str(x)[:7].replace("-", ".") if len(str(x)) >= 7 else str(x) for x in out.index])
    # 단위 표시: 네이버/FnGuide 주요재무정보는 억원
    out = out.rename(columns={"매출액": "매출액(억원)", "영업이익": "영업이익(억원)"})
    for c in out.columns:
        if c != "연도":
            out[c] = out[c].map(lambda x: f"{x:,.0f}" if pd.notna(x) and isinstance(x, (int, float)) else str(x))
    return out


# ============== 메인 분석 파이프라인 ==============
def run_full_analysis(end_date: str, progress_callback=None) -> tuple[pd.DataFrame, dict, dict, dict, dict]:
    """
    progress_callback(stage, current, total, msg) — stage 1~4, 각 25% 구간
    Returns: (result_df, price_info_dict, fin_cache_dict, cagr_cache_dict, stage_counts)
    """
    stage_counts = {"1단계_시총": 0, "2단계_가격": 0, "3·4단계_재무": 0, "5단계_수급거래량": 0, "최종": 0}
    cb = progress_callback

    # 1단계: 시총 구간 종목 선정
    if cb:
        cb(1, 0, 1, "1단계: 시총 구간 종목 선정 중...")
    supply_df = _fallback_tickers(
        end_date,
        min_mcap_billion=st.session_state.get("min_mcap_billion", 500),
        max_mcap_trillion=st.session_state.get("max_mcap_trillion", 2.0),
        max_stocks=st.session_state.get("max_stocks", 1000),
    )
    if supply_df.empty:
        return pd.DataFrame(), {}, {}, stage_counts, pd.DataFrame(), []
    tickers1 = supply_df["ticker"].tolist()
    names = dict(zip(supply_df["ticker"], supply_df["name"]))
    stage_counts["1단계_시총"] = len(tickers1)
    if cb:
        cb(1, 1, 1, f"1단계 완료: {len(tickers1)}종목 선정")

    # 2단계: 가격
    price_df = _fetch_price_filter(tickers1, end_date, progress_callback=cb)
    tickers2 = price_df["ticker"].tolist()
    price_info = price_df.set_index("ticker").to_dict("index")
    stage_counts["2단계_가격"] = len(tickers2)
    if cb:
        cb(2, 1, 1, f"2단계 완료: {len(tickers2)}개 통과 (탈락 {len(tickers1) - len(tickers2)})")

    if price_df.empty:
        return pd.DataFrame(), {}, {}, stage_counts, pd.DataFrame(), []

    # 3·4단계: 재무 (조건A 무적자 + 조건B 매출·영업 CAGR 10% 이상)
    passed, fin_info, finance_error_log = _filter_finance(tickers2, names, end_date, progress_callback=cb)
    stage_counts["3·4단계_재무"] = len(passed)
    data_na = sum(1 for e in finance_error_log if e.get("에러유형") == "데이터없음")
    if cb:
        cb(3, 1, 1, f"3·4단계 완료: {len(passed)}개 통과 (탈락 {len(tickers2)-len(passed)}, 그중 데이터없음 {data_na})")

    # 5단계: 수급·거래량 (외국인 매수 OR 거래량 급증)
    passed_5 = []
    supply_vol_info: dict = {}
    try:
        if passed:
            passed_5, supply_vol_info = _filter_supply_volume(passed, end_date, progress_callback=cb)
        stage_counts["5단계_수급거래량"] = len(passed_5)
        if cb and passed:
            cb(5, 1, 1, f"5단계 완료: {len(passed_5)}개 통과 (탈락 {len(passed)-len(passed_5)})")
    except Exception as e:
        # 5단계 API 에러 시 크래시 방지 — 3·4단계 통과 종목 모두 통과 처리, O/X는 X로
        stage_counts["5단계_수급거래량"] = len(passed)
        passed_5 = list(passed)
        supply_vol_info = {t: {"외국인 매수(O/X)": "X", "거래량 급증(O/X)": "X"} for t in passed}
        print(f"[5단계 예외] 수급·거래량 필터 건너뜀: {e}", flush=True)

    # 최종 결과 테이블 (5단계 통과 종목만) — 외국인 매수, 거래량 급증 컬럼 포함
    rows = []
    for t in passed_5:
        nm = names.get(t, t)
        pi = price_info.get(t, {})
        fin = fin_info.get(t, {})
        sv = supply_vol_info.get(t, {})
        op_val = fin.get("영업이익")
        pbr_val = fin.get("PBR")
        cagr = fin.get("cagr_dict") or {}
        rows.append({
            "종목코드": t,
            "종목명": nm,
            "현재가": int(pi.get("current_price", 0)),
            "위치(%)": pi.get("position_pct", 0),
            "매출CAGR(%)": cagr.get("매출CAGR(%)", 0),
            "영업CAGR(%)": cagr.get("영업CAGR(%)", 0),
            "PBR": round(pbr_val, 2) if pbr_val is not None else None,
            "외국인 매수(O/X)": sv.get("외국인 매수(O/X)", "X"),
            "거래량 급증(O/X)": sv.get("거래량 급증(O/X)", "X"),
            "5년최고가": int(pi.get("high_5y", 0)),
            "5년최저가": int(pi.get("low_5y", 0)),
            "네이버 재무제표": f"https://finance.naver.com/item/main.naver?code={t}",
        })
    result_df = pd.DataFrame(rows)
    if not result_df.empty:
        result_df = result_df.sort_values("위치(%)", ascending=True).reset_index(drop=True)
    stage_counts["최종"] = len(result_df)
    # 단계별 탈락 수 (표시용)
    stage_counts["2단계_탈락"] = stage_counts["1단계_시총"] - stage_counts["2단계_가격"]
    stage_counts["3·4단계_탈락"] = stage_counts["2단계_가격"] - stage_counts["3·4단계_재무"]
    stage_counts["5단계_탈락"] = stage_counts["3·4단계_재무"] - stage_counts["5단계_수급거래량"]
    data_unavail = sum(1 for e in finance_error_log if e.get("에러유형") == "데이터없음")
    stage_counts["데이터없음_탈락"] = data_unavail
    # 2단계 통과 종목 테이블 (최종 0개일 때 폴백 표시용)
    stage2_df = pd.DataFrame([
        {
            "종목코드": t,
            "종목명": names.get(t, t),
            "현재가": int(pi.get("current_price", 0)),
            "위치(%)": pi.get("position_pct", 0),
            "5년최고가": int(pi.get("high_5y", 0)),
            "5년최저가": int(pi.get("low_5y", 0)),
            "네이버 재무제표": f"https://finance.naver.com/item/main.naver?code={t}",
        }
        for t, pi in price_info.items()
    ])
    if not stage2_df.empty:
        stage2_df = stage2_df.sort_values("위치(%)", ascending=True).reset_index(drop=True)
    return result_df, price_info, fin_info, stage_counts, stage2_df, finance_error_log


def _pykrx_find_name_cap_columns(cap_df: pd.DataFrame) -> tuple[str | None, str | None]:
    """get_market_cap_by_ticker 결과의 종목명·시가총액 컬럼 추론 (pykrx/로케일 편차 대응)."""
    name_col = None
    cap_col = None
    for c in cap_df.columns:
        cs = str(c)
        lcs = cs.lower()
        if name_col is None and ("종목명" in cs or "종목" == cs or lcs in ("name", "stock name")):
            name_col = c
        if cap_col is None and ("시가총" in cs or "marcap" in lcs):
            cap_col = c
    if name_col is None and len(cap_df.columns) > 0:
        # 흔한 폴백: 첫 번째 문자열형이 이름
        for c in cap_df.columns:
            if cap_df[c].dtype == object or str(cap_df[c].dtype) == "string":
                name_col = c
                break
    return name_col, cap_col


def _pykrx_ticker_from_row(cap_df: pd.DataFrame, idx, row: pd.Series) -> str | None:
    """인덱스가 티커가 아닌 표에서 티커 열 탐색."""
    if not isinstance(cap_df.index, pd.RangeIndex):
        return str(idx).strip().zfill(6)
    for c in cap_df.columns:
        cs = str(c).lower()
        if "티커" in str(c) or "symbol" in cs or str(c) in ("Code", "code", "종목코드"):
            v = row.get(c)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                s = str(v).strip()
                if s.replace(".", "").replace("-", "").isdigit():
                    return s.split(".")[0].zfill(6)
    return None


def _fallback_tickers_pykrx(
    end_date: str,
    min_mcap_billion: float,
    max_mcap_trillion: float,
    max_stocks: int,
) -> pd.DataFrame | None:
    """
    pykrx get_market_cap_by_ticker 로 유니버스 생성 (Streamlit Cloud에서 FDR/KRX 웹 차단 시).
    """
    if not PYKRX_AVAILABLE:
        return None
    dt = datetime.strptime(end_date, "%Y%m%d")
    for _ in range(12):
        d_str = dt.strftime("%Y%m%d")
        all_rows: list[dict] = []
        for market, mlabel in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
            try:
                cap_df = pykrx_stock.get_market_cap_by_ticker(d_str, market=market)
            except Exception:
                cap_df = None
            if cap_df is None or cap_df.empty:
                continue
            name_col, cap_col = _pykrx_find_name_cap_columns(cap_df)
            if name_col is None or cap_col is None:
                continue
            for idx in cap_df.index:
                try:
                    row = cap_df.loc[idx]
                    t6 = _pykrx_ticker_from_row(cap_df, idx, row)
                    if not t6 or len(t6) != 6 or not t6.isdigit():
                        continue
                    nm = row[name_col]
                    marcap = float(row[cap_col])
                except (TypeError, ValueError, KeyError):
                    continue
                if pd.isna(nm) or marcap <= 0:
                    continue
                all_rows.append({"Code": t6, "Name": str(nm).strip(), "Marcap": marcap, "Market": mlabel})
        if all_rows:
            krx = pd.DataFrame(all_rows)
            krx = krx.dropna(subset=["Marcap"])
            krx = krx[~krx["Name"].fillna("").apply(_is_special_sector)]
            if min_mcap_billion < 0:
                filtered = krx.copy()
            else:
                min_val = min_mcap_billion * 1e8
                max_val = max_mcap_trillion * 1e12
                mask = (krx["Marcap"] >= min_val) & (krx["Marcap"] <= max_val)
                filtered = krx[mask]
            if max_stocks and max_stocks > 0:
                filtered = filtered.nlargest(max_stocks, "Marcap")
            return pd.DataFrame({
                "ticker": filtered["Code"].astype(str).str.zfill(6),
                "name": filtered["Name"].fillna(""),
            })
        dt -= timedelta(days=1)
        time.sleep(0.15)
    return None


def _fallback_tickers_naver(
    min_mcap_billion: float,
    max_mcap_trillion: float,
    max_stocks: int,
) -> pd.DataFrame | None:
    """
    네이버 금융 시가총액 순위 HTML (KRX 웹/pykrx가 Cloud에서 막힐 때).
    시가총액 숫자는 **억 원** 단위 → 내부 계산은 원으로 통일(×1e8).
    """
    max_pages = 45
    if max_stocks and max_stocks > 0:
        max_pages = min(50, max(10, (max_stocks * 2) // 45 + 8))
    all_rows: list[dict] = []
    for sosok, mlab in ((0, "KOSPI"), (1, "KOSDAQ")):
        for page in range(1, max_pages + 1):
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
            try:
                r = _requests_session.get(url, timeout=22, verify=False)
                r.raise_for_status()
            except Exception:
                break
            soup = BeautifulSoup(r.text, "lxml")
            table = soup.select_one("table.type_2")
            if not table:
                break
            tbody = table.find("tbody")
            if not tbody:
                break
            n_added = 0
            for tr in tbody.find_all("tr"):
                if tr.find("th"):
                    continue
                tds = tr.find_all("td")
                if len(tds) < 8:
                    continue
                a = tr.select_one("a.tltle") or tr.select_one("a[href*='main.naver?code=']")
                if not a:
                    continue
                m = re.search(r"code=(\d{6})", a.get("href", ""))
                if not m:
                    continue
                t6 = m.group(1)
                name = a.get_text(strip=True)
                link_i = next((i for i, td in enumerate(tds) if td.select_one("a[href*='code=']")), None)
                if link_i is None:
                    continue
                cap_i = link_i + 6
                if cap_i >= len(tds):
                    continue
                raw_cap = (
                    tds[cap_i]
                    .get_text(strip=True)
                    .replace(",", "")
                    .replace("\xa0", "")
                    .replace(" ", "")
                )
                if not raw_cap or raw_cap in ("N/A", "—", "-") or raw_cap.upper() == "N/A":
                    continue
                try:
                    cap_uk = float(raw_cap)
                except ValueError:
                    continue
                if cap_uk <= 0:
                    continue
                marcap = cap_uk * 1e8
                all_rows.append({"Code": t6, "Name": name, "Marcap": marcap, "Market": mlab})
                n_added += 1
            if n_added == 0:
                break
            time.sleep(0.22)
    if not all_rows:
        return None
    krx = pd.DataFrame(all_rows)
    krx = krx.sort_values("Marcap", ascending=False).drop_duplicates(subset=["Code"], keep="first")
    krx = krx[~krx["Name"].fillna("").apply(_is_special_sector)]
    if min_mcap_billion < 0:
        filtered = krx.copy()
    else:
        min_val = min_mcap_billion * 1e8
        max_val = max_mcap_trillion * 1e12
        mask = (krx["Marcap"] >= min_val) & (krx["Marcap"] <= max_val)
        filtered = krx[mask]
    if max_stocks and max_stocks > 0:
        filtered = filtered.nlargest(max_stocks, "Marcap")
    if filtered.empty:
        return None
    return pd.DataFrame({
        "ticker": filtered["Code"].astype(str).str.zfill(6),
        "name": filtered["Name"].fillna(""),
    })


def _fallback_tickers(
    _end_date: str,
    min_mcap_billion: float = 500,
    max_mcap_trillion: float = 2.0,
    max_stocks: int = 1000,
) -> pd.DataFrame:
    """
    KOSPI·KOSDAQ 중 시총 구간 필터 (중소형주 대상).
    min_mcap_billion=-1: 시총 무시, 전체 상장 종목.
    max_stocks=0: 제한 없음, 필터링된 전체 종목.
    """
    # pykrx 우선 — Streamlit Cloud 등에서 FinanceDataReader(KRX 웹)가 자주 실패함
    alt = _fallback_tickers_pykrx(_end_date, min_mcap_billion, max_mcap_trillion, max_stocks)
    if alt is not None and not alt.empty:
        return alt

    alt = _fallback_tickers_naver(min_mcap_billion, max_mcap_trillion, max_stocks)
    if alt is not None and not alt.empty:
        return alt

    krx = None
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            krx = fdr.StockListing("KRX")
            time.sleep(0.4)
            break
        except Exception as e:
            last_err = e
            if attempt < 1:
                time.sleep(1)
    if krx is None:
        raise RuntimeError(
            "증권(코스피·코스닥) **종목·시총** 목록을 가져오지 못했습니다.\n\n"
            "• 시도 순서: **pykrx** → **네이버 금융 시가총액 순위** → **FinanceDataReader**.\n"
            "• Streamlit Cloud는 `data.krx.co.kr` 차단이 흔합니다. **세 경로 모두 실패**하면 해당 서버에서 "
            "네이버·KRX 연결이 막힌 경우일 수 있어, 잠시 후 재시도하거나 로컬에서 실행해 보세요.\n"
            f"• (FDR 마지막 오류) {last_err}"
        ) from last_err
    krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])]
    krx = krx.dropna(subset=["Marcap"])
    # 특수 업종(스팩, 우선주, 리츠, 금융주 등) 제외
    krx = krx[~krx["Name"].fillna("").apply(_is_special_sector)]
    # 시총 필터: min_mcap_billion=-1 이면 시총 조건 무시
    if min_mcap_billion < 0:
        filtered = krx.copy()
    else:
        min_val = min_mcap_billion * 1e8
        max_val = max_mcap_trillion * 1e12
        mask = (krx["Marcap"] >= min_val) & (krx["Marcap"] <= max_val)
        filtered = krx[mask]
    # max_stocks=0 이면 제한 없음, 그 외 nlargest
    if max_stocks and max_stocks > 0:
        filtered = filtered.nlargest(max_stocks, "Marcap")
    return pd.DataFrame({
        "ticker": filtered["Code"].astype(str).str.zfill(6),
        "name": filtered["Name"].fillna(""),
    })


def _pykrx_etf_ticker_names(end_date: str) -> list[tuple[str, str]]:
    """pykrx ETF 코드·이름 (FinanceDataReader ETF/KR 실패 시 보조)."""
    if not PYKRX_AVAILABLE:
        return []
    fn = getattr(pykrx_stock, "get_etf_ticker_list", None)
    if not callable(fn):
        return []
    dt = datetime.strptime(end_date, "%Y%m%d")
    for _ in range(8):
        d = dt.strftime("%Y%m%d")
        tickers = None
        try:
            tickers = fn(d)
        except TypeError:
            try:
                tickers = fn()
            except Exception:
                tickers = None
        except Exception:
            tickers = None
        if tickers:
            out: list[tuple[str, str]] = []
            for t in tickers:
                t6 = str(t).strip().zfill(6)
                if not (len(t6) == 6 and t6.isdigit()):
                    continue
                try:
                    nm = pykrx_stock.get_market_ticker_name(t)
                except Exception:
                    nm = ""
                nm = str(nm).strip()
                if nm:
                    out.append((t6, nm))
            return out
        dt -= timedelta(days=1)
        time.sleep(0.1)
    return []


# ============== 모의 투자 포트폴리오 (CSV) ==============
@st.cache_data(ttl=3600)
def _get_krx_stock_options() -> list[str]:
    """
    KRX(KOSPI·KOSDAQ) 주식 + ETF 전체 종목 리스트 — "종목명 (종목코드)" 형태.
    FinanceDataReader 사용, 1시간 캐시로 속도 최적화.
    """
    result = []
    try:
        # 1) 주식 (KOSPI·KOSDAQ)
        krx = fdr.StockListing("KRX")
        krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])]
        krx = krx.dropna(subset=["Name", "Code"])
        krx = krx[~krx["Name"].fillna("").apply(_is_special_sector)]
        result.extend([f"{row['Name']} ({str(row['Code']).zfill(6)})" for _, row in krx.iterrows()])
        # 2) ETF (상장지수펀드) — EtfListing deprecated → StockListing("ETF/KR")
        try:
            etf = fdr.StockListing("ETF/KR")
            if etf is not None and not etf.empty:
                sym_col = "Symbol" if "Symbol" in etf.columns else etf.columns[0]
                name_col = "Name" if "Name" in etf.columns else etf.columns[1]
                for _, row in etf.iterrows():
                    code = str(row[sym_col]).strip().zfill(6)
                    name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
                    if code and code.isdigit() and len(code) == 6 and name:
                        result.append(f"{name} ({code})")
        except Exception:
            pass
    except Exception:
        pass
    if not result and PYKRX_AVAILABLE:
        try:
            alt = _fallback_tickers_pykrx(_get_end_date(), -1.0, 99999.0, 0)
            if alt is not None and not alt.empty:
                result = [f"{row['name']} ({row['ticker']})" for _, row in alt.iterrows()]
        except Exception:
            pass
    if PYKRX_AVAILABLE and result:
        have = set()
        for s in result:
            m = re.search(r"\(([0-9]{6})\)\s*$", s)
            if m:
                have.add(m.group(1))
        for t6, nm in _pykrx_etf_ticker_names(_get_end_date()):
            if t6 not in have and nm:
                result.append(f"{nm} ({t6})")
                have.add(t6)
    elif PYKRX_AVAILABLE and not result:
        for t6, nm in _pykrx_etf_ticker_names(_get_end_date()):
            if nm:
                result.append(f"{nm} ({t6})")
    return result


@st.cache_data(ttl=3600)
def _get_etf_codes() -> frozenset[str]:
    """ETF 종목코드 집합 (주식/ETF 구분용)."""
    codes = set()
    try:
        etf = fdr.StockListing("ETF/KR")
        if etf is not None and not etf.empty:
            sym_col = "Symbol" if "Symbol" in etf.columns else etf.columns[0]
            for _, row in etf.iterrows():
                c = str(row[sym_col]).strip().zfill(6)
                if c.isdigit() and len(c) == 6:
                    codes.add(c)
    except Exception:
        pass
    if not codes and PYKRX_AVAILABLE:
        for t6, _nm in _pykrx_etf_ticker_names(_get_end_date()):
            if len(t6) == 6 and t6.isdigit():
                codes.add(t6)
    return frozenset(codes)


def _parse_stock_selection(selection: str) -> tuple[str, str] | None:
    """'종목명 (종목코드)' 형식에서 (코드, 종목명) 추출. 6자리 코드로 반환."""
    if not selection or not isinstance(selection, str):
        return None
    m = re.search(r"\(([0-9]{6})\)\s*$", selection.strip())
    if m:
        code = m.group(1)
        name = selection[: m.start()].strip()
        if name:
            return (code, name)
    return None


def _normalize_mock_sheet_header(h: str) -> str:
    """시트 헤더 공백·별칭을 표준 컬럼명으로 맞춤 (예전 시트 호환)."""
    k = re.sub(r"\s+", "", str(h).strip().replace("\ufeff", ""))
    aliases = {
        "매수일자": "매수일자",
        "매수일": "매수일자",
        "일자": "매수일자",
        "종목코드": "종목코드",
        "코드": "종목코드",
        "티커": "종목코드",
        "종목명": "종목명",
        "종목": "종목명",
        "매수단가": "매수단가",
        "단가": "매수단가",
        "가격": "매수단가",
        "매수수량": "매수수량",
        "수량": "매수수량",
        "주식수": "매수수량",
        "보유수량": "매수수량",
    }
    return aliases.get(k, str(h).strip())


def _coerce_mock_sheet_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """시트/API는 숫자도 '9,000' 같은 문자로 올 수 있음 → 계산용 숫자로 변환."""
    d = df.copy()
    if "매수단가" in d.columns:
        d["매수단가"] = pd.to_numeric(
            d["매수단가"].astype(str).str.replace(",", "", regex=False).str.replace(r"[^\d.\-]", "", regex=True),
            errors="coerce",
        ).fillna(0)
    if "매수수량" in d.columns:
        d["매수수량"] = pd.to_numeric(
            d["매수수량"].astype(str).str.replace(",", "", regex=False).str.replace(r"[^\d\-]", "", regex=True),
            errors="coerce",
        ).fillna(0).astype(int)
    return d


def _mock_portfolio_stable_hash(df: pd.DataFrame) -> str:
    """시트 원본 vs data_editor 결과 비교용 (종목코드 6자리·단가 소수 자리 등 규칙 통일). 해시 불일치 시 매번 저장→rerun 되며 '계속 실행 중'이 될 수 있음."""
    if df is None or df.empty:
        return ""
    cols = ["매수일자", "종목코드", "종목명", "매수단가", "매수수량"]
    if not all(c in df.columns for c in cols):
        return ""
    d = df[cols].copy()
    d["매수일자"] = d["매수일자"].astype(str).str.strip()
    d["종목명"] = d["종목명"].astype(str).str.strip()
    sc = d["종목코드"].astype(str).str.strip().str.replace(r"\.0$", "", regex=False)
    nc = pd.to_numeric(sc, errors="coerce")
    d["종목코드"] = nc.fillna(0).astype(int).astype(str).str.zfill(6)
    d["매수단가"] = pd.to_numeric(d["매수단가"], errors="coerce").fillna(0).round(2)
    d["매수수량"] = pd.to_numeric(d["매수수량"], errors="coerce").fillna(0).astype(int)
    d = d.sort_values(["종목코드", "매수일자"], kind="mergesort").reset_index(drop=True)
    return d.to_csv(index=False, float_format="%.2f")


def _load_mock_portfolio() -> pd.DataFrame:
    """Google Sheets에서 모의 매수 내역 읽기. Secrets/공유 미설정 시 빈 DataFrame."""
    rows: list[list[str]] | None = None
    ck = _mock_portfolio_sheet_cache_key()
    if ck:
        try:
            rows = _cached_mock_portfolio_sheet_values(ck)
        except Exception:
            rows = None
    if rows is None:
        ws = _get_mock_portfolio_worksheet()
        if ws is None:
            return _empty_mock_portfolio_df()
        for attempt in range(3):
            try:
                rows = ws.get_all_values()
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
    if rows is None:
        return _empty_mock_portfolio_df()
    if len(rows) < 1:
        return _empty_mock_portfolio_df()
    header_raw = [str(x) for x in rows[0]]
    header = [_normalize_mock_sheet_header(x) for x in header_raw]
    data = rows[1:] if len(rows) >= 2 else []
    if not header:
        return _empty_mock_portfolio_df()
    if "종목코드" not in header:
        return _empty_mock_portfolio_df()
    if not data:
        return _empty_mock_portfolio_df()
    try:
        df = pd.DataFrame(data, columns=header)
    except Exception:
        return _empty_mock_portfolio_df()
    # 동일 표준명 중복 컬럼 방지(병합 등)
    df = df.loc[:, ~df.columns.duplicated()].copy()
    for c in _MOCK_PF_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[_MOCK_PF_COLS].copy()
    df = _coerce_mock_sheet_numeric_columns(df)
    return df


def _save_mock_portfolio(df: pd.DataFrame, *, allow_clear_sheet: bool = False) -> None:
    """모의 매수 내역을 Google Sheets에 덮어쓰기 저장."""
    ws = _get_mock_portfolio_worksheet()
    if ws is None:
        raise RuntimeError(
            "Google Sheets 연동 실패: Secrets에 SPREADSHEET_URL·GOOGLE_CREDENTIALS를 확인하거나, "
            "레거시 mock_portfolio_gsheets 블록·서비스 계정 공유(편집자)를 확인하세요."
        )

    if df is None or df.empty:
        if not allow_clear_sheet:
            raise RuntimeError(
                "저장할 유효한 행이 없습니다. 시트 전체를 비우지 않습니다. "
                "(표에서 값이 잠깐 깨지면 이 저장이 막혀야 합니다.)"
            )
        body = [_MOCK_PF_COLS]
    else:
        d = df.copy()
        for c in _MOCK_PF_COLS:
            if c not in d.columns:
                d[c] = None
        d = d[_MOCK_PF_COLS]
        body = [_MOCK_PF_COLS]
        for _, r in d.iterrows():
            pa = str(r["매수단가"]).replace(",", "").strip()
            qt = str(r["매수수량"]).replace(",", "").strip()
            body.append([
                str(r["매수일자"]).strip(),
                str(r["종목코드"]).strip().zfill(6),
                str(r["종목명"]).strip(),
                float(pd.to_numeric(pa, errors="coerce") or 0),
                int(pd.to_numeric(qt, errors="coerce") or 0),
            ])
    try:
        ws.clear()
        ws.update(body, "A1", value_input_option=gspread.utils.ValueInputOption.user_entered)
    except Exception as e:
        raise RuntimeError(f"Google Sheets 저장 실패: {e}") from e
    _invalidate_mock_portfolio_sheet_cache()
    _invalidate_mock_price_caches()


@st.cache_data(ttl=120)
def _fetch_current_price(ticker: str, _end_date: str = "") -> float | None:
    """종목의 최근 종가(현재가) 조회. pykrx 우선, 없으면 FDR. 2분 캐시."""
    t6 = str(ticker).zfill(6)
    end_date = _end_date or _get_end_date()
    end_fdr = _to_ymd(end_date)
    # pykrx
    if PYKRX_AVAILABLE:
        try:
            ohlc = pykrx_stock.get_market_ohlcv_by_date(
                (datetime.now() - timedelta(days=14)).strftime("%Y%m%d"), end_date, t6
            )
            if ohlc is not None and not ohlc.empty and "종가" in ohlc.columns:
                return float(ohlc["종가"].iloc[-1])
        except Exception:
            pass
    # FDR
    try:
        start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        df = fdr.DataReader(ticker, start, end_fdr)
        if df is not None and len(df) >= 1 and "Close" in df.columns:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return None


@st.cache_data(ttl=1800)
def _fetch_close_series_range(ticker: str, start_ymd: str, end_ymd: str) -> pd.Series:
    """
    start_ymd~end_ymd(YYYYMMDD) 거래일별 종가 시리즈.
    pykrx 우선, 실패 시 FinanceDataReader. 인덱스는 날짜(시각 제거) 기준 정렬.
    """
    t6 = str(ticker).zfill(6)
    start_ymd = str(start_ymd).zfill(8)[:8]
    end_ymd = str(end_ymd).zfill(8)[:8]
    if len(start_ymd) < 8 or len(end_ymd) < 8:
        return pd.Series(dtype=float)
    s_fdr = _to_ymd(start_ymd)
    e_fdr = _to_ymd(end_ymd)
    ser = pd.Series(dtype=float)
    if PYKRX_AVAILABLE:
        try:
            ohlc = pykrx_stock.get_market_ohlcv_by_date(start_ymd, end_ymd, t6)
            if ohlc is not None and not ohlc.empty and "종가" in ohlc.columns:
                raw_i = ohlc.index
                idx = pd.to_datetime(raw_i, errors="coerce")
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                idx = idx.normalize()
                ser = pd.Series(ohlc["종가"].astype(float).values, index=idx)
                ser = ser[~pd.isna(ser.index)].sort_index()
        except Exception:
            ser = pd.Series(dtype=float)
    if ser.empty:
        try:
            df = fdr.DataReader(t6, s_fdr, e_fdr)
            if df is not None and not df.empty and "Close" in df.columns:
                ix = pd.to_datetime(df.index, errors="coerce")
                if getattr(ix, "tz", None) is not None:
                    ix = ix.tz_localize(None)
                ix = ix.normalize()
                ser = pd.Series(df["Close"].astype(float).values, index=ix)
                ser = ser[~pd.isna(ser.index)].sort_index()
        except Exception:
            pass
    ser = ser[~ser.index.duplicated(keep="last")]
    return ser.astype(float)


def _build_etf_daily_return_pct_series(code6: str, lots: pd.DataFrame, end_ymd: str) -> pd.Series | None:
    """
    최초 매수일(유효 분 중 가장 이른 날) 이후 각 거래일 종가 기준 수익률(%).
    분할매수: 해당일까지 매수 완료된 분의 (수량×종가) 합 / 누적 투입금 - 1.
    """
    if lots is None or lots.empty:
        return None
    parsed: list[tuple[pd.Timestamp, float, int]] = []
    for _, row in lots.iterrows():
        dt = pd.to_datetime(row.get("매수일자"), errors="coerce")
        if pd.isna(dt):
            continue
        if getattr(dt, "tz", None) is not None:
            dt = dt.tz_localize(None)
        dt = dt.normalize()
        px = float(pd.to_numeric(row.get("매수단가"), errors="coerce") or 0)
        q = int(pd.to_numeric(row.get("매수수량"), errors="coerce") or 0)
        if px <= 0 or q <= 0:
            continue
        parsed.append((dt, px, q))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    start_ymd = parsed[0][0].strftime("%Y%m%d")
    closes = _fetch_close_series_range(code6, start_ymd, end_ymd)
    if closes is None or closes.empty:
        return None
    lots_sorted = sorted(parsed, key=lambda x: x[0])
    li = 0
    cum_shares = 0
    cum_cost = 0.0
    out_idx: list[pd.Timestamp] = []
    out_vals: list[float] = []
    for ts in closes.index.sort_values():
        while li < len(lots_sorted) and lots_sorted[li][0] <= ts:
            _d, px, q = lots_sorted[li]
            cum_shares += q
            cum_cost += px * q
            li += 1
        if cum_shares <= 0 or cum_cost <= 0:
            continue
        try:
            c = float(closes.loc[ts])
        except Exception:
            continue
        if pd.isna(c) or c <= 0:
            continue
        ret = (cum_shares * c / cum_cost - 1.0) * 100.0
        out_idx.append(ts)
        out_vals.append(ret)
    if not out_vals:
        return None
    return pd.Series(out_vals, index=pd.DatetimeIndex(out_idx), name="수익률(%)")


def _render_etf_return_chart_outside_fragment() -> None:
    """
    ETF 일별 수익률 Plotly 차트. st.fragment 밖에서 호출해야 모바일 브라우저에서도 안정적으로 표시되는 경우가 많습니다.
    (fragment 내부에 두면 일부 WebView에서 iframe 높이 0·미마운트 현상이 납니다.)
    """
    payload = st.session_state.get(_MOCK_ETF_CHART_PAYLOAD_KEY)
    if not payload or not isinstance(payload, dict):
        return
    seen_order = payload.get("seen_order") or []
    _sum_df = payload.get("sum_df")
    _end_ymd_ch = str(payload.get("end_ymd") or _get_end_date())
    if _sum_df is None or not isinstance(_sum_df, pd.DataFrame) or _sum_df.empty or not seen_order:
        return
    fig_etf_ret = go.Figure()
    chart_fail: list[str] = []
    for (code6, name) in seen_order:
        g_lots = _sum_df[_sum_df["_code6"] == code6][["매수일자", "매수단가", "매수수량"]]
        ser_ret = _build_etf_daily_return_pct_series(code6, g_lots, _end_ymd_ch)
        time.sleep(0.2)
        if ser_ret is None or ser_ret.empty:
            chart_fail.append(f"{code6} ({name})" if (name or "").strip() else code6)
            continue
        label = f"{code6} {name}".strip()
        if len(label) > 82:
            label = label[:79] + "…"
        fig_etf_ret.add_trace(go.Scatter(x=ser_ret.index, y=ser_ret.values, mode="lines", name=label))
    if len(fig_etf_ret.data) > 0:
        fig_etf_ret.update_layout(
            title="ETF 일별 수익률 (종가, 분할매수 반영)",
            xaxis_title="날짜",
            yaxis_title="수익률 (%)",
            height=420,
            template="plotly_white",
            legend=dict(orientation="h", yanchor="top", y=-0.28, xanchor="center", x=0.5),
            margin=dict(b=100),
        )
        fig_etf_ret.add_hline(y=0, line_dash="dot", line_color="#888", line_width=1)
        st.subheader("📉 ETF 일별 수익률 (종가)")
        st.caption(
            "가장 이른 매수일 이후, **그날까지 매수가 완료된 분**의 누적 투입금 대비 "
            "당일 종가로 산출한 보유 평가금의 수익률(%)입니다."
        )
        st.plotly_chart(
            fig_etf_ret,
            use_container_width=True,
            theme=None,
            config={"scrollZoom": False, "displayModeBar": True},
        )
    if chart_fail:
        tail = " …" if len(chart_fail) > 5 else ""
        st.warning(
            "일부 ETF는 시세 이력을 불러오지 못해 차트에서 제외했습니다: "
            + ", ".join(chart_fail[:5])
            + tail
        )


def _build_portfolio_with_prices(raw_df: pd.DataFrame) -> pd.DataFrame:
    """CSV 내역 + 실시간 현재가 → 평가금액, 수익금, 수익률(%), 매매 시그널 컬럼 추가."""
    if raw_df is None or raw_df.empty:
        return raw_df
    cols = ["매수일자", "종목코드", "종목명", "매수단가", "매수수량"]
    if not all(c in raw_df.columns for c in cols):
        return raw_df
    out = raw_df[cols].copy()
    out = _coerce_mock_sheet_numeric_columns(out)
    end_date = _get_end_date()
    unique_codes = out["종목코드"].astype(str).str.zfill(6).unique().tolist()
    price_map: dict[str, float] = {}
    # ThreadPoolExecutor + st.cache_data 조합은 워커 스레드에 ScriptRunContext가 없어
    # "missing ScriptRunContext!" 경고·캐시 비정상을 유발하므로, 시세는 메인 스레드에서 순차 조회.
    for c in unique_codes:
        try:
            p = _fetch_current_price(c, _end_date=end_date)
            price_map[c] = float(p) if p is not None else 0.0
        except Exception:
            price_map[c] = 0.0
    out["현재가"] = out["종목코드"].astype(str).str.zfill(6).map(lambda c: price_map.get(c, 0))
    out["평가금액"] = out["현재가"] * out["매수수량"]
    out["수익금"] = (out["현재가"] - out["매수단가"]) * out["매수수량"]
    cost = out["매수단가"] * out["매수수량"]
    out["수익률(%)"] = 0.0
    mask = cost != 0
    out.loc[mask, "수익률(%)"] = (out.loc[mask, "수익금"] / cost.loc[mask] * 100).round(2)
    # 매매 시그널: -15% 이하 손절, +20% 이상 익절, 그 사이 관망
    def _signal(pct: float) -> str:
        if pd.isna(pct):
            return "[🟡 관망 (보유)]"
        p = float(pct)
        if p <= -15:
            return "[🔴 전량 손절]"
        if p >= 20:
            return "[🟢 1차 익절]"
        return "[🟡 관망 (보유)]"
    out["매매 시그널"] = out["수익률(%)"].apply(_signal)
    return out


def _render_mock_portfolio_inner() -> None:
    """모의 보유 표·가격·손익 지표. st.fragment(run_every=…)와 같이 쓰면 주기 갱신 시 이 함수만 재실행되어 전체 앱(1탭 분석 등)은 다시 돌지 않습니다."""
    raw_inner = _load_mock_portfolio()
    if raw_inner.empty:
        st.warning("보유 종목이 없습니다. 새로고침하거나 모의 매수를 추가해 주세요.")
        return
    pf = _build_portfolio_with_prices(raw_inner)
    if pf.empty:
        st.warning("평가 데이터를 불러올 수 없습니다.")
        return
    st.session_state.pop(_MOCK_ETF_CHART_PAYLOAD_KEY, None)
    etf_codes = _get_etf_codes()
    pf["_code6"] = pf["종목코드"].astype(str).str.zfill(6)
    pf_stocks = pf[~pf["_code6"].isin(etf_codes)].drop(columns=["_code6"]).reset_index(drop=True)
    pf_etfs = pf[pf["_code6"].isin(etf_codes)].drop(columns=["_code6"]).reset_index(drop=True)
    if not pf_stocks.empty:
        pf_stocks["매수단가"] = pd.to_numeric(pf_stocks["매수단가"], errors="coerce").fillna(0).astype("float64")
    if not pf_etfs.empty:
        pf_etfs["매수단가"] = pd.to_numeric(pf_etfs["매수단가"], errors="coerce").fillna(0).astype("float64")

    save_cols = ["매수일자", "종목코드", "종목명", "매수단가", "매수수량"]
    # 종목코드·종목명은 동적 행 추가(num_rows=dynamic) 시 비어 있으므로 반드시 편집 가능해야 함
    col_config = {
        "매수일자": st.column_config.TextColumn("매수일자", disabled=False, help="YYYY-MM-DD 형식으로 수정 가능"),
        "종목코드": st.column_config.TextColumn("종목코드", disabled=False, help="6자리 숫자. 행 추가 시 필수"),
        "종목명": st.column_config.TextColumn("종목명", disabled=False, help="행 추가 시 필수"),
        "매수단가": st.column_config.NumberColumn(
            "매수단가",
            format="%,.2f",
            min_value=0.0,
            step=0.01,
            help="소수 둘째 자리까지 (모바일은 step·실수 타입 필요)",
        ),
        "매수수량": st.column_config.NumberColumn("매수수량", format="%d", step=1, help="직접 수정 가능"),
        "현재가": st.column_config.NumberColumn("현재가", format="%,d", disabled=True),
        "평가금액": st.column_config.NumberColumn("평가금액", format="%,d", disabled=True),
        "수익금": st.column_config.NumberColumn("수익금", format="%,d", disabled=True),
        "수익률(%)": st.column_config.NumberColumn("수익률(%)", format="%+.2f%%", disabled=True, help="📊 손절(-15%)·익절(+20%) 기준"),
        "매매 시그널": st.column_config.TextColumn("매매 시그널", disabled=True, help="🔴손절 🟢익절 🟡관망"),
    }
    st.caption("※ **수익률** -15% 이하: 🔴 전량 손절 추천 | +20% 이상: 🟢 1차 익절 추천 | 그 사이: 🟡 관망 (보유)")

    def _parse_price(val) -> float:
        if pd.isna(val):
            return 0.0
        s = str(val).strip().replace(",", "")
        try:
            return float(s) if s else 0.0
        except ValueError:
            return 0.0

    def _valid_save_df(df: pd.DataFrame) -> pd.DataFrame:
        d = df[save_cols].copy()
        sc = d["종목코드"].astype(str).str.strip().str.replace(r"\.0$", "", regex=False)
        nc = pd.to_numeric(sc, errors="coerce")
        d["종목코드"] = nc.fillna(0).astype(int).astype(str).str.zfill(6)
        d["매수단가"] = d["매수단가"].apply(_parse_price)
        valid = (
            d["종목코드"].str.match(r"^\d{6}$", na=False)
            & d["매수일자"].notna() & (d["매수일자"].astype(str).str.strip() != "")
            & (d["매수단가"] > 0)
            & (pd.to_numeric(d["매수수량"], errors="coerce").fillna(0) > 0)
            & d["종목명"].notna() & (d["종목명"].astype(str).str.strip() != "")
        )
        d["매수단가"] = d["매수단가"].round(2)
        return d[valid]

    edited_stocks = None
    edited_etfs = None
    if not pf_stocks.empty:
        st.subheader("📈 주식")
        edited_stocks = st.data_editor(
            pf_stocks,
            width="stretch",
            height=min(300, 80 + len(pf_stocks) * 38),
            hide_index=True,
            num_rows="dynamic",
            column_config=col_config,
            key="mock_stocks_editor",
        )

    st.subheader("📊 ETF")
    # 빈 표를 columns=만으로 만들면 매수단가가 object/int로 잡혀 data_editor가 소수 입력을 막는 경우가 있음(모바일 특히)
    etf_editor_df = pf_etfs.copy() if not pf_etfs.empty else pf.iloc[0:0].copy()
    _n_etf = len(etf_editor_df)
    edited_etfs = st.data_editor(
        etf_editor_df,
        width="stretch",
        height=min(420, 80 + max(_n_etf, 1) * 38),
        hide_index=True,
        num_rows="dynamic",
        column_config=col_config,
        key="mock_etfs_editor",
    )
    st.caption("**종목별 종합 수익률** (동일 ETF 분할매수 건 통합) · 행 하단 **+** 로 ETF 매수 건을 추가한 뒤 코드·종목명·일자·단가·수량을 입력하세요.")
    _etf_for_summary = edited_etfs if edited_etfs is not None and not edited_etfs.empty else pf_etfs
    if _etf_for_summary is not None and not _etf_for_summary.empty:
        _sum_df = _etf_for_summary.copy()
        _sum_df["_code6"] = _sum_df["종목코드"].astype(str).str.zfill(6)
        _sum_df = _sum_df[_sum_df["_code6"].isin(etf_codes)]
        if not _sum_df.empty:
            seen_codes: list[str] = []
            seen_order: list[tuple[str, str]] = []
            for _, row in _sum_df.iterrows():
                c6 = row["_code6"]
                if c6 not in seen_codes:
                    seen_codes.append(c6)
                    seen_order.append((c6, str(row.get("종목명", "") or "")))
            etf_summary = []
            for (code6, name) in seen_order:
                g = _sum_df[_sum_df["_code6"] == code6]
                cost = (g["매수단가"].astype(float) * g["매수수량"].astype(float)).sum()
                eval_amt = g["평가금액"].astype(float).sum()
                diff = int(round(eval_amt - cost))
                ret = (eval_amt - cost) / cost * 100 if cost > 0 else 0
                etf_summary.append({"종목코드": code6, "종목명": name, "총매수금액": int(round(cost)), "총평가금액": int(round(eval_amt)), "차액": diff, "종합 수익률(%)": round(ret, 2)})
            if etf_summary:
                summary_df = pd.DataFrame(etf_summary)
                tot_cost = summary_df["총매수금액"].sum()
                tot_eval = summary_df["총평가금액"].sum()
                tot_ret = (tot_eval - tot_cost) / tot_cost * 100 if tot_cost > 0 else 0
                tot_diff = int(tot_eval - tot_cost)
                rows = []
                for r in etf_summary:
                    구분 = f"{r['종목코드']} {r['종목명']}"
                    rows.append(f"| {구분} | {r['총매수금액']:,} | {r['총평가금액']:,} | {r['차액']:+,} | {r['종합 수익률(%)']:+.2f}% |")
                rows.append(f"| **ETF 전체** | **{int(tot_cost):,}** | **{int(tot_eval):,}** | **{tot_diff:+,}** | **{tot_ret:+.2f}%** |")
                tbl = "구분 | 총매수금액 | 총평가금액 | 차액 | 종합 수익률(%)\n" + "--- | --- | --- | --- | ---\n" + "\n".join(rows)
                st.markdown(tbl)
                stash = _sum_df[["매수일자", "매수단가", "매수수량", "_code6", "종목명"]].copy()
                st.session_state[_MOCK_ETF_CHART_PAYLOAD_KEY] = {
                    "seen_order": list(seen_order),
                    "sum_df": stash,
                    "end_ymd": _get_end_date(),
                }

    parts = []
    if edited_stocks is not None and not edited_stocks.empty:
        parts.append(_valid_save_df(edited_stocks))
    if edited_etfs is not None and not edited_etfs.empty:
        parts.append(_valid_save_df(edited_etfs))
    save_df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if (edited_stocks is not None and not edited_stocks.empty) or (edited_etfs is not None and not edited_etfs.empty):
        if save_df.empty and not raw_inner.empty:
            st.warning(
                "편집 내용에서 **저장 가능한 행이 없습니다** (날짜·코드·종목명·단가·수량 형식 확인). "
                "**Google 시트는 그대로 두었습니다.**"
            )
    pending_hash = _mock_portfolio_stable_hash(save_df) if not save_df.empty else ""
    sheet_hash = _mock_portfolio_stable_hash(raw_inner)
    has_pending_sheet_save = bool(pending_hash and pending_hash != sheet_hash)
    if has_pending_sheet_save:
        st.warning(
            "표를 수정했습니다. **Google 시트에 반영**하려면 아래 **저장** 버튼을 누르세요. "
            "(자동 저장은 하지 않습니다 — 실행 중 루프를 막기 위함입니다.)"
        )
    if st.button(
        "💾 Google 시트에 저장",
        type="primary",
        key="mock_push_sheet_button",
        disabled=not has_pending_sheet_save,
        help="표의 매수일·단가·수량 변경만 시트에 반영됩니다.",
    ):
        try:
            _save_mock_portfolio(save_df)
        except RuntimeError as e:
            st.error(str(e))
        else:
            st.success("시트에 저장했습니다.")
            st.rerun()

    def _code6_series(ser: pd.Series) -> pd.Series:
        s = ser.astype(str).str.strip().str.replace(r"\.0$", "", regex=False)
        n = pd.to_numeric(s, errors="coerce")
        return n.fillna(0).astype(int).astype(str).str.zfill(6)

    if edited_stocks is not None and not edited_stocks.empty:
        _s = edited_stocks.copy()
        _s["_c6"] = _code6_series(_s["종목코드"])
        pf_stocks_m = _s[~_s["_c6"].isin(etf_codes)].drop(columns=["_c6"], errors="ignore")
    else:
        pf_stocks_m = pf_stocks

    if edited_etfs is not None and not edited_etfs.empty:
        _e = edited_etfs.copy()
        _e["_c6"] = _code6_series(_e["종목코드"])
        pf_etfs_m = _e[_e["_c6"].isin(etf_codes)].drop(columns=["_c6"], errors="ignore")
    else:
        pf_etfs_m = pf_etfs

    # 합계는 화면 표(편집 반영)와 동일: Σ(매수단가×수량), 평가는 Σ(현재가×수량). 엑셀 SUMPRODUCT와 같음.
    _m_parts = [d for d in (pf_stocks_m, pf_etfs_m) if d is not None and not d.empty]
    if _m_parts:
        _mall = pd.concat(_m_parts, ignore_index=True)
        _q = pd.to_numeric(_mall["매수수량"], errors="coerce").fillna(0)
        _px = pd.to_numeric(_mall["매수단가"], errors="coerce").fillna(0)
        total_qty = int(_q.sum())
        total_cost = float((_px * _q).sum())
        if "현재가" in _mall.columns:
            _cp = pd.to_numeric(_mall["현재가"], errors="coerce").fillna(0)
            total_eval = float((_cp * _q).sum())
        else:
            total_eval = float(pd.to_numeric(_mall["평가금액"], errors="coerce").fillna(0).sum())
    else:
        total_qty = int(pd.to_numeric(pf["매수수량"], errors="coerce").fillna(0).sum())
        _q0 = pd.to_numeric(pf["매수수량"], errors="coerce").fillna(0)
        _p0 = pd.to_numeric(pf["매수단가"], errors="coerce").fillna(0)
        total_cost = float((_p0 * _q0).sum())
        total_eval = float(pf["평가금액"].sum())
    total_return_pct = (total_eval - total_cost) / total_cost * 100 if total_cost > 0 else 0

    def _calc_return(df_sub: pd.DataFrame) -> tuple[float, float, float]:
        if df_sub.empty:
            return 0.0, 0.0, 0.0
        q = pd.to_numeric(df_sub["매수수량"], errors="coerce").fillna(0)
        px = pd.to_numeric(df_sub["매수단가"], errors="coerce").fillna(0)
        cost = float((px * q).sum())
        if "현재가" in df_sub.columns:
            cp = pd.to_numeric(df_sub["현재가"], errors="coerce").fillna(0)
            eval_amt = float((cp * q).sum())
        else:
            eval_amt = float(pd.to_numeric(df_sub["평가금액"], errors="coerce").fillna(0).sum())
        ret = (eval_amt - cost) / cost * 100 if cost > 0 else 0
        return cost, eval_amt, ret

    stock_cost, stock_eval, stock_return = _calc_return(pf_stocks_m)
    etf_cost, etf_eval, etf_return = _calc_return(pf_etfs_m)

    st.divider()
    st.caption(
        "총 매수금액 = 각 행 **매수단가×매수수량**의 합입니다. 엑셀·구글시트에서는 `SUMPRODUCT(단가열, 수량열)`과 동일합니다."
    )
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("합계 수량", f"{total_qty:,}주")
    with c2:
        st.metric("총 매수금액", f"{total_cost:,.2f}원")
    with c3:
        st.metric("총 평가금액", f"{total_eval:,.2f}원")
    with c4:
        st.metric("전체 수익률", f"{total_return_pct:+.2f}%")
    with c5:
        st.metric("주식 수익률", f"{stock_return:+.2f}%" if not pf_stocks_m.empty else "—")
    with c6:
        st.metric("ETF 수익률", f"{etf_return:+.2f}%" if not pf_etfs_m.empty else "—")


# ============== Streamlit UI ==============
st.set_page_config(page_title="퀀트 투자 대시보드", layout="wide")

# 기본 글자 크기 (적당한 가독성)
st.markdown("""
<style>
    /* 캡션, 진행상황 메시지 */
    .stCaptionContainer p, [data-testid="stCaption"], .stCaptionContainer, .stCaption { font-size: 1.1rem !important; }
    /* 본문 */
    .stMarkdown p, [data-testid="stMarkdown"] p { font-size: 1rem !important; }
    /* 진행바 텍스트 */
    .stProgress > div > div, div[data-testid="stStatusWidget"] label, [data-testid="stProgress"] span { font-size: 1rem !important; }
    /* 셀렉트박스, 버튼, 텍스트입력, 숫자입력 */
    .stSelectbox label, .stButton label, .stTextInput label, .stNumberInput label { font-size: 1rem !important; }
    .stSelectbox input, .stTextInput input, .stNumberInput input { font-size: 0.95rem !important; }
    /* 테이블 (dataframe, data_editor) */
    .stDataFrame, .stDataFrame td, .stDataFrame th, .stDataFrame span,
    div[data-testid="stDataFrame"] *, div[data-testid="stDataFrameResizable"] *,
    [data-testid="element-container"] div[role="grid"] *,
    .ag-root, .ag-cell, .ag-header-cell { font-size: 1rem !important; }
    /* 사이드바 */
    [data-testid="stSidebar"] .stMarkdown, [data-testid="stSidebar"] .stCaptionContainer,
    [data-testid="stSidebar"] label { font-size: 1rem !important; }
    /* subheader (h2, h3) */
    .stMarkdown h2 { font-size: 1.25rem !important; }
    .stMarkdown h3 { font-size: 1.1rem !important; }
    /* 메트릭(합계 수량 등) */
    [data-testid="stMetricValue"], [data-testid="stMetricLabel"] { font-size: 1.1rem !important; }
    /* 진행바 길이 */
    [data-testid="stProgress"] { max-width: 320px !important; width: 100%; }
    /* 탭 메뉴 */
    .stTabs [data-baseweb="tab-list"] button,
    .stTabs [data-baseweb="tab"] p,
    .stTabs [data-baseweb="tab-list"] [data-testid="stMarkdownContainer"] p { font-size: 1.05rem !important; font-weight: 500 !important; }
</style>
""", unsafe_allow_html=True)

st.title("📊 퀀트 투자 웹 대시보드")
st.caption("강력한 성장 가치주: 시총 → 가격(하위 33%) → 3년 무적자 + 매출·영업 CAGR 10% → 5단계 수급·거래량(외국인 매수 OR 거래량 급증)")

with st.sidebar:
    st.header("분석 실행")
    st.subheader("시총 대상 (중소형주)")
    mcap_mode = st.selectbox(
        "시총 구간",
        ["전체 상장 종목 (코스피+코스닥)", "중소형주 (500억~2조)", "소형주 (500억~5천억)", "중형주 (5천억~2조)", "전체 (시총 상위 1000)"],
        help="전체 상장: 시총 기준 무시, 코스피+코스닥 전체 스캔",
    )
    if "전체 상장" in mcap_mode or "코스피+코스닥" in mcap_mode:
        min_b, max_t = -1, -1  # 시총 필터 무시
    elif "중소형주" in mcap_mode and "소형" not in mcap_mode and "중형" not in mcap_mode:
        min_b, max_t = 500, 2.0
    elif "소형주" in mcap_mode:
        min_b, max_t = 500, 0.5
    elif "중형주" in mcap_mode:
        min_b, max_t = 5000, 2.0
    else:
        min_b, max_t = 0, 99999  # 시총 상위 N개
    st.session_state["min_mcap_billion"] = min_b
    st.session_state["max_mcap_trillion"] = max_t
    max_stocks_options = [300, 500, 1000, 0]
    max_stocks = st.selectbox(
        "1단계 검색 종목 수 (적을수록 빠름)",
        max_stocks_options,
        index=1,
        format_func=lambda x: "제한 없음 (전체 2500+)" if x == 0 else str(x),
        help="제한 없음: 우선주·금융주 제외한 전체 종목 스캔 (5~10분 소요)",
    )
    st.session_state["max_stocks"] = max_stocks
    st.caption("3단계: 3년 무적자 + 매출·영업 CAGR 10% 이상")
    if min_b == -1:
        st.caption("코스피+코스닥 전체 상장 종목 (시총 무관)")
    elif min_b == 0:
        st.caption(f"시총 상위 {max_stocks if max_stocks else '전체'}종목 (대형주 포함)")
    else:
        st.caption(f"시총 {min_b}억~{max_t}조원 구간 최대 {max_stocks if max_stocks else '전체'}종목")
    if st.button("🚀 데이터 수집 및 분석 시작", width="stretch"):
        st.session_state["run_analysis"] = True
    if "run_analysis" not in st.session_state:
        st.session_state["run_analysis"] = False
    if "result_df" not in st.session_state:
        st.session_state["result_df"] = None
    if "price_info" not in st.session_state:
        st.session_state["price_info"] = {}
    if "fin_info" not in st.session_state:
        st.session_state["fin_info"] = {}
    if "finance_error_log" not in st.session_state:
        st.session_state["finance_error_log"] = []
    if "stage_counts" not in st.session_state:
        st.session_state["stage_counts"] = {}
    if "stage2_df" not in st.session_state:
        st.session_state["stage2_df"] = None
    if "last_error" not in st.session_state:
        st.session_state["last_error"] = None

# 탭: 퀀트 종목 발굴 | 모의 투자 포트폴리오
tab1, tab2 = st.tabs(["🔍 퀀트 종목 발굴", "📋 모의 투자 포트폴리오"])


def _apply_analysis_result(res, err):
    """분석 결과를 session_state에 반영."""
    if err:
        st.session_state["last_error"] = err
        st.session_state["result_df"] = pd.DataFrame()
        st.session_state["stage_counts"] = {}
        st.session_state["stage2_df"] = pd.DataFrame()
    elif res:
        result_df, price_info, fin_info, stage_counts, stage2_df, finance_error_log = res
        st.session_state["result_df"] = result_df
        st.session_state["price_info"] = price_info
        st.session_state["fin_info"] = fin_info
        st.session_state["stage_counts"] = stage_counts
        st.session_state["stage2_df"] = stage2_df
        st.session_state["finance_error_log"] = finance_error_log or []
        st.session_state["last_error"] = None
        if finance_error_log:
            for e in finance_error_log[:20]:
                print(f"[재무탈락] {e.get('종목코드', '')} {e.get('종목명', '')} | {e.get('에러유형', '')}: {e.get('에러내용', '')}", flush=True)
            if len(finance_error_log) > 20:
                print(f"[재무탈락] ... 외 {len(finance_error_log) - 20}건", flush=True)

with tab1:
    # 분석 실행 (동기 실행 — 버튼 클릭 시 즉시 실행, 진행률 실시간 표시)
    if st.session_state.get("run_analysis"):
        st.session_state["run_analysis"] = False
        progress_ph = st.empty()
        def _on_progress(stage: int, current: int, total: int, msg: str):
            weights = {1: (0, 0.05), 2: (0.05, 0.45), 3: (0.45, 0.75), 4: (0.75, 0.95), 5: (0.95, 1.0)}
            low, high = weights.get(stage, (0, 1))
            pct = low + (current / total) * (high - low) if total and total > 0 else low
            progress_ph.progress(min(1.0, max(0, pct)), text=msg)
        try:
            st.caption("분석이 완료될 때까지 잠시만 기다려 주세요. (300종목 기준 약 3~5분 소요)")
            with st.spinner(""):
                res = run_full_analysis(_get_end_date(), progress_callback=_on_progress)
            _apply_analysis_result(res, None)
        except Exception as e:
            _apply_analysis_result(None, str(e))
        st.rerun()
    else:
        df = st.session_state.get("result_df")
        if df is not None and not df.empty:
            st.subheader("✅ 최종 합격 종목")
            sc = st.session_state.get("stage_counts", {})
            if sc:
                with st.expander("📊 단계별 통과·탈락 현황"):
                    s1, s2, s3, s4, s5 = sc.get("1단계_시총", 0), sc.get("2단계_가격", 0), sc.get("3·4단계_재무", 0), sc.get("5단계_수급거래량", 0), sc.get("최종", 0)
                    d2, d34, d5 = sc.get("2단계_탈락", 0), sc.get("3·4단계_탈락", 0), sc.get("5단계_탈락", 0)
                    data_na = sc.get("데이터없음_탈락", 0)
                    st.write(f"1단계(시총): **{s1}**개 → 2단계(가격 하위33%): **{s2}**개 *(탈락 {d2})* → "
                             f"3·4단계(재무): **{s3}**개 *(탈락 {d34})* → 5단계(수급·거래량): **{s4}**개 *(탈락 {d5})* → **최종: {s5}개**")
                    if data_na > 0:
                        st.caption(f"⚠️ 3·4단계 탈락 중 **데이터없음**(크롤링 실패): **{data_na}**건")
            st.caption("※ 추출 결과는 **네이버 금융 재무제표**에서 반드시 확인·검증하세요.")
            st.dataframe(
                df,
                width="stretch",
                hide_index=True,
                column_config={
                    "네이버 재무제표": st.column_config.LinkColumn("재무제표 확인", display_text="🔗 네이버 금융에서 확인"),
                    "현재가": st.column_config.NumberColumn("현재가", format="%,d"),
                    "5년최고가": st.column_config.NumberColumn("5년최고가", format="%,d"),
                    "5년최저가": st.column_config.NumberColumn("5년최저가", format="%,d"),
                },
            )

            options = [f"{r['종목코드']} {r['종목명']}" for _, r in df.iterrows()]
            sel = st.selectbox("상세 분석할 종목 선택", options)
            if sel:
                ticker = sel.split()[0]
                name = " ".join(sel.split()[1:])
                st.divider()
                st.subheader(f"📈 {ticker} {name} 상세")
                naver_url = f"https://finance.naver.com/item/main.naver?code={ticker}"
                st.link_button("🔗 네이버 금융 재무제표에서 분석 확인", naver_url)
                col1, col2 = st.columns(2)
                with col1:
                    price_info = st.session_state.get("price_info", {})
                    curr_price = price_info.get(ticker, {}).get("current_price") if price_info else None
                    fig = _build_price_chart(ticker, name, _get_end_date(), current_price=curr_price)
                    st.plotly_chart(fig, width="stretch")
                with col2:
                    fin = st.session_state.get("fin_info", {}).get(ticker)
                    if fin:
                        fin_df = fin.get("fin_df")
                        cagr_dict = fin.get("cagr_dict") or {}
                        pbr_val = fin.get("PBR")
                        if fin_df is not None and not fin_df.empty:
                            tbl = _build_fin_table(fin_df)
                            if not tbl.empty:
                                st.dataframe(tbl, width="stretch", hide_index=True)
                                st.caption("최근 3개년 매출액·영업이익 (조건A: 3년 무적자)")
                        rev_cagr = cagr_dict.get("매출CAGR(%)")
                        op_cagr = cagr_dict.get("영업CAGR(%)")
                        lines = []
                        if rev_cagr is not None: lines.append(f"매출CAGR: **{rev_cagr}%**")
                        if op_cagr is not None: lines.append(f"영업CAGR: **{op_cagr}%**")
                        if pbr_val is not None: lines.append(f"PBR: **{pbr_val:.2f}**" + (" (저평가)" if pbr_val < 1.0 else ""))
                        if lines: st.markdown(" · ".join(lines))
                        if (fin_df is None or fin_df.empty) and not lines: st.info("재무 데이터 없음")
                    else:
                        st.info("재무 데이터 없음")
        else:
            # 분석 실행 후 최종 결과가 없을 때: 단계별 결과 + 2단계 통과 종목 표시
            stage_counts = st.session_state.get("stage_counts", {})
            stage2_df = st.session_state.get("stage2_df")
            last_error = st.session_state.get("last_error")
            if last_error:
                st.error(f"⚠️ **이전 실행에서 오류가 발생했습니다.**\n\n`{last_error}`\n\n터미널 로그를 확인해 보시고, SSL·연결 오류일 경우 `pip install curl_cffi` 설치 후 앱을 재시작해 보세요.")
                if st.button("에러 메시지 지우기"):
                    st.session_state["last_error"] = None
                    st.rerun()
            elif stage_counts:
                st.warning("⚠️ 최종 합격 종목 0개 — 조건이 매우 엄격합니다.")
                sc = stage_counts
                st.markdown("**📊 필터 단계별 통과·탈락 현황**")
                s1, s2, s3, s4, s5 = sc.get("1단계_시총", 0), sc.get("2단계_가격", 0), sc.get("3·4단계_재무", 0), sc.get("5단계_수급거래량", 0), sc.get("최종", 0)
                d2, d34, d5 = sc.get("2단계_탈락", 0), sc.get("3·4단계_탈락", 0), sc.get("5단계_탈락", 0)
                data_na = sc.get("데이터없음_탈락", 0)
                st.write(f"1단계(시총): **{s1}**개 → 2단계(가격 하위33%): **{s2}**개 *(탈락 {d2})* → "
                         f"3·4단계(재무): **{s3}**개 *(탈락 {d34})* → 5단계(수급·거래량): **{s4}**개 *(탈락 {d5})* → **최종: {s5}개**")
                if data_na > 0:
                    st.caption(f"⚠️ 3·4단계 탈락 중 **데이터없음**(크롤링 실패): **{data_na}**건 — SSL/연결 오류로 재무 데이터 조회 실패")
                st.caption("시총 → 가격 하위 33% → 3년 무적자 + 매출·영업 CAGR 10% 이상 → 외국인 매수 OR 거래량 급증(1.5배) 중 하나 만족.")
                st.caption("💡 **조건 완화 방법**: 시총 범위를 넓히거나, 2단계 통과 종목을 참고해 직접 검토해 보세요.")
                if stage2_df is not None and not stage2_df.empty:
                    st.divider()
                    st.subheader("📋 2단계 통과 종목 (가격 하위 33% 구간)")
                    st.caption("3·4단계(3년 무적자 + 매출·영업 CAGR 10% 이상)에서 탈락했습니다. 참고용으로 표시합니다.")
                    st.dataframe(
                        stage2_df,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "네이버 재무제표": st.column_config.LinkColumn("재무제표 확인", display_text="🔗 네이버 금융"),
                            "현재가": st.column_config.NumberColumn("현재가", format="%,d"),
                            "5년최고가": st.column_config.NumberColumn("5년최고가", format="%,d"),
                            "5년최저가": st.column_config.NumberColumn("5년최저가", format="%,d"),
                        },
                    )
                    options = [f"{r['종목코드']} {r['종목명']}" for _, r in stage2_df.iterrows()]
                    sel = st.selectbox("상세 분석할 종목 선택 (2단계 통과)", options, key="stage2_select")
                    if sel:
                        ticker = sel.split()[0]
                        name = " ".join(sel.split()[1:])
                        st.divider()
                        st.subheader(f"📈 {ticker} {name} (2단계 통과)")
                        naver_url = f"https://finance.naver.com/item/main.naver?code={ticker}"
                        st.link_button("🔗 네이버 금융 재무제표에서 분석 확인", naver_url)
                        price_info = st.session_state.get("price_info", {})
                        curr_price = price_info.get(ticker, {}).get("current_price") if price_info else None
                        fig = _build_price_chart(ticker, name, _get_end_date(), current_price=curr_price)
                        st.plotly_chart(fig, width="stretch")
            else:
                st.info("👈 사이드바에서 '데이터 수집 및 분석 시작' 버튼을 눌러주세요.")

with tab2:
    st.subheader("📋 모의 투자 포트폴리오")
    st.caption("발굴한 종목을 모의 매수하고 수익률을 추적합니다. 기록은 **Google Sheets**에 저장되어 PC·스마트폰·클라우드 재시작 후에도 유지됩니다.")
    if not _mock_gsheets_configured():
        st.error(
            "**Google Sheets 연동 정보가 없습니다.** Streamlit Cloud → **Settings → Secrets**에 "
            "`SPREADSHEET_URL`(시트 공유 URL 또는 스프레드시트 ID), `GOOGLE_CREDENTIALS`(서비스 계정 JSON 전체 문자열)를 넣으세요. "
            "선택: `WORKSHEET_NAME`(기본값 `mock_portfolio`). 로컬은 `.streamlit/secrets.toml`에 동일 키 또는 `[mock_portfolio_gsheets]` 테이블을 사용할 수 있습니다."
        )
    elif _get_mock_portfolio_worksheet() is None:
        st.error(
            "**스프레드시트에 연결할 수 없습니다.** URL·JSON 형식, **서비스 계정 이메일**을 해당 시트에 **편집자**로 공유했는지 확인하세요."
        )
    _c_ar1, _c_ar2 = st.columns([3, 1])
    with _c_ar1:
        _mock_auto_price = st.checkbox(
            "실시간 가격 자동 갱신",
            value=False,
            key="mock_auto_price_refresh",
            help="켜면 아래 주기마다 현재가·평가금액·수익률을 다시 불러옵니다. 장 마감 후에는 당일 종가 기준일 수 있습니다.",
        )
    with _c_ar2:
        _mock_refresh_sec = st.selectbox(
            "갱신 주기",
            options=[30, 60, 120, 300],
            index=1,
            format_func=lambda s: f"{s}초",
            key="mock_price_refresh_interval",
            disabled=not _mock_auto_price,
            label_visibility="visible",
        )
    if _mock_auto_price:
        st.caption(
            "표에서 매수 정보를 수정할 때는 갱신 때문에 저장하지 않은 편집이 덮어씌워질 수 있으니, 수정 중에는 자동 갱신을 끄는 것을 권장합니다. "
            "가격 갱신은 이 탭의 포트폴리오 블록만 다시 그리므로, 1탭 분석 전체가 반복 실행되지는 않습니다(Streamlit 1.33+ 필요)."
        )

    if _mock_gsheets_configured() and _get_mock_portfolio_worksheet() is not None:
        st.caption(
            "📱 **스마트폰**에서 PC와 다르게 보이면: 아래 **다시 불러오기**를 누른 뒤, 그래도 같으면 브라우저에서 이 주소의 **캐시/사이트 데이터 삭제**를 해 보세요. "
            "PC는 `localhost`로 열고 폰은 **같은 주소**로 열어야 합니다(집 PC면 `http://PC_IP:8501`, **Streamlit Cloud**면 배포 URL만 둘 다 사용)."
        )
        if st.button(
            "🔄 시트·시세 캐시 무시하고 다시 불러오기",
            key="mock_sheet_hard_refresh",
            use_container_width=True,
            help="Google 시트 내용과 현재가 캐시를 비우고 최신으로 가져옵니다.",
        ):
            _invalidate_mock_portfolio_sheet_cache()
            _invalidate_mock_price_caches()
            st.rerun()

    # 2~3글자만 입력하면 드롭다운으로 매칭 종목 표시 (ETF 등 긴 이름 입력 편의)
    stock_options = _get_krx_stock_options()
    search_key = st.text_input("종목 검색 (2글자 이상 입력)", placeholder="예: tiger 미국, kod 200, 삼성, 005930", key="mock_search")
    filtered_options = []
    if search_key and len(search_key.strip()) >= 2:
        # 띄어쓰기로 분리 후, 각 단어가 모두 포함된 종목 검색 (TIGER 미국테크TOP10 등)
        tokens = [t.strip().lower() for t in search_key.split() if t.strip()]
        if tokens:
            def _matches(o: str) -> bool:
                lo = o.lower()
                return all(t in lo for t in tokens)
            filtered_options = [o for o in stock_options if _matches(o)]
    # 검색 시 최대 50건 드롭다운 (ETF 긴 이름도 한눈에 선택 가능)
    display_options = filtered_options[:50] if filtered_options else []

    with st.form("paper_trading_mock_buy_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if display_options:
                sel_stock = st.selectbox("검색 결과에서 선택", display_options, key="mock_stock_select")
                st.caption(f"💰 {len(filtered_options)}건 중 {len(display_options)}건 표시")
            else:
                sel_stock = None
                st.info("← 2글자 이상 입력하면 드롭다운 표시")
        with c2:
            price_str = st.text_input("매수단가(원)", value="", key="mock_price", placeholder="예: 10000 또는 13534.5")
            price = 0.0
            if price_str and price_str.strip():
                try:
                    price = float(price_str.strip().replace(",", ""))
                except ValueError:
                    price = 0.0
        with c3:
            qty = st.number_input("매수수량", min_value=1, value=1, step=1, key="mock_qty")
        with c4:
            st.caption("")  # 레이아웃용
        if st.form_submit_button("모의 매수 추가"):
            if sel_stock and price > 0 and qty > 0:
                parsed = _parse_stock_selection(sel_stock)
                if parsed:
                    code, name = parsed
                    code = str(code).zfill(6)
                    raw = _load_mock_portfolio()
                    row = pd.DataFrame([{
                        "매수일자": datetime.now().strftime("%Y-%m-%d"),
                        "종목코드": code,
                        "종목명": name,
                        "매수단가": round(float(price), 2),
                        "매수수량": int(qty),
                    }])
                    try:
                        _save_mock_portfolio(pd.concat([raw, row], ignore_index=True))
                    except RuntimeError as e:
                        st.error(str(e))
                    else:
                        st.success(f"✅ {name}({code}) {qty}주 @ {price:,.2f}원 모의 매수 추가됨.")
                        st.rerun()
                else:
                    st.warning("종목 형식 오류. '종목명 (종목코드)' 형식으로 선택해 주세요.")
            else:
                st.warning("종목 선택 후, 매수단가(1원 이상)와 매수수량을 입력하세요.")

    raw_df = _load_mock_portfolio()
    if raw_df.empty:
        st.info("모의 매수 내역이 없습니다. 위 폼에서 추가해 주세요.")
    else:
        _pf_run_every = float(_mock_refresh_sec) if _mock_auto_price else None
        st.fragment(run_every=_pf_run_every)(_render_mock_portfolio_inner)()
        _render_etf_return_chart_outside_fragment()
