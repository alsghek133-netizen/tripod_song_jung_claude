#!/usr/bin/env python3
"""트라이팟 자동 판정 → index.html / data.json 생성.

규칙(한규님 정의)
  1) 시장 상태: NDX 종가 > 250일선×1.01 → 상승, < 250일선×0.95 → 하락, 그 사이 → 어제 상태 유지
  2) 목표 비중
       상승: VIX10 < 28 이고 52주 고점 대비 낙폭 <= 9% → TQQQ 100%, 아니면 QQQ 50% / QLD 50%
       하락: VIX10 < 18 → QQQ 50% / QLD 50%, 아니면 현금 100%
  3) 어제와 (시장 상태, 목표 비중)이 같으면 유지, 다르면 리밸런싱
"""
import datetime as dt
import html
import json
import sys
import time
from pathlib import Path

# ===================== 설정 (숫자를 바꾸면 규칙이 바뀝니다) =====================
START_DATE = "2026-09-18"   # 기록 시작일 (엑셀 기록 시트 첫 행과 같게)
INITIAL_STATE = "상승"      # 시작일에 선(250일선 ±) 사이에 있을 때 쓸 첫날 상태
MA_DAYS = 250               # 이동평균 일수
UP_BAND = 1.01              # 상승 판정: 250일선 × 1.01 초과
DOWN_BAND = 0.95            # 하락 판정: 250일선 × 0.95 미만
VIX_DAYS = 10               # VIX 평균 일수
VIX_UP_LIMIT = 28           # 상승 상태에서 VIX10 이 이 값 미만이어야 공격
VIX_DOWN_LIMIT = 18         # 하락 상태에서 VIX10 이 이 값 미만이면 QQQ/QLD
DD_LIMIT = 0.09             # 52주 고점 대비 낙폭 한도 (9%)
HIGH_WINDOW = "365D"        # 52주 고점 계산 기간 (일 단위, 장중 고가 기준)
SHOW_ROWS = 15              # 페이지에 보여줄 최근 기록 수
OUT_DIR = Path(__file__).resolve().parent
KST = dt.timezone(dt.timedelta(hours=9))
# ===============================================================================

TQQQ = "TQQQ 100%"
MIX = "QQQ 50% / QLD 50%"
CASH = "현금 100%"


def judge(close, sma, vix10, high52, prev_state):
    """하루치 판정. (시장 상태, 목표 비중)을 돌려준다."""
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


def fetch():
    import yfinance as yf

    last_err = None
    for i in range(4):
        try:
            ndx = yf.Ticker("^NDX").history(period="3y", interval="1d", auto_adjust=False)
            vix = yf.Ticker("^VIX").history(period="3y", interval="1d", auto_adjust=False)
            if len(ndx) > MA_DAYS + 30 and len(vix) > VIX_DAYS + 5:
                return ndx, vix
            last_err = f"데이터 부족 (NDX {len(ndx)}행, VIX {len(vix)}행)"
        except Exception as e:  # 네트워크/차단 등
            last_err = str(e)
        print(f"재시도 {i + 1}: {last_err}", file=sys.stderr)
        time.sleep(15 * (i + 1))
    sys.exit(f"데이터 수신 실패: {last_err}")


def build_frame(ndx, vix):
    import pandas as pd

    def clean(s):
        s = s.copy()
        s.index = pd.to_datetime(s.index.tz_localize(None) if s.index.tz is not None else s.index).normalize()
        return s[~s.index.duplicated(keep="last")]

    ndx, vix = clean(ndx), clean(vix)
    df = pd.DataFrame({"close": ndx["Close"], "high": ndx["High"]})
    df["vix"] = vix["Close"]
    df = df.dropna(subset=["close", "vix"])
    df["high"] = df["high"].fillna(df["close"])
    df["sma"] = df["close"].rolling(MA_DAYS).mean()
    df["vix10"] = df["vix"].rolling(VIX_DAYS).mean()
    df["hi52"] = df["high"].rolling(HIGH_WINDOW).max()
    return df


def compute(df):
    """START_DATE 이후 매일을 순서대로 판정해 기록 리스트를 만든다."""
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


def fmt(x, n=2):
    return f"{x:,.{n}f}"


def render(rows):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if not rows:
        body = "<p>아직 기록이 없습니다.</p>"
        last = None
    else:
        last = rows[-1]
        tone = {TQQQ: "atk", MIX: "mid", CASH: "def"}[last["target"]]
        changed = last["action"].startswith("변경")
        checks = [
            ("NDX 종가", fmt(last["close"]), ""),
            (f"{MA_DAYS}일선", fmt(last["sma"]), ""),
            ("상승 기준선 (×%.2f)" % UP_BAND, fmt(last["up_line"]), "이 선 위 = 상승"),
            ("하락 기준선 (×%.2f)" % DOWN_BAND, fmt(last["down_line"]), "이 선 아래 = 하락"),
            (f"VIX {VIX_DAYS}일 평균", fmt(last["vix10"]),
             f"상승일 때 {VIX_UP_LIMIT} 미만 / 하락일 때 {VIX_DOWN_LIMIT} 미만"),
            ("52주 고점", fmt(last["hi52"]), ""),
            ("고점 대비 낙폭", fmt(last["dd"]) + "%", f"{DD_LIMIT * 100:.0f}% 이하여야 공격"),
        ]
        check_html = "".join(
            f"<tr><th>{html.escape(a)}</th><td>{b}</td><td class='note'>{html.escape(c)}</td></tr>"
            for a, b, c in checks
        )
        hist_html = "".join(
            f"<tr class='{'chg' if r['action'].startswith('변경') else ''}'>"
            f"<td>{r['date'][5:]}</td><td>{fmt(r['close'], 0)}</td><td>{r['state']}</td>"
            f"<td>{html.escape(r['target'])}</td></tr>"
            for r in reversed(rows[-SHOW_ROWS:])
        )
        body = f"""
<section class="hero {tone}">
  <div class="asof">{last['date']} 미국장 종가 기준</div>
  <div class="target">{html.escape(last['target'])}</div>
  <div class="sub">시장 상태 <b>{last['state']}</b> · {html.escape(last['action'])}</div>
  <div class="do">{'오늘 해야 할 일: 목표 비중으로 리밸런싱' if changed else '오늘 해야 할 일: 없음 (그대로 유지)'}</div>
</section>
<h2>판정에 쓴 숫자</h2>
<table class="kv">{check_html}</table>
<h2>최근 기록</h2>
<div class="scroll"><table class="hist">
<thead><tr><th>날짜</th><th>NDX</th><th>상태</th><th>목표 비중 (색칠 = 변경일)</th></tr></thead>
<tbody>{hist_html}</tbody></table></div>
"""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>트라이팟 일별 판정</title>
<style>
:root{{--bg:#f6f7f9;--fg:#15181d;--mut:#5d6672;--card:#fff;--line:#e1e5ea;
--atk:#0d7a4f;--atkbg:#e3f4ec;--mid:#8a5a00;--midbg:#fbf0d9;--def:#a3262a;--defbg:#fbe6e6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1216;--fg:#e8ebef;--mut:#9aa4b0;--card:#171b21;--line:#2a313a;
--atk:#53d19b;--atkbg:#12281f;--mid:#f0b94d;--midbg:#2b2210;--def:#ff8a8d;--defbg:#2e1516}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}}
main{{max-width:640px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:18px;margin:0 0 14px}} h2{{font-size:15px;margin:26px 0 8px;color:var(--mut);font-weight:600}}
.hero{{border-radius:14px;padding:20px 18px}}
.hero.atk{{background:var(--atkbg);color:var(--atk)}} .hero.mid{{background:var(--midbg);color:var(--mid)}} .hero.def{{background:var(--defbg);color:var(--def)}}
.asof{{font-size:13px;opacity:.85}} .target{{font-size:clamp(22px,7.2vw,32px);font-weight:800;margin:4px 0;letter-spacing:-.5px;white-space:nowrap}}
.sub{{font-size:15px}} .do{{margin-top:10px;font-weight:700;font-size:15px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}}
th,td{{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line)}}
.kv th{{font-weight:500;color:var(--mut);width:42%}} .kv td{{font-variant-numeric:tabular-nums;font-weight:600}} .note{{color:var(--mut);font-size:12px;font-weight:400!important}}
.hist thead th{{font-size:12px;color:var(--mut);font-weight:600}} .hist td{{font-variant-numeric:tabular-nums;white-space:nowrap}}
tr.chg td{{font-weight:700;background:var(--midbg)}}
.scroll{{overflow-x:auto}} tr:last-child td,tr:last-child th{{border-bottom:0}}
footer{{margin-top:28px;font-size:12px;color:var(--mut)}}
</style></head><body><main>
<h1>트라이팟 일별 판정</h1>
{body}
<footer>
생성 {now} · 자료: Yahoo Finance (^NDX, ^VIX)<br>
기록 시작 {START_DATE}. 개인이 정한 규칙을 기계적으로 계산한 결과이며 투자 권유가 아닙니다. 매매 판단과 책임은 본인에게 있습니다.
</footer></main></body></html>"""


def main():
    ndx, vix = fetch()
    rows = compute(build_frame(ndx, vix))
    (OUT_DIR / "data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(render(rows), encoding="utf-8")
    if rows:
        r = rows[-1]
        print(f"{r['date']} {r['state']} → {r['target']} ({r['action']})")


if __name__ == "__main__":
    main()
