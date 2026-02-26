"""
notifiers/chart_generator.py
[v5.0 Phase 5 신규] 차트 이미지 생성 전담 모듈

[역할]
- 종목별 주가 차트 (캔들 + 거래량 + 이동평균선) → BytesIO PNG
- 주간 성과 차트 (트리거별 승률 바차트 + 수익률 트렌드) → BytesIO PNG
- 수익률 트렌드 차트 → BytesIO PNG

[아키텍처 규칙]
- pykrx는 마감 후 확정치 전용 (장중 호출 금지 — 규칙 #10)
- 생성 실패 시 None 반환 (비치명적) — 호출처에서 None 체크 필수
- 텔레그램 발송 로직 없음 (포맷 생성만) — telegram_bot.py 에서 전송

[의존성]
chart_generator → pykrx (일별 OHLCV)
chart_generator ← notifiers/telegram_bot.py (이미지 발송)
chart_generator ← reports/weekly_report.py (주간 차트)

[수정이력]
- v5.0: Phase 5 신규 (종목 차트 + 주간 성과 차트)
"""

from io import BytesIO
from utils.logger import logger

import matplotlib
matplotlib.use("Agg")   # 헤드리스 환경 (서버/Railway) — GUI 없이 PNG 생성
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib import rcParams

# 한글 폰트: 나눔고딕 없으면 기본 폰트로 폴백 (Railway에서 한글 깨짐 방지)
try:
    rcParams["font.family"] = "NanumGothic"
    rcParams["axes.unicode_minus"] = False
except Exception:
    pass


# ══════════════════════════════════════════════════════════════
# 종목별 주가 차트
# ══════════════════════════════════════════════════════════════

def generate_stock_chart(
    ticker: str,
    name: str,
    days: int = 30,
) -> BytesIO | None:
    """
    [v5.0] 종목별 주가 차트 생성 (캔들스틱 + 거래량 + MA5/MA20)

    Args:
        ticker: 종목코드 (예: "005930")
        name:   종목명 (예: "삼성전자")
        days:   조회 일수 (기본 30일)

    Returns:
        BytesIO (PNG) — 실패 시 None
    """
    try:
        from pykrx import stock
        from utils.date_utils import get_today, get_prev_trading_day
        import pandas as pd
        from datetime import timedelta

        today = get_today()
        start_date = (today - timedelta(days=days * 2)).strftime("%Y%m%d")  # 주말 여유
        end_date   = today.strftime("%Y%m%d")

        df = stock.get_market_ohlcv_by_date(start_date, end_date, ticker)
        if df is None or df.empty:
            logger.debug(f"[chart] {name}({ticker}) OHLCV 없음")
            return None

        # 최근 days 영업일만
        df = df.tail(days)
        if len(df) < 5:
            return None

        df["MA5"]  = df["종가"].rolling(5).mean()
        df["MA20"] = df["종가"].rolling(20).mean()

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(10, 6),
            gridspec_kw={"height_ratios": [3, 1]},
            facecolor="#1a1a2e",
        )
        fig.subplots_adjust(hspace=0.05)

        # ── 캔들스틱 ─────────────────────────────────────────
        ax1.set_facecolor("#1a1a2e")
        ax1.tick_params(colors="white", labelsize=8)
        ax1.spines[:].set_color("#333355")

        up   = df[df["종가"] >= df["시가"]]
        down = df[df["종가"] <  df["시가"]]

        # 실제 날짜 인덱스 → 정수 x축 변환
        x = range(len(df))
        dates = df.index.strftime("%m/%d").tolist()

        for i, (_, row) in enumerate(df.iterrows()):
            color = "#ff4444" if row["종가"] >= row["시가"] else "#4488ff"
            # 심지
            ax1.plot([i, i], [row["저가"], row["고가"]], color=color, linewidth=1)
            # 몸통
            h = abs(row["종가"] - row["시가"]) or (row["고가"] - row["저가"]) * 0.01
            bottom = min(row["종가"], row["시가"])
            ax1.bar(i, h, bottom=bottom, color=color, width=0.6, alpha=0.9)

        # 이동평균선
        x_arr = list(range(len(df)))
        ax1.plot(x_arr, df["MA5"].values,  color="#ffd700", linewidth=1.2, label="MA5",  alpha=0.9)
        ax1.plot(x_arr, df["MA20"].values, color="#ff8c00", linewidth=1.2, label="MA20", alpha=0.7)
        ax1.legend(loc="upper left", fontsize=8, facecolor="#1a1a2e", labelcolor="white", framealpha=0.7)

        # x축 레이블 (5개만 표시)
        step = max(1, len(df) // 5)
        ax1.set_xticks([i for i in x_arr if i % step == 0])
        ax1.set_xticklabels([dates[i] for i in x_arr if i % step == 0], color="white", fontsize=7)
        ax1.set_xlim(-0.5, len(df) - 0.5)

        # 최고/최저 표시
        max_idx = df["고가"].argmax()
        min_idx = df["저가"].argmin()
        ax1.annotate(
            f"{df['고가'].max():,}",
            xy=(max_idx, df["고가"].max()),
            xytext=(max_idx + 0.5, df["고가"].max() * 1.002),
            color="#ff4444", fontsize=7, alpha=0.9,
        )

        ax1.set_title(f"{name}  ({ticker})  최근 {len(df)}일", color="white", fontsize=11, pad=8)
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax1.yaxis.label.set_color("white")

        # ── 거래량 ────────────────────────────────────────────
        ax2.set_facecolor("#1a1a2e")
        ax2.tick_params(colors="white", labelsize=7)
        ax2.spines[:].set_color("#333355")

        vol_colors = [
            "#ff4444" if df["종가"].iloc[i] >= df["시가"].iloc[i] else "#4488ff"
            for i in range(len(df))
        ]
        ax2.bar(x_arr, df["거래량"].values, color=vol_colors, width=0.6, alpha=0.8)
        ax2.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda v, _: f"{int(v/10000)}만" if v >= 10000 else str(int(v)))
        )
        ax2.set_xticks([])
        ax2.set_xlim(-0.5, len(df) - 0.5)

        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#1a1a2e")
        plt.close(fig)
        buf.seek(0)
        return buf

    except Exception as e:
        logger.warning(f"[chart] 종목 차트 생성 실패 ({ticker}): {e}")
        return None


# ══════════════════════════════════════════════════════════════
# 주간 성과 차트
# ══════════════════════════════════════════════════════════════

def generate_weekly_performance_chart(stats: dict) -> BytesIO | None:
    """
    [v5.0] 주간 성과 차트 생성
    - 좌: 트리거별 승률 수평 바차트
    - 우: 수익률 트렌드 라인차트 (top/miss picks 최근 1주)

    Args:
        stats: performance_tracker.get_weekly_stats() 반환값

    Returns:
        BytesIO (PNG) — 실패 시 None
    """
    try:
        trigger_stats = stats.get("trigger_stats", [])
        top_picks     = stats.get("top_picks", [])
        miss_picks    = stats.get("miss_picks", [])

        if not trigger_stats and not top_picks:
            return None

        source_label = {
            "volume":    "거래량급증",
            "rate":      "등락률포착",
            "websocket": "워치리스트",
            "gap_up":    "갭상승",
        }

        fig, axes = plt.subplots(1, 2, figsize=(12, 5), facecolor="#1a1a2e")
        fig.suptitle("주간 성과 리포트", color="white", fontsize=13, y=1.02)

        # ── 왼쪽: 트리거별 승률 ─────────────────────────────
        ax1 = axes[0]
        ax1.set_facecolor("#1a1a2e")
        ax1.tick_params(colors="white", labelsize=9)
        ax1.spines[:].set_color("#333355")
        ax1.set_title("트리거별 7일 승률", color="white", fontsize=10)

        if trigger_stats:
            labels     = [source_label.get(t["trigger_type"], t["trigger_type"]) for t in trigger_stats]
            win_rates  = [t.get("win_rate_7d", 0) for t in trigger_stats]
            counts     = [t.get("tracked_7d", 0) for t in trigger_stats]
            bar_colors = ["#ff4444" if w >= 60 else "#ffd700" if w >= 40 else "#4488ff" for w in win_rates]

            y_pos = range(len(labels))
            bars = ax1.barh(y_pos, win_rates, color=bar_colors, height=0.5, alpha=0.85)
            ax1.set_yticks(list(y_pos))
            ax1.set_yticklabels(labels, color="white", fontsize=9)
            ax1.set_xlabel("승률 (%)", color="white", fontsize=9)
            ax1.set_xlim(0, 100)
            ax1.axvline(50, color="#aaaaaa", linewidth=0.8, linestyle="--", alpha=0.6)

            for i, (bar, wr, n) in enumerate(zip(bars, win_rates, counts)):
                ax1.text(
                    min(wr + 2, 90), i,
                    f"{wr:.0f}%  (n={n})",
                    va="center", color="white", fontsize=8,
                )
        else:
            ax1.text(0.5, 0.5, "데이터 없음", ha="center", va="center",
                     color="#888888", transform=ax1.transAxes)

        # ── 오른쪽: 수익률 상위/하위 비교 ───────────────────
        ax2 = axes[1]
        ax2.set_facecolor("#1a1a2e")
        ax2.tick_params(colors="white", labelsize=8)
        ax2.spines[:].set_color("#333355")
        ax2.set_title("7일 수익률 상위/하위", color="white", fontsize=10)
        ax2.axhline(0, color="#aaaaaa", linewidth=0.8, linestyle="--", alpha=0.5)

        combined = []
        for p in top_picks[:3]:
            combined.append((p.get("name", p.get("ticker", "?")), p.get("return_7d", 0), True))
        for p in miss_picks[:3]:
            if p.get("return_7d", 0) < 0:
                combined.append((p.get("name", p.get("ticker", "?")), p.get("return_7d", 0), False))

        if combined:
            names   = [c[0][:6] for c in combined]   # 이름 6자 자름
            returns = [c[1] for c in combined]
            colors  = ["#ff4444" if c[2] else "#4488ff" for c in combined]

            ax2.bar(range(len(combined)), returns, color=colors, alpha=0.85, width=0.6)
            ax2.set_xticks(range(len(combined)))
            ax2.set_xticklabels(names, color="white", fontsize=8, rotation=20, ha="right")
            ax2.set_ylabel("수익률 (%)", color="white", fontsize=9)

            for i, r in enumerate(returns):
                sign = "+" if r >= 0 else ""
                ax2.text(i, r + (0.3 if r >= 0 else -0.8),
                         f"{sign}{r:.1f}%", ha="center", color="white", fontsize=8)
        else:
            ax2.text(0.5, 0.5, "데이터 없음", ha="center", va="center",
                     color="#888888", transform=ax2.transAxes)

        red_patch  = mpatches.Patch(color="#ff4444", label="수익")
        blue_patch = mpatches.Patch(color="#4488ff", label="손실")
        ax2.legend(handles=[red_patch, blue_patch], facecolor="#1a1a2e",
                   labelcolor="white", fontsize=8, framealpha=0.7)

        plt.tight_layout()
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#1a1a2e")
        plt.close(fig)
        buf.seek(0)
        return buf

    except Exception as e:
        logger.warning(f"[chart] 주간 성과 차트 생성 실패: {e}")
        return None
