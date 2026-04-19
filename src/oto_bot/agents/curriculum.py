"""CurriculumLoader — Türkçe/İngilizce açık kaynak trader derslerini ingest eder.

Kaynaklar:
    - borsaninizinden.com : Türkçe, İbrahim Babadağı (TA + price action)
    - babypips.com        : İngilizce, "School of Pipsology" (forex/swing odaklı)
    - investopedia.com    : İngilizce, kategorize trading makaleleri
    - litefinance.org     : İngilizce, price action ve beginner içerik

İki mod:
    - rule_based : keyword + pattern extraction → kısa lesson
    - llm_enhanced : Claude CLI → 3 yapılandırılmış insight

Her ders → LearningJournal'a `author:Talos Curriculum` + `source:curriculum:<domain>`
tag'leriyle yazılır. Nova hipotez üretirken / Bayesian slot iyileştirirken
retriever bu dersleri context olarak çeker.

Faz 7 — daimi mode:
    EducatorLoop bu sınıfı arka plan thread'inde belirli aralıklarla
    tetikler. State file (`artifacts/curriculum_state.json`) hangi URL'in
    son ne zaman ingest edildiğini tutar; rotated index ile aynı sayfa
    tekrar tekrar çekilmez. Dedupe LearningJournal.save'de (Faz 5).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
try:
    from bs4 import BeautifulSoup  # beautifulsoup4 zaten kurulu (streamlit deps)
except ImportError:
    BeautifulSoup = None  # type: ignore

from oto_bot.agents.proposals import Proposal, ProposalQueue
from oto_bot.core.models import Lesson
from oto_bot.memory.journal import LearningJournal


# State file: hangi URL hangi zamanda işlendi, hangi cursor'dayız
_STATE_PATH = Path("artifacts/curriculum_state.json")


def _load_state() -> dict[str, Any]:
    """Curriculum state'i diskten yükle (yoksa boş dict)."""
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    """State'i atomik olarak diske yaz (os.replace pattern)."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Borsanın İzinden — curated URL listesi
# ---------------------------------------------------------------------------

BORSANINIZINDEN_DERSLER: list[tuple[str, str, str]] = [
    # (url, title, tag_hint)
    ("https://borsaninizinden.com/mum-grafiklerine-giris-ders-1/",    "Mum Grafiklerine Giriş",         "candlestick"),
    ("https://borsaninizinden.com/doji-mumu/",                       "DOJI Mumu Formasyonu",           "candlestick:doji"),
    ("https://borsaninizinden.com/hareketli-ortalamalar-ders-1/",    "Hareketli Ortalamalar",          "indicator:ma"),
    ("https://borsaninizinden.com/indikator-nedir/",                 "İndikatör Nedir?",               "indicator"),
    ("https://borsaninizinden.com/price-action-nedir/",              "Price Action Nedir?",            "price_action"),
    ("https://borsaninizinden.com/rsi-nedir/",                       "RSI Nedir?",                     "indicator:rsi"),
    ("https://borsaninizinden.com/swing-trader-nedir/",              "Swing Trader Nedir?",            "strategy:swing"),
    ("https://borsaninizinden.com/omuz-bas-omuz-obo-formasyonu/",    "Omuz Baş Omuz Formasyonu",       "pattern:head_shoulders"),
    ("https://borsaninizinden.com/fincan-kulp-formasyonu-nedir/",    "Fincan Kulp Formasyonu",         "pattern:cup_handle"),
    ("https://borsaninizinden.com/bayrak-ve-flama-formasyonu-nedir/","Bayrak ve Flama Formasyonu",     "pattern:flag"),
    ("https://borsaninizinden.com/ikili-dip-ve-ikili-tepe-formasyonu/","İkili Dip/Tepe Formasyonu",    "pattern:double"),
    ("https://borsaninizinden.com/margin-call-nedir/",               "Margin Call Nedir?",             "risk:margin"),
    ("https://borsaninizinden.com/stop-loss-nedir-nasil-yapilir/",   "Stop Loss Nasıl Yapılır?",       "risk:stop_loss"),
    ("https://borsaninizinden.com/kaldiracli-islem-nedir/",          "Kaldıraçlı İşlem",               "risk:leverage"),
]


# Babypips — School of Pipsology — forex/swing odaklı 100+ ders.
# Her sayfa müstakil bir lesson. Sıra önemli (giriş -> ileri).
BABYPIPS_DERSLER: list[tuple[str, str, str]] = [
    ("https://www.babypips.com/learn/forex/what-is-forex",                  "What Is Forex?",                       "forex:basics"),
    ("https://www.babypips.com/learn/forex/what-is-traded-in-forex",        "What Is Traded in Forex?",             "forex:pairs"),
    ("https://www.babypips.com/learn/forex/buying-and-selling-currency-pairs","Buying and Selling Currency Pairs",  "forex:execution"),
    ("https://www.babypips.com/learn/forex/types-of-forex-orders",          "Types of Forex Orders",                "forex:orders"),
    ("https://www.babypips.com/learn/forex/how-to-make-money-trading-forex","How to Make Money Trading Forex",      "forex:edge"),
    ("https://www.babypips.com/learn/forex/three-types-of-analysis",        "Three Types of Analysis",              "analysis:framework"),
    ("https://www.babypips.com/learn/forex/japanese-candle-sticks",         "Japanese Candlesticks",                "candlestick"),
    ("https://www.babypips.com/learn/forex/single-candlestick-patterns",    "Single Candlestick Patterns",          "candlestick:single"),
    ("https://www.babypips.com/learn/forex/dual-candlestick-patterns",      "Dual Candlestick Patterns",            "candlestick:dual"),
    ("https://www.babypips.com/learn/forex/triple-candlestick-patterns",    "Triple Candlestick Patterns",          "candlestick:triple"),
    ("https://www.babypips.com/learn/forex/support-and-resistance-levels",  "Support and Resistance Levels",        "price_action:sr"),
    ("https://www.babypips.com/learn/forex/trend-lines",                    "Trend Lines",                          "price_action:trend"),
    ("https://www.babypips.com/learn/forex/channels",                       "Channels",                             "price_action:channel"),
    ("https://www.babypips.com/learn/forex/moving-average",                 "Moving Average",                       "indicator:ma"),
    ("https://www.babypips.com/learn/forex/bollinger-bands",                "Bollinger Bands",                      "indicator:bb"),
    ("https://www.babypips.com/learn/forex/macd",                           "MACD",                                 "indicator:macd"),
    ("https://www.babypips.com/learn/forex/parabolic-sar",                  "Parabolic SAR",                        "indicator:sar"),
    ("https://www.babypips.com/learn/forex/stochastic",                     "Stochastic",                           "indicator:stoch"),
    ("https://www.babypips.com/learn/forex/relative-strength-index",        "Relative Strength Index",              "indicator:rsi"),
    ("https://www.babypips.com/learn/forex/average-directional-index",      "Average Directional Index",            "indicator:adx"),
    ("https://www.babypips.com/learn/forex/ichimoku-kinko-hyo",             "Ichimoku Kinko Hyo",                   "indicator:ichimoku"),
    ("https://www.babypips.com/learn/forex/fibonacci",                      "Fibonacci",                            "indicator:fibo"),
    ("https://www.babypips.com/learn/forex/multiple-time-frame-analysis",   "Multiple Time Frame Analysis",         "mtf:confluence"),
    ("https://www.babypips.com/learn/forex/elliott-wave-theory",            "Elliott Wave Theory",                  "pattern:elliott"),
    ("https://www.babypips.com/learn/forex/divergences",                    "Divergences",                          "indicator:divergence"),
    ("https://www.babypips.com/learn/forex/chart-patterns",                 "Chart Patterns",                       "pattern:chart"),
    ("https://www.babypips.com/learn/forex/pivot-points",                   "Pivot Points",                         "indicator:pivot"),
    ("https://www.babypips.com/learn/forex/risk-management",                "Risk Management",                      "risk:basics"),
    ("https://www.babypips.com/learn/forex/position-sizing",                "Position Sizing",                      "risk:sizing"),
    ("https://www.babypips.com/learn/forex/breakout-trading",               "Breakout Trading",                     "strategy:breakout"),
    ("https://www.babypips.com/learn/forex/range-trading",                  "Range Trading",                        "strategy:range"),
    ("https://www.babypips.com/learn/forex/trend-trading",                  "Trend Trading",                        "strategy:trend"),
    ("https://www.babypips.com/learn/forex/swing-trading",                  "Swing Trading",                        "strategy:swing"),
    ("https://www.babypips.com/learn/forex/scalping",                       "Scalping",                             "strategy:scalp"),
]


# Investopedia — kategorize trading makaleleri (kısa, tek-kavram).
INVESTOPEDIA_DERSLER: list[tuple[str, str, str]] = [
    ("https://www.investopedia.com/terms/m/movingaverage.asp",      "Moving Average",                  "indicator:ma"),
    ("https://www.investopedia.com/terms/r/rsi.asp",                "Relative Strength Index",          "indicator:rsi"),
    ("https://www.investopedia.com/terms/m/macd.asp",               "MACD",                             "indicator:macd"),
    ("https://www.investopedia.com/terms/b/bollingerbands.asp",     "Bollinger Bands",                  "indicator:bb"),
    ("https://www.investopedia.com/terms/s/stochasticoscillator.asp","Stochastic Oscillator",           "indicator:stoch"),
    ("https://www.investopedia.com/terms/a/atr.asp",                "Average True Range",               "indicator:atr"),
    ("https://www.investopedia.com/terms/v/vwap.asp",               "Volume Weighted Average Price",    "indicator:vwap"),
    ("https://www.investopedia.com/terms/m/meanreversion.asp",      "Mean Reversion",                   "strategy:mean_reversion"),
    ("https://www.investopedia.com/terms/m/momentum_investing.asp", "Momentum Investing",               "strategy:momentum"),
    ("https://www.investopedia.com/terms/d/daytrader.asp",          "Day Trader",                       "strategy:day"),
    ("https://www.investopedia.com/terms/s/swingtrading.asp",       "Swing Trading",                    "strategy:swing"),
    ("https://www.investopedia.com/terms/s/scalping.asp",           "Scalping",                         "strategy:scalp"),
    ("https://www.investopedia.com/terms/s/stop-lossorder.asp",     "Stop-Loss Order",                  "risk:stop_loss"),
    ("https://www.investopedia.com/terms/p/positionsizing.asp",     "Position Sizing",                  "risk:sizing"),
    ("https://www.investopedia.com/terms/k/kellycriterion.asp",     "Kelly Criterion",                  "risk:kelly"),
    ("https://www.investopedia.com/terms/d/drawdown.asp",           "Drawdown",                         "risk:drawdown"),
    ("https://www.investopedia.com/terms/s/sharperatio.asp",        "Sharpe Ratio",                     "metric:sharpe"),
    ("https://www.investopedia.com/terms/s/sortinoratio.asp",       "Sortino Ratio",                    "metric:sortino"),
    ("https://www.investopedia.com/terms/c/candlestick.asp",        "Candlestick",                      "candlestick"),
    ("https://www.investopedia.com/terms/d/doji.asp",               "Doji",                             "candlestick:doji"),
    ("https://www.investopedia.com/terms/h/headandshoulderspattern.asp","Head and Shoulders Pattern",   "pattern:head_shoulders"),
    ("https://www.investopedia.com/terms/d/doubletop.asp",          "Double Top",                       "pattern:double_top"),
    ("https://www.investopedia.com/terms/c/cupandhandle.asp",       "Cup and Handle",                   "pattern:cup_handle"),
    ("https://www.investopedia.com/terms/f/fibonaccilines.asp",     "Fibonacci Lines",                  "indicator:fibo"),
    ("https://www.investopedia.com/terms/p/pivotpoint.asp",         "Pivot Point",                      "indicator:pivot"),
    ("https://www.investopedia.com/terms/i/ichimoku-cloud.asp",     "Ichimoku Cloud",                   "indicator:ichimoku"),
    ("https://www.investopedia.com/terms/b/backtesting.asp",        "Backtesting",                      "framework:backtest"),
    ("https://www.investopedia.com/terms/w/walk-forward-analysis.asp","Walk-Forward Analysis",          "framework:wf"),
    ("https://www.investopedia.com/terms/m/montecarlosimulation.asp","Monte Carlo Simulation",          "framework:mc"),
    ("https://www.investopedia.com/terms/o/optimization.asp",       "Optimization",                     "framework:opt"),
]


# LiteFinance — price action / beginner blog.
LITEFINANCE_DERSLER: list[tuple[str, str, str]] = [
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/", "Best Forex Strategies",                "strategy:overview"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/price-action-trading/", "Price Action Trading", "price_action"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/swing-trading-strategies/", "Swing Trading Strategies", "strategy:swing"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/scalping-strategy/", "Scalping Strategy",      "strategy:scalp"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/breakout-trading/", "Breakout Trading",        "strategy:breakout"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/trend-trading/", "Trend Trading",              "strategy:trend"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-strategies/range-trading/", "Range Trading",              "strategy:range"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-indicators/", "Best Forex Indicators",                   "indicator:overview"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-indicators/best-trend-indicators/", "Best Trend Indicators","indicator:trend"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-indicators/best-momentum-indicators/", "Momentum Indicators","indicator:momentum"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-indicators/best-volume-indicators/", "Volume Indicators", "indicator:volume"),
    ("https://www.litefinance.org/blog/for-beginners/best-forex-indicators/best-volatility-indicators/", "Volatility Indicators","indicator:volatility"),
    ("https://www.litefinance.org/blog/for-beginners/risk-management/", "Risk Management",                                "risk:basics"),
    ("https://www.litefinance.org/blog/for-beginners/how-to-trade-forex/", "How to Trade Forex",                          "forex:basics"),
    ("https://www.litefinance.org/blog/for-beginners/how-to-trade-forex/how-to-read-japanese-candlesticks/", "How to Read Japanese Candlesticks","candlestick"),
]


# ---------------------------------------------------------------------------
# Rule-based Türkçe TA anahtar kelime çıkarımı
# ---------------------------------------------------------------------------

TR_TA_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, türkçe fayda, strategy family hint)
    (r"(?i)(doji|hammer|shooting star|ters çekiç|çekiç)",
     "Mum formasyonu: reversal sinyali olarak doji/çekiç/ters çekiç kullanılır",
     "day"),
    (r"(?i)(hareketli ortalama|ema|sma|golden cross|death cross)",
     "Hareketli ortalama crossover trend teyidi — golden cross uzun, death cross kısa",
     "swing"),
    (r"(?i)(rsi|relative strength|aşırı alım|aşırı satım)",
     "RSI aşırı alım/satım (>70 / <30) reversal adayı; trend içinde RSI orta bant (40-60) retracement",
     "day"),
    (r"(?i)(destek|direnç|support|resistance|pivot)",
     "Destek/direnç seviyeleri: fiyat bu seviyelerde tepki verir, breakout sinyal oluşturur",
     "swing"),
    (r"(?i)(price action|fiyat hareketi)",
     "Price action: indikatör yerine saf fiyat okuma — mum boyutları, kuyruk uzunluğu, yapı kırılımı",
     "day"),
    (r"(?i)(stop ?loss|kayıp ?stopu|zararı durdur)",
     "Stop loss: her işleme %1-2 risk; kritik destek altına kuyruk + ATR × 1.5 standart",
     "*"),
    (r"(?i)(kaldıraç|leverage|marj|margin)",
     "Kaldıraçlı işlem: yüksek kaldıraç = yüksek likidasyon riski; scalp'te 2-3x maksimum öneri",
     "scalp"),
    (r"(?i)(omuz.?baş.?omuz|obo|head.?shoulder)",
     "OBO formasyonu: uzun trend sonu reversal; boyun çizgisi kırılımı teyit",
     "swing"),
    (r"(?i)(fincan.?kulp|cup.?handle)",
     "Fincan-kulp: bullish devam formasyonu; kulp kırılımı long giriş",
     "swing"),
    (r"(?i)(bayrak|flama|flag|pennant)",
     "Bayrak/flama: trendde kısa konsolidasyon sonrası devam; breakout long/short sinyal",
     "day"),
    (r"(?i)(ikili dip|ikili tepe|double bottom|double top)",
     "İkili dip/tepe: reversal formasyon, teyit için yüksek hacim şart",
     "swing"),
    (r"(?i)(hacim|volume|hacim profili|vwap)",
     "Hacim teyidi: breakout ancak yüksek hacimle güvenilir; VWAP mean-reversion için referans",
     "*"),
    (r"(?i)(bollinger|bant|band)",
     "Bollinger bantları: üst/alt band ekstremleri mean-reversion; daralma → breakout",
     "scalp"),
    (r"(?i)(fibonacci|fibo|retracement)",
     "Fibonacci retracement: 38.2% / 50% / 61.8% önemli geri çekilme seviyeleri",
     "swing"),
    (r"(?i)(risk yönetimi|risk management|pozisyon büyüklüğü)",
     "Risk yönetimi: tek trade'de sermayenin %1-2'si; ardışık 3 kayıp sonrası pozisyon boyutunu yarıla",
     "*"),
    # İngilizce ek pattern'lar (Babypips / Investopedia / LiteFinance)
    (r"(?i)(multi.?time.?frame|mtf|higher.?timeframe)",
     "Multi-timeframe (MTF) confluence: üst-TF trend yönü filtre, alt-TF entry — sinyal kalitesi artar",
     "swing"),
    (r"(?i)(breakout|break.?out|range.?expansion)",
     "Breakout: konsolidasyon sonrası volatilite genişlemesi; hacim teyidi şart, fake-out riski var",
     "day"),
    (r"(?i)(mean.?reversion|reversion to mean|overextended)",
     "Mean reversion: aşırı uzayan fiyat geri çeker; range piyasada güçlü, trendde tehlikeli",
     "scalp"),
    (r"(?i)(momentum|trend.?follow|trend.?riding)",
     "Momentum/trend-follow: kazananları bırak, kaybedenleri kes; pyramiding ile pozisyon büyüt",
     "swing"),
    (r"(?i)(stop.?loss|protective stop|trailing.?stop)",
     "Stop loss: önceden tanımlı maksimum kayıp; ATR×1.5 veya yapısal seviye altı standart",
     "*"),
    (r"(?i)(position.?siz|kelly|fixed.?fractional)",
     "Position sizing: Kelly criterion (fractional) veya fixed-%; sermayenin %1-2'si trade başına",
     "*"),
    (r"(?i)(divergence|hidden divergence|regular divergence)",
     "Divergence: fiyat ve momentum (RSI/MACD) zıt yönde — reversal erken sinyali",
     "swing"),
    (r"(?i)(elliott|wave.?count|impulse.?wave|corrective.?wave)",
     "Elliott wave: 5 impulse + 3 corrective; sayım sübjektif, tek başına entry yetmez",
     "swing"),
    (r"(?i)(ichimoku|kumo|tenkan|kijun)",
     "Ichimoku: bulut (kumo) trend filtre; tenkan/kijun cross sinyal; geç ama güçlü",
     "swing"),
    (r"(?i)(supply.?(zone|area)|demand.?(zone|area)|order.?block)",
     "Supply/demand zones: kurumsal emir bölgeleri; fiyat bu zone'lara dönerse reaksiyon olası",
     "swing"),
    (r"(?i)(macd|signal line cross|histogram)",
     "MACD: signal cross momentum; histogram divergence reversal; 12/26/9 default ama optimize edilebilir",
     "day"),
    (r"(?i)(stochastic|%k|%d|stoch crossover)",
     "Stochastic: %K/%D crossover; 80 üstü aşırı alım, 20 altı aşırı satım — range'de daha güvenilir",
     "scalp"),
    (r"(?i)(adx|directional.?movement|trend.?strength)",
     "ADX: trend gücü ölçer; >25 trend, <20 range — rejim filtresi olarak kullan",
     "*"),
    (r"(?i)(parabolic.?sar|psar)",
     "Parabolic SAR: trailing stop indicator; trend yönü flip — geç sinyaller verir",
     "swing"),
    (r"(?i)(sharpe|sortino|calmar|risk.?adjusted)",
     "Risk-adjusted metrics: Sharpe (vol), Sortino (downside vol), Calmar (DD) — promotion gate",
     "*"),
    (r"(?i)(walk.?forward|out.?of.?sample|oos|in.?sample)",
     "Walk-forward / OOS validation: train/test split şart; in-sample overfit yakalama",
     "*"),
    (r"(?i)(monte.?carlo|bootstrap|equity.?curve simulation)",
     "Monte Carlo: equity curve perturbation, max DD distribution — robustness ölçümü",
     "*"),
    (r"(?i)(drawdown|max.?dd|equity curve)",
     "Drawdown: peak-trough sermaye düşüşü; max DD %20 kritik eşik, recovery time önemli",
     "*"),
]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


@dataclass
class ArticleContent:
    url: str
    title: str
    body: str
    tag_hint: str


class TalosCurriculumLoader:
    """Açık kaynak trader derslerini çeker, LearningJournal'a yazar."""

    def __init__(
        self,
        journal: LearningJournal | None = None,
        queue: ProposalQueue | None = None,
        use_llm: bool = False,
        user_agent: str = "oto-bot-talos-curriculum-loader",
    ) -> None:
        self.journal = journal or LearningJournal()
        self.queue = queue or ProposalQueue()
        self.use_llm = use_llm
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

    # ------------------------------------------------------------------

    def fetch_article(self, url: str, title: str, tag_hint: str) -> ArticleContent | None:
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            r.raise_for_status()
        except Exception:
            return None
        html = r.text
        if BeautifulSoup is None:
            # Fallback: strip HTML tags crudely
            text = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            body = re.sub(r"\s+", " ", text).strip()
        else:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            article = soup.find("article") or soup.find("main") or soup
            body = article.get_text(separator=" ", strip=True)
            body = re.sub(r"\s+", " ", body)

        body = body[:20_000]
        if len(body) < 300:
            return None
        return ArticleContent(url=url, title=title, body=body, tag_hint=tag_hint)

    # ------------------------------------------------------------------

    def extract_lessons_rule_based(self, article: ArticleContent) -> list[Lesson]:
        lessons: list[Lesson] = []
        body = article.body
        seen = set()
        for pattern, benefit, strat in TR_TA_PATTERNS:
            match = re.search(pattern, body)
            if not match:
                continue
            key = benefit[:40]
            if key in seen:
                continue
            seen.add(key)
            # Context window
            start = max(0, match.start() - 150)
            end = min(len(body), match.end() + 250)
            context = body[start:end].strip()
            content = f"[{article.title}] {benefit} | Kaynak: {context[:300]}"
            lessons.append(Lesson(
                lesson_id="",
                author_agent="Talos Curriculum",
                content=content[:600],
                tags=[
                    "source:curriculum",
                    f"source_domain:{_domain(article.url)}",
                    f"tag_hint:{article.tag_hint}",
                    f"strategy:{strat}" if strat != "*" else "strategy:any",
                ],
                market="*",
                strategy_family=strat if strat != "*" else "*",
                regime="*",
                symbol="*",
                severity="info",
                evidence_experiment_id=None,
                source_cycle=0,
            ))
        return lessons

    # ------------------------------------------------------------------

    def extract_lessons_llm(self, article: ArticleContent) -> list[Lesson]:
        from oto_bot.llm import query as claude_query

        prompt = f"""Aşağıda Türkçe bir trader eğitim makalesi var.
Başlık: "{article.title}"

Bu makaleden bizim trading botumuza uygulanabilecek EN FAZLA 3 pratik ders çıkar.
Her ders 1-2 cümle, Türkçe, uygulanabilir olmalı. SADECE JSON array dön:
[{{"content":"...","strategy_family":"day|swing|scalp|*","severity":"info|medium|high"}}]

Makale (kısaltılmış):
{article.body[:6000]}
"""
        response = claude_query(prompt, timeout=120)
        if not response:
            return []

        import json as _json
        match = re.search(r"\[\s*\{.*?\}\s*\]", response, re.DOTALL)
        if not match:
            return []
        try:
            items = _json.loads(match.group(0))
        except Exception:
            return []

        lessons: list[Lesson] = []
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content", ""))[:600]
            if not content:
                continue
            strat = str(it.get("strategy_family", "*"))
            severity = str(it.get("severity", "info"))
            lessons.append(Lesson(
                lesson_id="",
                author_agent="Talos Curriculum (LLM)",
                content=f"[{article.title}] {content}",
                tags=[
                    "source:curriculum", "source:llm",
                    f"source_domain:{_domain(article.url)}",
                    f"tag_hint:{article.tag_hint}",
                    f"strategy:{strat}",
                ],
                strategy_family=strat if strat != "*" else "*",
                severity=severity if severity in ("info", "medium", "high", "critical") else "info",
            ))
        return lessons

    # ------------------------------------------------------------------
    # Source-bazlı ingest helper'ları — rotated cursor + state file
    # ------------------------------------------------------------------

    def _ingest_one_url(self, url: str, title: str, tag_hint: str) -> int:
        """Tek bir URL'i çek, lesson'ları journal'a yaz; eklenen ders sayısını dön."""
        article = self.fetch_article(url, title, tag_hint)
        if article is None:
            return 0
        added = 0
        for lesson in self.extract_lessons_rule_based(article):
            self.journal.save(lesson)
            added += 1
        if self.use_llm:
            for lesson in self.extract_lessons_llm(article):
                self.journal.save(lesson)
                added += 1
        return added

    def _rotated_ingest(
        self,
        source_key: str,
        url_list: list[tuple[str, str, str]],
        max_pages: int,
    ) -> dict[str, Any]:
        """``url_list`` üzerinde rotated cursor ile ``max_pages`` sayfa ingest et.

        State: artifacts/curriculum_state.json içinde
            {source_key: {"cursor": int, "processed": {url: ts_iso, ...}}}.
        Cursor mod operatörü ile dolaşır — liste sonuna gelince başa döner.
        """
        if not url_list:
            return {"source": source_key, "fetched": 0, "lessons": 0, "cursor": 0}
        state = _load_state()
        src = state.setdefault(source_key, {"cursor": 0, "processed": {}})
        cursor = int(src.get("cursor", 0)) % len(url_list)
        processed: dict[str, str] = src.setdefault("processed", {})

        fetched = 0
        lessons = 0
        last_url = None
        for _ in range(max(1, max_pages)):
            url, title, tag_hint = url_list[cursor]
            added = self._ingest_one_url(url, title, tag_hint)
            if added > 0:
                fetched += 1
                lessons += added
            # 200 OK olsun olmasın cursor ilerletilir — hatalı URL'i sonsuz tekrar etmemek için
            processed[url] = datetime.now(timezone.utc).isoformat()
            last_url = url
            cursor = (cursor + 1) % len(url_list)
            time.sleep(0.5)

        src["cursor"] = cursor
        src["processed"] = processed
        src["last_run"] = datetime.now(timezone.utc).isoformat()
        src["last_url"] = last_url
        state[source_key] = src
        _save_state(state)
        return {
            "source": source_key,
            "fetched": fetched,
            "lessons": lessons,
            "cursor": cursor,
            "total_urls": len(url_list),
        }

    # --- yabancı kaynak shortcut'ları --------------------------------

    def ingest_babypips(self, max_pages: int = 1) -> int:
        """Babypips'den ``max_pages`` sayfa çek. Eklenen ders sayısını dön."""
        out = self._rotated_ingest("babypips", BABYPIPS_DERSLER, max_pages)
        return int(out.get("lessons", 0))

    def ingest_investopedia(self, max_pages: int = 1) -> int:
        out = self._rotated_ingest("investopedia", INVESTOPEDIA_DERSLER, max_pages)
        return int(out.get("lessons", 0))

    def ingest_litefinance(self, max_pages: int = 1) -> int:
        out = self._rotated_ingest("litefinance", LITEFINANCE_DERSLER, max_pages)
        return int(out.get("lessons", 0))

    def ingest_borsaninizinden_step(self, max_pages: int = 1) -> int:
        """Rotated step (daimi mode için) — tüm listeyi tek seferde çekmek
        yerine her tetiklemede ``max_pages`` sayfa."""
        out = self._rotated_ingest("borsaninizinden", BORSANINIZINDEN_DERSLER, max_pages)
        return int(out.get("lessons", 0))

    def loop_step(self) -> dict[str, Any]:
        """EducatorLoop'un her tetiklemede çağırdığı tek-adım rutin.

        4 kaynaktan birer sayfa çeker — borsaninizinden + babypips +
        investopedia + litefinance. Her kaynak kendi cursor'ını ilerletir,
        aynı sayfa hemen tekrar edilmez. Hata durumunda diğer kaynakları
        bloke etmez (her biri ayrı try/except).
        """
        result: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "sources": {},
            "total_lessons": 0,
            "errors": [],
        }
        for src_key, ingest_fn in (
            ("borsaninizinden", lambda: self._rotated_ingest("borsaninizinden", BORSANINIZINDEN_DERSLER, 1)),
            ("babypips",        lambda: self._rotated_ingest("babypips",        BABYPIPS_DERSLER,        1)),
            ("investopedia",    lambda: self._rotated_ingest("investopedia",    INVESTOPEDIA_DERSLER,    1)),
            ("litefinance",     lambda: self._rotated_ingest("litefinance",     LITEFINANCE_DERSLER,     1)),
        ):
            try:
                out = ingest_fn()
                result["sources"][src_key] = out
                result["total_lessons"] += int(out.get("lessons", 0))
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{src_key}: {type(exc).__name__}: {exc}")
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

    # ------------------------------------------------------------------

    def ingest_borsaninizinden(self) -> dict[str, Any]:
        return self.ingest(BORSANINIZINDEN_DERSLER)

    def ingest(self, url_list: list[tuple[str, str, str]]) -> dict[str, Any]:
        total_lessons = 0
        articles_fetched = 0
        proposal_id = None

        for url, title, tag_hint in url_list:
            article = self.fetch_article(url, title, tag_hint)
            if article is None:
                continue
            articles_fetched += 1
            # Rule-based
            rl = self.extract_lessons_rule_based(article)
            for lesson in rl:
                self.journal.save(lesson)
                total_lessons += 1
            # LLM (optional)
            if self.use_llm:
                llm_lessons = self.extract_lessons_llm(article)
                for lesson in llm_lessons:
                    self.journal.save(lesson)
                    total_lessons += 1
            time.sleep(0.5)  # polite rate-limit

        # Proposal submit: insan farkında olsun
        try:
            p = Proposal(
                proposal_id="",
                proposal_type="curriculum",
                title=f"Curriculum ingest: {articles_fetched} makale → {total_lessons} ders",
                author_agent="Talos Curriculum",
                summary=(
                    f"Borsanın İzinden (+ ek kaynaklar) üzerinden {articles_fetched} makale tarandı.\n"
                    f"{total_lessons} ders journal'a eklendi (source:curriculum tag'li).\n"
                    f"Nova hipotez üretirken Retriever ile bu dersleri okuyacak."
                ),
                estimated_benefit="Türkçe TA eğitim malzemesinden edge-pattern hatırlatmaları",
                estimated_risk="Kuramsal bilgi; backtest kanıtı gerekli. Körlemesine uygulama olmasın.",
                action_steps=[
                    "Nova hipotez üretiminde Retriever'ın 'source:curriculum' tag'li dersleri öncelik olarak çekmesi",
                    "Bir sonraki hipotez batch'inde curriculum-inspired formation (OBO, fincan-kulp) dene",
                    "6 ay sonra curriculum-inspired vs rastgele hipotezlerin başarı oranını karşılaştır",
                ],
                source_url=url_list[0][0] if url_list else None,
                metadata={
                    "articles_fetched": articles_fetched,
                    "total_lessons": total_lessons,
                    "use_llm": self.use_llm,
                },
            )
            proposal_id = self.queue.submit(p)
        except Exception:
            pass

        return {
            "articles_fetched": articles_fetched,
            "total_lessons": total_lessons,
            "use_llm": self.use_llm,
            "proposal_id": proposal_id,
        }


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1) if m else "unknown"


# Faz 7 — kısa alias (EducatorLoop ve smoke testler için).
CurriculumLoader = TalosCurriculumLoader

__all__ = [
    "ArticleContent",
    "BABYPIPS_DERSLER",
    "BORSANINIZINDEN_DERSLER",
    "CurriculumLoader",
    "INVESTOPEDIA_DERSLER",
    "LITEFINANCE_DERSLER",
    "TalosCurriculumLoader",
]
