#!/usr/bin/env python3
"""트라이팟 자동 판정 → index.html / data.json / backtest.json 생성.

규칙(한규님 정의)
  1) 시장 상태: NDX 종가 > 250일선×1.01 → 상승, < 250일선×0.95 → 하락, 그 사이 → 어제 상태 유지
  2) 목표 비중
       상승: VIX10 < 28 이고 52주 고점 대비 낙폭 <= 9% → TQQQ 100%, 아니면 QQQ 50% / QLD 50%
       하락: VIX10 < 18 → QQQ 50% / QLD 50%, 아니면 현금 100%
  3) 어제와 (시장 상태, 목표 비중)이 같으면 유지, 다르면 리밸런싱

추가 기능: 기준선까지 거리, 텔레그램 알림, 과거 백테스트, 투자금 계산기,
           NDX·250일선 차트, 데이터 예비 경로(FRED)
"""
import datetime as dt
import html
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

# ===================== 설정 (숫자를 바꾸면 규칙이 바뀝니다) =====================
START_DATE = "2026-09-18"   # 기록 시작일 (엑셀 기록 시트 첫 행과 같게)
INITIAL_STATE = "상승"      # 시작일에 두 선 사이일 때 쓸 첫날 상태
MA_DAYS = 250               # 이동평균 일수
UP_BAND = 1.01              # 상승 판정: 250일선 × 1.01 초과
DOWN_BAND = 0.95            # 하락 판정: 250일선 × 0.95 미만
VIX_DAYS = 10               # VIX 평균 일수
VIX_UP_LIMIT = 28           # 상승 상태에서 VIX10 이 이 값 미만이어야 공격
VIX_DOWN_LIMIT = 18         # 하락 상태에서 VIX10 이 이 값 미만이면 QQQ/QLD
DD_LIMIT = 0.09             # 52주 고점 대비 낙폭 한도 (9%)
HIGH_WINDOW = "365D"        # 52주 고점 계산 기간 (장중 고가 기준)
SHOW_ROWS = 15              # 페이지에 보여줄 최근 기록 수

# --- 백테스트 가정 (실제 ETF 수익률이 아니라 NDX 일수익률로 만든 근사치입니다) ---
BACKTEST_ON = True
BORROW_RATE = 0.03          # 레버리지 차입 비용(연). 실제는 금리에 따라 0~6% 이상 변동
CASH_RATE = 0.0             # 현금 수익률(연). 보수적으로 0
SWITCH_COST = 0.001         # 비중을 바꿀 때마다 0.1% 비용(수수료·스프레드·환전 근사)
EXPENSE = {"QQQ": 0.0020, "QLD": 0.0095, "TQQQ": 0.0084}   # 운용보수(연)
EXEC_LAG = 1                # 1 = 판정 다음 거래일 종가에 체결, 0 = 판정한 종가에 바로 체결(낙관적)
# ===============================================================================

OUT_DIR = Path(__file__).resolve().parent
KST = dt.timezone(dt.timedelta(hours=9))
TQQQ = "TQQQ 100%"
MIX = "QQQ 50% / QLD 50%"
CASH = "현금 100%"


# ----------------------------- 판정 -----------------------------
def judge(close, sma, vix10, high52, prev_state):
    """하루치 판정. (시장 상태, 목표 비중, 낙폭)을 돌려준다."""
    if close > sma * UP_BAND:
        state = "상승"
    elif close < sma * DOWN_BAND:
        state = "하락"
    else:
        state = prev_state
    dd = (high52 - close) / high52
    if state == "상승":
        target = TQQQ if (vix10 < VIX_UP_LIMIT and dd <= DD_LIMIT) else MIX
    else:
        target = MIX if vix10 < VIX_DOWN_LIMIT else CASH
    return state, target, dd


# ----------------------------- 데이터 -----------------------------
def _clean_index(s):
    s = s.copy()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    return s[~s.index.duplicated(keep="last")]


def fetch_yahoo():
    import yfinance as yf

    last = None
    for i in range(4):
        try:
            ndx = yf.Ticker("^NDX").history(period="max", interval="1d", auto_adjust=False)
            vix = yf.Ticker("^VIX").history(period="max", interval="1d", auto_adjust=False)
            if len(ndx) > MA_DAYS + 30 and len(vix) > VIX_DAYS + 5:
                ndx, vix = _clean_index(ndx), _clean_index(vix)
                df = pd.DataFrame({"close": ndx["Close"], "high": ndx["High"]})
                df["vix"] = vix["Close"]
                return df, "Yahoo Finance"
            last = f"데이터 부족 (NDX {len(ndx)}행, VIX {len(vix)}행)"
        except Exception as e:
            last = str(e)
        print(f"Yahoo 재시도 {i + 1}: {last}", file=sys.stderr)
        time.sleep(15 * (i + 1))
    raise RuntimeError(last)


def _fred(series):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    s = pd.read_csv(url, na_values=".")
    s.columns = ["date", "v"]
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["v"].astype(float).dropna()


def fetch_fred():
    """예비 경로. 고가 자료가 없어 52주 고점은 종가 기준이 되고, 하루 늦게 올라올 수 있다."""
    ndx, vix = _fred("NASDAQ100"), _fred("VIXCLS")
    df = pd.DataFrame({"close": ndx})
    df["high"] = df["close"]
    df["vix"] = vix
    return df, "FRED (예비 경로: Yahoo 접속 실패)"


def load():
    try:
        df, src = fetch_yahoo()
    except Exception as e:
        print(f"Yahoo 실패 → FRED 예비 경로 시도: {e}", file=sys.stderr)
        try:
            df, src = fetch_fred()
        except Exception as e2:
            sys.exit(f"데이터 수신 실패 (Yahoo, FRED 모두): {e2}")
    df = df.dropna(subset=["close", "vix"]).sort_index()
    df["high"] = df["high"].where(df["high"] > 0).fillna(df["close"])
    df["high"] = df[["high", "close"]].max(axis=1)
    if len(df) < MA_DAYS + 30:
        sys.exit("데이터가 너무 짧습니다")
    age = (pd.Timestamp(dt.datetime.now(dt.timezone.utc).date()) - df.index[-1]).days
    if age > 6:
        sys.exit(f"데이터가 오래됐습니다 (마지막 {df.index[-1].date()})")
    return df, src


def add_indicators(df):
    df = df.copy()
    df["sma"] = df["close"].rolling(MA_DAYS).mean()
    df["vix10"] = df["vix"].rolling(VIX_DAYS).mean()
    df["hi52"] = df["high"].rolling(HIGH_WINDOW).max()
    return df


def fetch_prices():
    """투자금 계산기용 최근 종가와 환율. 실패해도 계산기만 금액 표시로 동작한다."""
    out = {}
    try:
        import yfinance as yf

        for k, sym in {"QQQ": "QQQ", "QLD": "QLD", "TQQQ": "TQQQ", "FX": "KRW=X"}.items():
            h = yf.Ticker(sym).history(period="7d", interval="1d")["Close"].dropna()
            if len(h):
                out[k] = round(float(h.iloc[-1]), 2)
                out["asof"] = h.index[-1].strftime("%Y-%m-%d")
    except Exception as e:
        print(f"가격 수신 실패(계산기는 금액만 표시): {e}", file=sys.stderr)
    return out


# ----------------------------- 일별 기록 -----------------------------
def compute(df):
    rows = []
    state = INITIAL_STATE
    prev = None
    for d, r in df[df.index >= START_DATE].iterrows():
        if r[["sma", "vix10", "hi52"]].isna().any():
            continue
        state, target, dd = judge(r["close"], r["sma"], r["vix10"], r["hi52"], state)
        if prev is None:
            action = "첫 등록 → 목표 비중대로 매수"
        elif (state, target) == prev:
            action = "어제와 동일 → 유지"
        else:
            action = "변경됨 → 리밸런싱"
        prev = (state, target)
        rows.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "close": round(float(r["close"]), 2),
                "sma": round(float(r["sma"]), 2),
                "up_line": round(float(r["sma"]) * UP_BAND, 2),
                "down_line": round(float(r["sma"]) * DOWN_BAND, 2),
                "vix10": round(float(r["vix10"]), 2),
                "hi52": round(float(r["hi52"]), 2),
                "dd": round(dd * 100, 2),
                "state": state,
                "target": target,
                "action": action,
            }
        )
    return rows


def distance_lines(last):
    """판정이 바뀌려면 무엇이 필요한지 보여주는 문장들."""
    c, up, dn = last["close"], last["up_line"], last["down_line"]
    v = last["vix10"]
    out = []
    if last["state"] == "하락":
        out.append(("상승으로 바뀌려면", f"종가가 {up:,.0f} 초과 (지금보다 {(up / c - 1) * 100:+.1f}%)"))
        if v >= VIX_DOWN_LIMIT:
            out.append(("현금 → QQQ/QLD가 되려면", f"VIX 10일 평균이 {VIX_DOWN_LIMIT} 미만 (지금 {v:.1f}, {v - VIX_DOWN_LIMIT:.1f} 더 내려야)"))
        else:
            out.append(("지금 QQQ/QLD인 이유", f"VIX 10일 평균 {v:.1f} < {VIX_DOWN_LIMIT}. {VIX_DOWN_LIMIT} 이상이 되면 현금"))
    else:
        out.append(("하락으로 바뀌려면", f"종가가 {dn:,.0f} 미만 (지금보다 {(dn / c - 1) * 100:+.1f}%)"))
        need = last["hi52"] * (1 - DD_LIMIT)
        if last["target"] == TQQQ:
            out.append(("TQQQ가 풀리는 조건", f"VIX 평균 {VIX_UP_LIMIT} 이상 (지금 {v:.1f}) 또는 종가 {need:,.0f} 미만 (낙폭 {DD_LIMIT * 100:.0f}% 초과)"))
        else:
            why = []
            if v >= VIX_UP_LIMIT:
                why.append(f"VIX 평균 {v:.1f} ≥ {VIX_UP_LIMIT}")
            if last["dd"] / 100 > DD_LIMIT:
                why.append(f"낙폭 {last['dd']:.1f}% > {DD_LIMIT * 100:.0f}% (종가 {need:,.0f} 이상이면 해소)")
            out.append(("TQQQ가 되려면", " / ".join(why) + " 이 모두 풀려야"))
    return out


# ----------------------------- 백테스트 -----------------------------
def _metrics(x):
    eq = (1 + x).cumprod()
    yrs = max((x.index[-1] - x.index[0]).days / 365.25, 1e-9)
    return {
        "mult": float(eq.iloc[-1]),
        "cagr": float(eq.iloc[-1] ** (1 / yrs) - 1),
        "mdd": float((eq / eq.cummax() - 1).min()),
    }


def run_backtest(df):
    d = df.dropna(subset=["sma", "vix10", "hi52"]).copy()
    if len(d) < 1500:
        return None
    state, S, T = INITIAL_STATE, [], []
    for c, s, v, h in zip(d["close"].values, d["sma"].values, d["vix10"].values, d["hi52"].values):
        state, tg, _ = judge(c, s, v, h, state)
        S.append(state)
        T.append(tg)
    d["target"] = T
    r = d["close"].pct_change().fillna(0.0)
    b = BORROW_RATE / 252
    qqq = r - EXPENSE["QQQ"] / 252
    # 레버리지 ETF는 하루에 -100% 아래로 내려갈 수 없으므로 하한을 둔다
    qld = (2 * r - b - EXPENSE["QLD"] / 252).clip(lower=-0.999)
    tqqq = (3 * r - 2 * b - EXPENSE["TQQQ"] / 252).clip(lower=-0.999)
    cash = pd.Series(CASH_RATE / 252, index=r.index)
    leg = {TQQQ: tqqq, MIX: 0.5 * qqq + 0.5 * qld, CASH: cash}

    def strat(lag):
        pos = d["target"].shift(1 + lag)
        x = pd.Series(CASH_RATE / 252, index=r.index)
        for tg, ret in leg.items():
            m = pos == tg
            x[m] = ret[m]
        prevpos = pos.shift(1)
        chg = (pos != prevpos) & pos.notna() & prevpos.notna()
        return x - SWITCH_COST * chg, pos

    x0, _ = strat(0)
    x1, pos1 = strat(1)
    x, pos = (x1, pos1) if EXEC_LAG == 1 else (x0, strat(0)[1])
    first = pos.first_valid_index()
    end = d.index[-1]
    names = [("트라이팟 규칙", x), ("나스닥100(QQQ) 보유", qqq), ("QLD 보유", qld), ("TQQQ 보유", tqqq)]
    windows = []
    for label, start in [
        ("전체", first),
        ("최근 10년", max(first, end - pd.DateOffset(years=10))),
        ("최근 5년", max(first, end - pd.DateOffset(years=5))),
    ]:
        rows = []
        for n, s in names:
            m = _metrics(s.loc[start:])
            m["name"] = n
            rows.append(m)
        p = pos.loc[start:].dropna()
        yrs = (p.index[-1] - p.index[0]).days / 365.25
        sw = int((p != p.shift()).sum() - 1)
        windows.append(
            {
                "label": label,
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "rows": rows,
                "switches": sw,
                "per_year": sw / yrs,
                "cash_pct": float((p == CASH).mean()),
                "tqqq_pct": float((p == TQQQ).mean()),
            }
        )
    # 곡선(전체 구간, 주 단위로 솎아서)
    base = slice(first, None)
    curves = {n: (1 + s.loc[base]).cumprod() for n, s in [names[0], names[1], names[3]]}
    step = max(1, len(curves[names[0][0]]) // 600)
    idx = curves[names[0][0]].index[::step]
    chart = {"dates": [i.strftime("%Y-%m-%d") for i in idx], "series": {n: [float(c.loc[i]) for i in idx] for n, c in curves.items()}}
    return {
        "windows": windows,
        "chart": chart,
        "lag0_cagr_full": _metrics(x0.loc[first:])["cagr"],
        "assumptions": {
            "borrow": BORROW_RATE, "cash": CASH_RATE, "switch_cost": SWITCH_COST,
            "expense": EXPENSE, "lag": EXEC_LAG,
        },
    }


# ----------------------------- 알림 -----------------------------
def notify(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("텔레그램 설정이 없어 알림을 건너뜁니다")
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20).read()
        print("텔레그램 알림 전송")
        return True
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
        return False


def page_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


# ----------------------------- 화면 -----------------------------
def fmt(x, n=2):
    return f"{x:,.{n}f}"


def svg_chart(dates, series, log=False, xlabel=lambda s: s[:4], ylabel=lambda v: f"{v:,.0f}", label="차트", w=640, h=230):
    ml, mr, mt, mb = 56, 10, 10, 24
    vals = [v for s in series for v in s["y"] if v is not None and v == v and v > 0]
    tr = (lambda v: math.log10(v)) if log else (lambda v: v)
    lo, hi = min(tr(v) for v in vals), max(tr(v) for v in vals)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    n = len(dates)
    X = lambda i: ml + (w - ml - mr) * i / max(n - 1, 1)
    Y = lambda v: mt + (h - mt - mb) * (1 - (tr(v) - lo) / (hi - lo))
    p = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{html.escape(label)}">']
    for k in range(5):
        g = lo + (hi - lo) * k / 4
        y = mt + (h - mt - mb) * (1 - k / 4)
        val = 10 ** g if log else g
        p.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        p.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{ylabel(val)}</text>')
    last_x = -999
    for k in range(6):
        i = round((n - 1) * k / 5)
        x = X(i)
        t = xlabel(dates[i])
        anchor = "start" if k == 0 else ("end" if k == 5 else "middle")
        if x - last_x > 40:
            p.append(f'<text x="{x:.1f}" y="{h - 6}" class="ax" text-anchor="{anchor}">{t}</text>')
            last_x = x
    for s in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(s["y"]) if v is not None and v == v and v > 0)
        p.append(f'<polyline points="{pts}" class="ln {s["cls"]}"/>')
    p.append("</svg>")
    return "".join(p)


def legend(items):
    return "<div class='legend'>" + "".join(f"<span><i class='sw {c}'></i>{html.escape(n)}</span>" for n, c in items) + "</div>"


CSS = """
:root{--bg:#f6f7f9;--fg:#15181d;--mut:#5d6672;--card:#fff;--line:#e1e5ea;
--atk:#0d7a4f;--atkbg:#e3f4ec;--mid:#8a5a00;--midbg:#fbf0d9;--def:#a3262a;--defbg:#fbe6e6;
--c0:#1c64f2;--c1:#6b7280;--c2:#d9480f;--c3:#0d7a4f}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e8ebef;--mut:#9aa4b0;--card:#171b21;--line:#2a313a;
--atk:#53d19b;--atkbg:#12281f;--mid:#f0b94d;--midbg:#2b2210;--def:#ff8a8d;--defbg:#2e1516;
--c0:#6ea0ff;--c1:#9aa4b0;--c2:#ff9a5c;--c3:#53d19b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}
main{max-width:640px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:18px;margin:0 0 14px} h2{font-size:15px;margin:26px 0 8px;color:var(--mut);font-weight:600}
.hero{border-radius:14px;padding:20px 18px}
.hero.atk{background:var(--atkbg);color:var(--atk)} .hero.mid{background:var(--midbg);color:var(--mid)} .hero.def{background:var(--defbg);color:var(--def)}
.asof{font-size:13px;opacity:.85} .target{font-size:clamp(22px,7.2vw,32px);font-weight:800;margin:4px 0;letter-spacing:-.5px;white-space:nowrap}
.sub{font-size:15px} .do{margin-top:10px;font-weight:700;font-size:15px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line)}
.kv th{font-weight:500;color:var(--mut);width:42%} .kv td{font-variant-numeric:tabular-nums;font-weight:600} .note{color:var(--mut);font-size:12px;font-weight:400!important}
.hist thead th,.bt thead th{font-size:12px;color:var(--mut);font-weight:600} .hist td,.bt td{font-variant-numeric:tabular-nums;white-space:nowrap}
.bt td.n,.bt th.n{text-align:right} tr.me td{font-weight:700}
tr.chg td{font-weight:700;background:var(--midbg)}
.scroll{overflow-x:auto} tr:last-child td,tr:last-child th{border-bottom:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card ul{margin:0;padding-left:18px;font-size:14px} .card li{margin:4px 0}
.calc label{font-size:14px;color:var(--mut);display:block;margin-bottom:6px}
.calc input{width:100%;font-size:18px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
#calc-out{margin-top:10px}
.chart{width:100%;height:auto;display:block;background:var(--card);border:1px solid var(--line);border-radius:10px}
.chart .grid{stroke:var(--line);stroke-width:1} .chart .ax{fill:var(--mut);font-size:10px}
.ln{fill:none;stroke-width:1.8;stroke-linejoin:round} .c0{stroke:var(--c0);background:var(--c0)} .c1{stroke:var(--c1);background:var(--c1)} .c2{stroke:var(--c2);background:var(--c2)} .c3{stroke:var(--c3);background:var(--c3)}
.ln.dash{stroke-dasharray:4 3}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--mut);margin:6px 2px 0}
.sw{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;border-radius:2px}
details{margin-top:26px} summary{cursor:pointer;font-size:15px;color:var(--mut);font-weight:600;padding:4px 0}
.warn{font-size:13px;color:var(--mut);margin:8px 0}
footer{margin-top:28px;font-size:12px;color:var(--mut)}
"""

JS = """
(function(){
var D=__DATA__;
var el=document.getElementById('cap'),out=document.getElementById('calc-out');
if(!el)return;
function num(v){return Number(String(v).replace(/[^0-9]/g,''))||0}
function won(n){return Math.round(n).toLocaleString('ko-KR')+'원'}
function calc(){
  var cap=num(el.value);
  if(!cap){out.innerHTML='<span class="note">투자금을 입력하면 목표 비중대로 계산해 드려요.</span>';return}
  var al=D.alloc,h='<table class="bt"><thead><tr><th>종목</th><th class="n">금액</th><th class="n">약 주수</th></tr></thead><tbody>';
  al.forEach(function(a){
    var amt=cap*a[1],sh='-';
    if(a[0]!=='현금'&&D.prices[a[0]]&&D.prices.FX){sh=Math.floor(amt/(D.prices.FX*D.prices[a[0]]))+'주'}
    h+='<tr><td>'+a[0]+' '+Math.round(a[1]*100)+'%</td><td class="n">'+won(amt)+'</td><td class="n">'+sh+'</td></tr>';
  });
  h+='</tbody></table>';
  if(D.prices.FX){h+='<div class="note" style="margin-top:6px">'+(D.prices.asof||'')+' 종가 · 환율 '+D.prices.FX.toLocaleString('ko-KR')+'원 기준 (실제 체결가와 다를 수 있어요)</div>'}
  out.innerHTML=h;
}
try{el.value=localStorage.getItem('tripod_cap')||''}catch(e){}
el.addEventListener('input',function(){
  var n=num(el.value);el.value=n?n.toLocaleString('ko-KR'):'';
  try{localStorage.setItem('tripod_cap',el.value)}catch(e){}
  calc();
});
calc();
})();
"""


def bt_html(bt):
    a = bt["assumptions"]
    tabs = []
    for w in bt["windows"]:
        trs = []
        for i, r in enumerate(w["rows"]):
            trs.append(
                f"<tr class='{'me' if i == 0 else ''}'><td>{html.escape(r['name'])}</td>"
                f"<td class='n'>{r['mult']:,.1f}배</td><td class='n'>{r['cagr'] * 100:+.1f}%</td><td class='n'>{r['mdd'] * 100:.0f}%</td></tr>"
            )
        tabs.append(
            f"<h2>{w['label']} ({w['start']} ~ {w['end']})</h2>"
            "<div class='scroll'><table class='bt'><thead><tr><th>전략</th><th class='n'>총 수익 배수</th><th class='n'>연 수익률</th><th class='n'>최대 낙폭</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div>"
            f"<div class='warn'>이 구간에서 비중을 바꾼 횟수 {w['switches']}회 (연 평균 {w['per_year']:.1f}회), "
            f"현금으로 있던 기간 {w['cash_pct'] * 100:.0f}%, TQQQ 100%였던 기간 {w['tqqq_pct'] * 100:.0f}%</div>"
        )
    ch = bt["chart"]
    names = list(ch["series"].keys())
    cls = ["c0", "c1", "c2"]
    svg = svg_chart(
        ch["dates"],
        [{"y": ch["series"][n], "cls": c} for n, c in zip(names, cls)],
        log=True,
        ylabel=lambda v: f"{v:,.0f}배" if v >= 10 else f"{v:.1f}배",
        label="전략별 누적 수익 (로그 눈금)",
    )
    exp = ", ".join(f"{k} {v * 100:.2f}%" for k, v in a["expense"].items())
    return (
        "".join(tabs)
        + "<h2>누적 수익 (세로축은 로그 눈금)</h2>"
        + svg
        + legend(list(zip(names, cls)))
        + "<div class='warn'><b>읽을 때 주의</b><br>"
        f"· 실제 ETF 기록이 아니라 나스닥100 일 수익률에 2배·3배를 곱해 만든 근사치입니다. 차입 비용 연 {a['borrow'] * 100:.0f}%, 현금 수익 연 {a['cash'] * 100:.0f}%, 운용보수({exp}), 비중을 바꿀 때마다 {a['switch_cost'] * 100:.1f}% 비용을 넣었습니다.<br>"
        f"· 판정 다음 거래일 종가에 체결한다고 가정했습니다(같은 종가에 바로 체결하면 전체 구간 연 수익률이 {bt['lag0_cagr_full'] * 100:+.1f}%로 더 좋게 나오지만 현실적이지 않습니다).<br>"
        "· 세금(해외주식 양도소득세 22%), 환율 변동, 세금·수수료 정산 시차는 넣지 않았습니다. 규칙 수치는 과거 데이터를 보고 정해진 값이라 앞으로도 같은 결과가 나온다는 보장이 없습니다.</div>"
    )


def render(rows, src, bt, prices, chart250):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if not rows:
        body, data = "<p>아직 기록이 없습니다.</p>", {"alloc": [], "prices": {}}
    else:
        last = rows[-1]
        tone = {TQQQ: "atk", MIX: "mid", CASH: "def"}[last["target"]]
        changed = last["action"].startswith("변경")
        alloc = {TQQQ: [["TQQQ", 1]], MIX: [["QQQ", 0.5], ["QLD", 0.5]], CASH: [["현금", 1]]}[last["target"]]
        data = {"alloc": alloc, "prices": prices}
        dist = "".join(f"<li><b>{html.escape(a)}</b> {html.escape(b)}</li>" for a, b in distance_lines(last))
        checks = [
            ("NDX 종가", fmt(last["close"]), ""),
            (f"{MA_DAYS}일선", fmt(last["sma"]), ""),
            ("상승 기준선 (×%.2f)" % UP_BAND, fmt(last["up_line"]), "이 선 위 = 상승"),
            ("하락 기준선 (×%.2f)" % DOWN_BAND, fmt(last["down_line"]), "이 선 아래 = 하락"),
            (f"VIX {VIX_DAYS}일 평균", fmt(last["vix10"]), f"상승일 때 {VIX_UP_LIMIT} 미만 / 하락일 때 {VIX_DOWN_LIMIT} 미만"),
            ("52주 고점", fmt(last["hi52"]), ""),
            ("고점 대비 낙폭", fmt(last["dd"]) + "%", f"{DD_LIMIT * 100:.0f}% 이하여야 공격"),
        ]
        check_html = "".join(
            f"<tr><th>{html.escape(a)}</th><td>{b}</td><td class='note'>{html.escape(c)}</td></tr>" for a, b, c in checks
        )
        hist_html = "".join(
            f"<tr class='{'chg' if r['action'].startswith('변경') else ''}'>"
            f"<td>{r['date'][5:]}</td><td>{fmt(r['close'], 0)}</td><td>{r['state']}</td><td>{html.escape(r['target'])}</td></tr>"
            for r in reversed(rows[-SHOW_ROWS:])
        )
        cd = chart250
        price_chart = svg_chart(
            cd["dates"],
            [
                {"y": cd["close"], "cls": "c0"},
                {"y": cd["sma"], "cls": "c1"},
                {"y": cd["up"], "cls": "c3 dash"},
                {"y": cd["down"], "cls": "c2 dash"},
            ],
            xlabel=lambda s: s[2:7],
            label="NDX 종가와 250일선",
        )
        bt_block = (
            "<details><summary>과거 규칙 점검 (백테스트) 펼치기</summary>" + bt_html(bt) + "</details>" if bt else ""
        )
        body = f"""
<section class="hero {tone}">
  <div class="asof">{last['date']} 미국장 종가 기준</div>
  <div class="target">{html.escape(last['target'])}</div>
  <div class="sub">시장 상태 <b>{last['state']}</b> · {html.escape(last['action'])}</div>
  <div class="do">{'오늘 해야 할 일: 목표 비중으로 리밸런싱' if changed else '오늘 해야 할 일: 없음 (그대로 유지)'}</div>
</section>
<h2>판정이 바뀌려면</h2>
<div class="card"><ul>{dist}</ul></div>
<h2>내 투자금으로 계산</h2>
<div class="card calc">
  <label for="cap">투자금 (원) · 입력값은 이 기기에만 저장돼요</label>
  <input id="cap" inputmode="numeric" autocomplete="off" placeholder="예: 20,000,000">
  <div id="calc-out"></div>
</div>
<h2>판정에 쓴 숫자</h2>
<table class="kv">{check_html}</table>
<h2>NDX와 250일선 (최근 1년)</h2>
{price_chart}
{legend([("NDX 종가", "c0"), (f"{MA_DAYS}일선", "c1"), ("상승 기준선", "c3"), ("하락 기준선", "c2")])}
<h2>최근 기록</h2>
<div class="scroll"><table class="hist">
<thead><tr><th>날짜</th><th>NDX</th><th>상태</th><th>목표 비중 (색칠 = 변경일)</th></tr></thead>
<tbody>{hist_html}</tbody></table></div>
{bt_block}
"""
    js = JS.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>트라이팟 일별 판정</title>
<style>{CSS}</style></head><body><main>
<h1>트라이팟 일별 판정</h1>
{body}
<footer>
생성 {now} · 자료: {html.escape(src)} (^NDX, ^VIX)<br>
기록 시작 {START_DATE}. 개인이 정한 규칙을 기계적으로 계산한 결과이며 투자 권유가 아닙니다. 매매 판단과 책임은 본인에게 있습니다.
</footer></main><script>{js}</script></body></html>"""


# ----------------------------- 실행 -----------------------------
def main():
    old_last = None
    try:
        old_last = json.loads((OUT_DIR / "data.json").read_text(encoding="utf-8"))[-1]["date"]
    except Exception:
        pass

    df, src = load()
    df = add_indicators(df)
    rows = compute(df)

    bt = None
    if BACKTEST_ON:
        try:
            bt = run_backtest(df)
        except Exception as e:  # 백테스트가 실패해도 일별 판정은 계속
            print(f"백테스트 실패(건너뜀): {e}", file=sys.stderr)

    t = df.dropna(subset=["sma"]).tail(250)
    chart250 = {
        "dates": [i.strftime("%Y-%m-%d") for i in t.index],
        "close": [round(float(v), 2) for v in t["close"]],
        "sma": [round(float(v), 2) for v in t["sma"]],
        "up": [round(float(v) * UP_BAND, 2) for v in t["sma"]],
        "down": [round(float(v) * DOWN_BAND, 2) for v in t["sma"]],
    }
    prices = fetch_prices()

    (OUT_DIR / "data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    if bt:
        (OUT_DIR / "backtest.json").write_text(json.dumps(bt, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(render(rows, src, bt, prices, chart250), encoding="utf-8")

    if os.environ.get("TEST_NOTIFY", "").lower() == "true":
        notify("[트라이팟] 알림 테스트입니다. 이 메시지가 보이면 연결 성공!")

    if rows:
        r = rows[-1]
        print(f"{r['date']} {r['state']} → {r['target']} ({r['action']}) · 출처 {src}")
        if r["action"].startswith("변경") and r["date"] != old_last:
            notify(
                f"[트라이팟] {r['date']} 종가 기준 판정이 바뀌었어요\n"
                f"시장 상태: {r['state']}\n목표 비중: {r['target']}\n"
                f"NDX {r['close']:,.0f} / 250일선 {r['sma']:,.0f} / VIX10 {r['vix10']:.1f}\n{page_url()}"
            )


if __name__ == "__main__":
    main()
