"""oto-bot komuta merkezi — Streamlit dashboard (Türkçe, yumuşak koyu tema).

Sayfalar (sol panel):
    Genel Bakış     — KPI + aktivite akışı
    Ajanlar         — kart grid, ne yaptıkları + temel özellikleri
    Org Şeması      — departmanlar + raporlama hattı
    İş Akışı        — interaktif n8n tarzı node graph
    Bot Projeleri   — tüm deneyler + ROI/WR/trades/komisyon
    Hedefler        — %60 CAGR hedefine ilerleme
    Pod'lar         — sermaye dağılımı
    Canlı Akış      — log kuyruğu + son debate'ler

Çalıştırma:
    .venv\\Scripts\\python.exe -m streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd
import streamlit as st
import yaml
from streamlit_agraph import Config, Edge, Node, agraph

# ---------------------------------------------------------------------------
# Sayfa ayarı + tema
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="oto-bot // komuta merkezi",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Yumuşak koyu tema — göz yormayan palette
st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

        /* ---------- Base ---------- */
        .stApp {
            background: #1a1f2e;
            color: #cdd6e0;
        }
        html, body, [class*="css"], [class*="st-"] {
            font-family: 'Inter', -apple-system, sans-serif !important;
        }
        code, pre, kbd, samp {
            font-family: 'JetBrains Mono', 'Cascadia Code', monospace !important;
            font-size: 0.85rem !important;
            line-height: 1.55 !important;
        }

        /* ---------- Headers ---------- */
        h1 { color: #e6ecf4 !important; font-weight: 700 !important; letter-spacing: -0.02em; }
        h2 { color: #dae2ec !important; font-weight: 600 !important; letter-spacing: -0.01em; }
        h3 { color: #c5cfdc !important; font-weight: 600 !important; }
        h4 { color: #c5cfdc !important; font-weight: 600 !important; }
        p, li, span, label { color: #b8c1cc !important; line-height: 1.55; }
        .stCaption, small { color: #7e8898 !important; }

        /* ---------- Sidebar ---------- */
        section[data-testid="stSidebar"] {
            background: #151a27 !important;
            border-right: 1px solid #242b3c;
        }
        section[data-testid="stSidebar"] * { color: #c2cad7 !important; }
        section[data-testid="stSidebar"] h3 { color: #e6ecf4 !important; }

        /* ---------- Metric kartları ---------- */
        [data-testid="stMetricValue"] {
            font-size: 1.55rem !important;
            font-weight: 700 !important;
            color: #e6ecf4 !important;
        }
        [data-testid="stMetricLabel"] {
            color: #8691a0 !important;
            font-size: 0.73rem !important;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            font-weight: 500 !important;
        }
        [data-testid="stMetricDelta"] { color: #8691a0 !important; }

        /* ---------- Card borders (Streamlit container border) ---------- */
        div[data-testid="stVerticalBlockBorderWrapper"] > div {
            background: #212838;
            border: 1px solid #2c3447 !important;
            border-radius: 10px;
        }

        /* ---------- Buttons ---------- */
        .stButton button {
            border-radius: 8px;
            border: 1px solid #334055;
            background: #232b3c;
            color: #d7dfeb !important;
            font-weight: 500;
            padding: 6px 14px;
        }
        .stButton button:hover {
            background: #2c3447;
            border-color: #455170;
            color: #ffffff !important;
        }

        /* ---------- Inputs ---------- */
        .stSelectbox > div > div,
        .stTextInput > div > input,
        .stNumberInput > div > div > input {
            background: #232b3c !important;
            color: #e0e6f0 !important;
            border: 1px solid #334055 !important;
            border-radius: 8px !important;
        }
        .stCheckbox label { color: #c2cad7 !important; }

        /* ---------- Dataframes ---------- */
        .stDataFrame {
            background: #232b3c !important;
            border-radius: 8px;
            border: 1px solid #2c3447;
        }
        .stDataFrame [data-testid="stDataFrameResizable"] * {
            color: #d7dfeb !important;
        }

        /* ---------- Code blocks ---------- */
        pre, .stCode, code {
            background: #171c29 !important;
            color: #c5d0de !important;
            border: 1px solid #242b3c !important;
            border-radius: 8px !important;
            padding: 12px !important;
            white-space: pre-wrap !important;
            word-break: break-word !important;
        }
        pre code { background: transparent !important; border: none !important; padding: 0 !important; }

        /* ---------- JSON viewer (st.json) ---------- */
        [data-testid="stJson"] {
            background: #171c29 !important;
            border: 1px solid #242b3c !important;
            border-radius: 8px !important;
            padding: 10px !important;
        }
        [data-testid="stJson"] * { font-family: 'JetBrains Mono', monospace !important; }

        /* ---------- Tabs ---------- */
        button[data-baseweb="tab"] {
            color: #8b94a3 !important;
            font-weight: 500 !important;
        }
        button[data-baseweb="tab"][aria-selected="true"] {
            color: #60a5fa !important;
            border-bottom-color: #60a5fa !important;
        }

        /* ---------- Expanders ---------- */
        details summary {
            background: #1e2433 !important;
            border-radius: 8px !important;
            color: #cdd6e0 !important;
            padding: 10px 14px !important;
            font-weight: 500;
        }
        details[open] summary { border-radius: 8px 8px 0 0 !important; }

        /* ---------- Ajan kartı ---------- */
        .agent-card {
            background: #212838;
            border: 1px solid #2c3447;
            border-radius: 10px;
            padding: 18px 20px;
            margin-bottom: 14px;
        }
        .agent-card .head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
        .agent-card .title { display: flex; align-items: center; gap: 10px; }
        .agent-card .title .icon {
            width: 34px; height: 34px; border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            font-size: 18px; background: #2c3447;
        }
        .agent-card h4 { margin: 0 !important; color: #e6ecf4 !important; font-size: 1.05rem; font-weight: 600; }
        .agent-card .role { color: #8691a0; font-size: 0.82rem; }

        .agent-card .section-label {
            color: #8691a0; font-size: 0.70rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.08em;
            margin: 14px 0 4px 0;
        }
        .agent-card .what-does {
            color: #cdd6e0; line-height: 1.55; font-size: 0.92rem;
        }
        .agent-card ul.features {
            margin: 4px 0 0 0; padding-left: 18px;
        }
        .agent-card ul.features li {
            color: #b8c1cc; font-size: 0.88rem; line-height: 1.6;
            padding-left: 2px; margin-bottom: 3px;
        }

        .badge {
            display: inline-block; padding: 3px 10px; border-radius: 20px;
            font-size: 0.70rem; font-weight: 600; letter-spacing: 0.04em;
            border: 1px solid transparent;
        }
        .badge-active { background: #1a3822; color: #4ade80; border-color: #22602e; }
        .badge-retired { background: #381a1a; color: #ef7e7e; border-color: #602222; }
        .badge-dept {
            background: #1a2a42; color: #7fb9ff; border-color: #2a4263;
            margin-left: 6px;
        }

        /* ---------- Feed item ---------- */
        .feed-item {
            padding: 12px 14px;
            border-left: 3px solid #3b5a8a;
            background: #1e2433;
            margin-bottom: 7px;
            border-radius: 6px;
            color: #cdd6e0;
            font-size: 0.92rem;
        }
        .feed-item .meta { color: #7e8898; font-size: 0.72rem; margin-bottom: 4px; }

        /* ---------- Goal bar ---------- */
        .goal-track {
            height: 18px; background: #1e2433; border-radius: 20px;
            overflow: hidden; border: 1px solid #2c3447;
        }
        .goal-fill {
            height: 100%;
            background: linear-gradient(90deg, #60a5fa, #34d399);
            transition: width 400ms ease;
        }

        /* ---------- Info / warning / error kutu ---------- */
        .stAlert { border-radius: 10px !important; }
        div[data-baseweb="notification"] { background: #1e2433 !important; border: 1px solid #2c3447 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Veri yolu ve yükleyiciler
# ---------------------------------------------------------------------------

ROOT = _ROOT
AGENTS_FILE = ROOT / "memories" / "agents.json"
PODS_FILE = ROOT / "memories" / "pods.json"
DB_FILE = ROOT / "artifacts" / "experiments.sqlite3"
WINNERS_FILE = ROOT / "artifacts" / "winners.jsonl"
LOG_FILE = ROOT / "logs" / "autonomous.log"
ORG_YAML = ROOT / "configs" / "organization.yaml"
CLAUDE_AGENTS_DIR = ROOT / ".claude" / "agents"


# Her ajan için yapılandırılmış profil — ne yaptığı + temel özellikleri + icon
AGENT_PROFILES: dict[str, dict[str, Any]] = {
    "Atlas CEO": {
        "icon": "👑",
        "what_does": "Laboratuvarın Head of Trading'i. Tüm ajanlardan gelen bilgiyi birleştirir, sermaye dağıtır, promote/reject kararını verir. BofA / Citadel doktrininde düşünür.",
        "features": [
            "Sabah briefing üretir (rejim + makro + book riski)",
            "Investment committee + Risk committee başkanı",
            "Pod sermaye dağılımını Sharpe/DD/korelasyona göre rebalance",
            "Panel debate sonucu + eşik + trend → promote / hold / iterate / reject",
            "Hire/fire yetkisi: yeni specialist oluşturur, performans düşen ajanı retire eder",
        ],
    },
    "Iris ChiefOfStaff": {
        "icon": "📋",
        "what_does": "Yürütme takipçisi. Ajanların ne yaptığını, hangi iş akışlarının beklediğini, hangi bağımlılıkların çözüldüğünü CEO'ya raporlar.",
        "features": [
            "Günlük dashboard üretir (artifacts/dashboard.json)",
            "Her ajanın son çıktısını izler",
            "24 saat çıktı üretmeyen ajanı flag eder",
            "Engelleyici (blocker) listesi tutar",
            "Committee follow-up'larını sıraya koyar",
        ],
    },
    "Vega MarketIntel": {
        "icon": "🔭",
        "what_does": "Piyasa taraması yapar. 25+ sembolü her döngüde rejim, volatilite, ADX, BB genişliği ve hacim açısından değerlendirir; hangi coin'in/pazarın hangi stratejiye uygun olduğunu söyler.",
        "features": [
            "Her sembol için FAVORABLE / NEUTRAL / UNFAVORABLE sınıflaması",
            "Rejim (bull/bear/sideways) + ATR% + ADX seviyesi",
            "Bollinger genişliği + hacim trendi",
            "Çıktı: artifacts/market_intel.json",
        ],
    },
    "Nova StrategyRND": {
        "icon": "💡",
        "what_does": "Yeni strateji fikirleri üretir. CEO'dan zayıflık raporu alıp hipotez ve parametre mutasyonları tasarlar. Hem config değişiklikleri hem yeni sinyal kodu önerir.",
        "features": [
            "Her hipotez için thesis + invalidation şartı",
            "5-10 parametre mutasyonu / döngü",
            "Yeni indikatör, filtre, sinyal logic önerileri",
            "Niye-başarısız-oldu analizi → yeni öneri",
            "Çıktı: artifacts/strategy_proposals.json",
        ],
    },
    "Sigma Quant": {
        "icon": "📊",
        "what_does": "Kantitatif doğrulama ajanı. Her backtest sonucu için overfitting, örneklem yeterliliği, Monte Carlo DD, rejim dayanıklılığı ve parametre duyarlılığı kontrolü yapar.",
        "features": [
            "Walk-forward: IS vs OOS Sharpe oranı",
            "Minimum 100 trade örneklem",
            "Monte Carlo 95%-DD hesabı",
            "Binomial + t-test istatistiksel anlamlılık",
            "Çıktı: PASS / FAIL / NEEDS_MORE_DATA",
        ],
    },
    "Mercury Macro": {
        "icon": "🌐",
        "what_does": "Makro ve çapraz varlık overlay'i. BTC dominansı, DXY proxy'si, cross-asset korelasyon, risk-on/off skoru üretir. Kriz tespitinde tüm promosyonları dondurur.",
        "features": [
            "BTC/ETH dominans proxy'si",
            "Fear/Greed proxy (getiri skew + ATR genişlemesi)",
            "Bias etiketi: risk_on / neutral / risk_off / crisis",
            "Strateji-ortam hizalamasını yorumlar",
            "Çıktı: artifacts/macro_context.json",
        ],
    },
    "Regime Oracle": {
        "icon": "🎯",
        "what_does": "Her pazarın rejimini sınıflar (trend_up/down, range, high_vol, crisis). ADX + EMA + ATR + BB kombinasyonunu kullanır, önceki 5 barı pekiştirme için okur.",
        "features": [
            "6 rejim etiketi + 0-1 güven skoru",
            "Strateji-rejim fit matrisi (scalp → range, swing → trend)",
            "prior_regime takibi — değişim CEO'ya bildirilir",
            "regime_age_bars: rejim ne kadar sürdü",
            "Çıktı: artifacts/regime_snapshot.json",
        ],
    },
    "Helix Backtest": {
        "icon": "⚗️",
        "what_does": "Backtest infaz ajanı. Gerçek OHLCV (veya sentetik fallback) üzerinde her stratejiyi fee + slippage + funding varsayımlarıyla koşar. Sonucu dürüstçe raporlar.",
        "features": [
            "Sabit notional + olasılık bazlı kaldıraç",
            "0.15% fee / side, volume filtresi",
            "Max 6 pozisyon, ardışık kayıp yönetimi",
            "Funding + ATR trailing stop",
            "Çıktı: experiment_id + tam metrik listesi",
        ],
    },
    "Shockwave StressLab": {
        "icon": "💥",
        "what_does": "İsimli stres senaryolarını çalıştırır. 6 tarihsel şok (COVID-2020, Luna-2022, FTX, Flash Crash, Slow Bleed, Vol Compression) uygular ve hayatta kalma kriterini test eder.",
        "features": [
            "Her senaryo: price shock + vol multiplier + correlation jump + liquidity haircut",
            "Hayatta kalma: DD ≥ -25% ve kill-switch atmamış",
            "Promote için min 3 senaryo SURVIVED şart",
            "Çıktı: artifacts/stress_results/",
        ],
    },
    "Sentinel Risk": {
        "icon": "🛡️",
        "what_does": "Strateji bazlı risk eşiklerini uygular. Max DD, max tek trade riski, max günlük kayıp, max kaldıraç, min trade sayısı — hepsine VETO yetkisi vardır.",
        "features": [
            "Max DD %20 → REJECT",
            "Max tek trade risk %2 → REJECT",
            "Ardışık 15 kayıp → REJECT",
            "Günlük $500 kayıp → halt",
            "Max 5x kaldıraç, max 6 pozisyon",
        ],
    },
    "Apex PortfolioRisk": {
        "icon": "⚖️",
        "what_does": "Stratejilerden bağımsız, doğrudan CEO'ya raporlayan kitap-seviyesi risk yöneticisi. VaR, ES, korelasyon, konsantrasyon izler. Veto yetkili.",
        "features": [
            "Parametrik + tarihsel VaR 95%",
            "Expected Shortfall (CVaR) 95%",
            "Pod-pod korelasyon matrisi (max 0.75)",
            "Konsantrasyon (tek pod max %20)",
            "Status: green / amber / red / black",
        ],
    },
    "Cassandra PreMortem": {
        "icon": "🔮",
        "what_does": "Promosyon öncesi başarısızlık senaryoları üretir: 'bu strateji 3 ay sonra nasıl çöker?' 10 başlıklı failure taksonomisi tarar.",
        "features": [
            "Overfitting / regime fragility / sample size",
            "Low-edge / WR trap / correlation leak",
            "Execution naivety / tail blindness",
            "Capacity / data leakage",
            "0-100 risk skoru → RED / AMBER / GREEN",
        ],
    },
    "Forge Execution": {
        "icon": "🔥",
        "what_does": "Onaylanan stratejileri paper trading olarak çalıştırır. Margin takibi, slippage/latency simülasyonu, kill-switch, real-time P&L.",
        "features": [
            "Sabit $600 notional, prob-based leverage",
            "+0.05% ekstra slippage (gerçeğe yakın)",
            "Real-time pozisyon + P&L tracking",
            "Kill-switch kitap DD -%10'u aşarsa",
            "Çıktı: artifacts/paper_trading_state.json",
        ],
    },
    "Tariq TCA": {
        "icon": "📐",
        "what_does": "Transaction cost analysis. Her emir için arrival slippage, implementation shortfall, market impact, fees, latency ölçer. Backtest varsayımlarının gerçekçiliğini denetler.",
        "features": [
            "Her emir için ExecutionReport",
            "Avg slippage 15bps üstü → uyarı",
            "Avg market impact 25bps üstü → alert",
            "Simüle vs gerçek fill karşılaştırması",
            "Çıktı: artifacts/tca/<date>.jsonl",
        ],
    },
    "Ledger Allocator": {
        "icon": "💰",
        "what_does": "Pod bazlı sermaye dağıtıcı. Her stratejiye sleeve açar, Sharpe/DD'ye göre günlük rebalance yapar, %5 DD'de yarıya indirir, %7.5'te kapatır.",
        "features": [
            "Millennium/Citadel tarzı pod modeli",
            "Otomatik halve / retire tetikleri",
            "Sharpe-ağırlıklı, DD-cezalı yeniden dağıtım",
            "< %5'lik mikro değişiklik atlanır",
            "Çıktı: memories/pods.json",
        ],
    },
    "Archive Memory": {
        "icon": "📚",
        "what_does": "Kurumsal hafıza yöneticisi. Eski deneyleri özetler, sıkıştırır; hiçbir bilgi kaybolmasın ama token da boşa gitmesin.",
        "features": [
            "Son 100 deneyi tam ayrıntıyla tutar",
            "30 gün öncesi → aggregate özet",
            "artifacts/experiment_index.json maintain",
            "Duplicate / bozuk kayıt temizliği",
            "Çıktı: SQLite + JSON index",
        ],
    },
    "Pulse Analytics": {
        "icon": "📈",
        "what_does": "Performans analitik ajanı. Strateji versiyonlarını karşılaştırır, improvement trendleri çıkarır, scorecard üretir. Sharpe ile sıralar, ROI değil.",
        "features": [
            "Versiyon karşılaştırma",
            "Coin rank (en kârlı/zararlı)",
            "Rejim bazlı performans",
            "Risk-adjusted ranking (Sharpe birinci kriter)",
            "Çıktı: artifacts/scorecard.json",
        ],
    },
    "Ledger Attribution": {
        "icon": "🧾",
        "what_does": "P&L attribution uzmanı. Her trade listesini sinyal / sembol / rejim / saat bazında ayrıştırır; alpha vs beta ayırır, fee/slippage/funding çıkarır.",
        "features": [
            "by_signal / by_symbol / by_regime / by_hour",
            "alpha_pnl vs beta_pnl ayrımı",
            "net_edge_after_costs hesabı",
            "Tek cümlelik narrative üretir",
            "Çıktı: artifacts/attributions/<experiment_id>.json",
        ],
    },
}


@st.cache_data(ttl=5)
def load_agents() -> list[dict[str, Any]]:
    if not AGENTS_FILE.exists():
        return []
    return json.loads(AGENTS_FILE.read_text(encoding="utf-8"))


@st.cache_data(ttl=5)
def load_pods() -> list[dict[str, Any]]:
    if not PODS_FILE.exists():
        return []
    return json.loads(PODS_FILE.read_text(encoding="utf-8"))


@st.cache_data(ttl=5)
def load_winners() -> list[dict[str, Any]]:
    if not WINNERS_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in WINNERS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


@st.cache_data(ttl=5)
def load_org() -> dict[str, Any]:
    if not ORG_YAML.exists():
        return {}
    return yaml.safe_load(ORG_YAML.read_text(encoding="utf-8")) or {}


@st.cache_data(ttl=30)
def load_agent_docs() -> dict[str, str]:
    docs: dict[str, str] = {}
    if not CLAUDE_AGENTS_DIR.exists():
        return docs
    for md in CLAUDE_AGENTS_DIR.glob("*.md"):
        docs[md.stem] = md.read_text(encoding="utf-8")
    return docs


@st.cache_resource
def _conn() -> sqlite3.Connection | None:
    if not DB_FILE.exists():
        return None
    con = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    con = _conn()
    if con is None:
        return []
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _parse_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        try:
            data = json.loads(r["data"]) if isinstance(r.get("data"), str) else r.get("data")
        except Exception:
            data = {}
        merged = {"id": r.get("id"), "timestamp": r.get("timestamp"),
                  "category": r.get("category"), "agent_id": r.get("agent_id")}
        if isinstance(data, dict):
            merged.update(data)
        out.append(merged)
    return out


@st.cache_data(ttl=5)
def experiments_df(limit: int = 500) -> pd.DataFrame:
    rows = _query("SELECT * FROM experiments ORDER BY timestamp DESC LIMIT ?", (limit,))
    parsed = _parse_rows(rows)
    if not parsed:
        return pd.DataFrame()
    df = pd.DataFrame(parsed)
    if "total_trades" in df.columns and "win_rate" in df.columns:
        df["kazanan_trade"] = (df["total_trades"].fillna(0) * df["win_rate"].fillna(0)).round().astype(int)
        df["kaybeden_trade"] = (df["total_trades"].fillna(0).astype(int) - df["kazanan_trade"]).clip(lower=0)
    if "total_trades" in df.columns:
        df["komisyon_tahmini_usd"] = (df["total_trades"].fillna(0) * 2 * 600 * 0.0015).round(2)
    return df


@st.cache_data(ttl=5)
def hypotheses_df(limit: int = 200) -> pd.DataFrame:
    rows = _query("SELECT * FROM hypotheses ORDER BY timestamp DESC LIMIT ?", (limit,))
    parsed = _parse_rows(rows)
    return pd.DataFrame(parsed) if parsed else pd.DataFrame()


@st.cache_data(ttl=5)
def decisions_df(limit: int = 200) -> pd.DataFrame:
    rows = _query("SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (limit,))
    parsed = _parse_rows(rows)
    return pd.DataFrame(parsed) if parsed else pd.DataFrame()


@st.cache_data(ttl=5)
def debates_df(limit: int = 200) -> pd.DataFrame:
    rows = _query("SELECT * FROM debate_records ORDER BY timestamp DESC LIMIT ?", (limit,))
    parsed = _parse_rows(rows)
    return pd.DataFrame(parsed) if parsed else pd.DataFrame()


@st.cache_data(ttl=5)
def tail_log(lines: int = 120) -> str:
    if not LOG_FILE.exists():
        return "(log bulunamadı)"
    data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def count_recent_activity() -> dict[str, int]:
    con = _conn()
    if con is None:
        return {"experiments": 0, "decisions": 0, "hypotheses": 0, "debates": 0}
    q = lambda t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {
        "experiments": q("experiments"),
        "decisions": q("decisions"),
        "hypotheses": q("hypotheses"),
        "debates": q("debate_records"),
    }


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.markdown("### 🧭 oto-bot")
st.sidebar.caption("Komuta merkezi · Atlas CEO")

PAGE = st.sidebar.radio(
    "Bölüm",
    [
        "🏠 Genel Bakış",
        "👥 Ajanlar",
        "🏛️ Org Şeması",
        "🔄 İş Akışı",
        "🧪 Bot Projeleri",
        "🎯 Hedefler",
        "📦 Pod'lar",
        "📡 Canlı Akış",
    ],
)

auto_refresh = st.sidebar.checkbox("🔁 Otomatik yenile (15 sn)", value=True)

st.sidebar.markdown("---")
stats = count_recent_activity()
st.sidebar.metric("Deneyler", f"{stats['experiments']:,}")
st.sidebar.metric("Kararlar", f"{stats['decisions']:,}")
st.sidebar.metric("Hipotezler", f"{stats['hypotheses']:,}")
st.sidebar.metric("Debate'ler", f"{stats['debates']:,}")

agents = load_agents()
active = [a for a in agents if a.get("active")]
st.sidebar.metric("Aktif ajan", f"{len(active)}")
winners = load_winners()
st.sidebar.metric("🏆 Kazanan", f"{len(winners)}")
pods = load_pods()
st.sidebar.metric("Pod", f"{len(pods)}")

# ---------------------------------------------------------------------------
# 🏠 GENEL BAKIŞ
# ---------------------------------------------------------------------------

if PAGE == "🏠 Genel Bakış":
    st.title("🏠 Komuta merkezi")
    st.caption("Atlas CEO doktrini · Paper trading laboratuvarı · Risk-ayarlı önce")

    exps = experiments_df(500)

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Toplam deney", f"{stats['experiments']:,}")
    promoted_ct = int(exps["promoted"].fillna(False).sum()) if "promoted" in exps.columns else 0
    col2.metric("Promote", f"{promoted_ct:,}")
    col3.metric("🏆 Kazanan", f"{len(winners):,}")
    col4.metric("Aktif ajan", f"{len(active)}")
    col5.metric("Aktif pod", f"{len([p for p in pods if p.get('status') == 'active'])}")
    avg_sharpe = exps["sharpe"].mean() if "sharpe" in exps.columns and not exps.empty else 0
    col6.metric("Ort. Sharpe", f"{avg_sharpe:.2f}")

    st.divider()

    c1, c2 = st.columns([2, 1])

    with c1:
        st.subheader("📈 Son 50 deneyin Sharpe seyri")
        if not exps.empty and "sharpe" in exps.columns:
            recent = exps.head(50).copy().reset_index(drop=True)
            recent["sıra"] = recent.index
            st.line_chart(recent.set_index("sıra")[["sharpe"]], height=220)
        else:
            st.info("Henüz yeterli veri yok. Otonom loop ilk deneyleri üretiyor.")

        st.subheader("💰 ROI dağılımı (son 100)")
        if not exps.empty and "roi" in exps.columns:
            st.bar_chart(exps.head(100).reset_index(drop=True)["roi"], height=220)
        else:
            st.info("Veri yok.")

    with c2:
        st.subheader("🥇 En iyi 5 (Sharpe)")
        if not exps.empty and "sharpe" in exps.columns:
            best = exps.dropna(subset=["sharpe"]).nlargest(5, "sharpe")[
                ["hypothesis_title", "sharpe", "roi", "max_drawdown", "total_trades"]
            ].rename(columns={
                "hypothesis_title": "başlık",
                "sharpe": "Sharpe",
                "roi": "ROI",
                "max_drawdown": "DD",
                "total_trades": "trade",
            })
            st.dataframe(best, hide_index=True, use_container_width=True)
        else:
            st.caption("Veri yok.")

        st.subheader("📉 En kötü 5")
        if not exps.empty and "sharpe" in exps.columns:
            worst = exps.dropna(subset=["sharpe"]).nsmallest(5, "sharpe")[
                ["hypothesis_title", "sharpe", "roi", "max_drawdown"]
            ].rename(columns={
                "hypothesis_title": "başlık",
                "sharpe": "Sharpe",
                "roi": "ROI",
                "max_drawdown": "DD",
            })
            st.dataframe(worst, hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("🕒 Son aktivite")
    recent_decs = decisions_df(8)
    if not recent_decs.empty:
        for _, row in recent_decs.iterrows():
            ts = str(row.get("timestamp", ""))[:19]
            dec = row.get("decision", "")
            reason = str(row.get("reasoning", ""))[:220]
            st.markdown(
                f"<div class='feed-item'><div class='meta'>{ts} · karar: <b>{dec}</b></div>{reason}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("Henüz karar kaydı yok.")

# ---------------------------------------------------------------------------
# 👥 AJANLAR
# ---------------------------------------------------------------------------

elif PAGE == "👥 Ajanlar":
    st.title("👥 Ajan kadrosu")
    st.caption(f"Toplam {len(agents)} ajan · {len(active)} aktif · her kart ne yaptığını ve temel özelliklerini gösterir")

    depts = sorted(set(a.get("department", "?") for a in agents))
    c1, c2, c3 = st.columns([1, 1, 2])
    dept_choice = c1.selectbox("Departman", ["— tümü —"] + depts)
    only_active = c2.checkbox("Sadece aktif", value=True)
    search = c3.text_input("Ara (isim/rol)", value="")

    filtered = agents
    if dept_choice != "— tümü —":
        filtered = [a for a in filtered if a.get("department") == dept_choice]
    if only_active:
        filtered = [a for a in filtered if a.get("active")]
    if search:
        s = search.lower()
        filtered = [a for a in filtered if s in a.get("name", "").lower() or s in a.get("role", "").lower()]

    hypos = hypotheses_df(400)
    decs = decisions_df(400)
    debs = debates_df(400)

    # 2 sütun grid
    for i in range(0, len(filtered), 2):
        cols = st.columns(2, gap="medium")
        for j, col in enumerate(cols):
            if i + j >= len(filtered):
                continue
            agent = filtered[i + j]
            name = agent.get("name", "?")
            role = agent.get("role", "")
            dept = agent.get("department", "")
            active_flag = "aktif" if agent.get("active") else "emekli"
            badge_class = "badge-active" if active_flag == "aktif" else "badge-retired"

            profile = AGENT_PROFILES.get(name, {
                "icon": "🤖",
                "what_does": agent.get("mandate", ""),
                "features": [],
            })
            icon = profile["icon"]
            what = profile["what_does"]
            feats = profile["features"]

            feats_html = "".join(f"<li>{f}</li>" for f in feats)

            with col:
                st.markdown(
                    f"""
                    <div class='agent-card'>
                        <div class='head'>
                            <div class='title'>
                                <div class='icon'>{icon}</div>
                                <div>
                                    <h4>{name}</h4>
                                    <div class='role'>{role}</div>
                                </div>
                            </div>
                            <div style='text-align:right;'>
                                <span class='badge {badge_class}'>{active_flag}</span>
                                <span class='badge badge-dept'>{dept}</span>
                            </div>
                        </div>
                        <div class='section-label'>📋 Ne yapıyor</div>
                        <div class='what-does'>{what}</div>
                        <div class='section-label'>⚡ Temel özellikler</div>
                        <ul class='features'>{feats_html}</ul>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                with st.expander(f"📡 {name} — son üretim"):
                    tabs = st.tabs(["Hipotezler", "Debate oyları", "Kararlar"])

                    with tabs[0]:
                        if not hypos.empty:
                            sub = hypos
                            if "author_agent_id" in sub.columns:
                                sub = sub[sub["author_agent_id"] == agent.get("agent_id")]
                            if sub.empty:
                                st.caption(f"{name} imzalı hipotez yok; genel son 5:")
                                sub = hypos.head(5)
                            cols_show = [c for c in ["timestamp", "title", "thesis"] if c in sub.columns]
                            st.dataframe(sub[cols_show].head(5), hide_index=True, use_container_width=True)
                        else:
                            st.caption("hipotez kaydı yok")

                    with tabs[1]:
                        if not debs.empty and "arguments" in debs.columns:
                            agent_debates = []
                            for _, row in debs.head(100).iterrows():
                                args = row.get("arguments", [])
                                if isinstance(args, list):
                                    for a in args:
                                        if isinstance(a, dict) and a.get("agent_name") == name:
                                            agent_debates.append({
                                                "konu": str(row.get("topic", ""))[:60],
                                                "oy": a.get("position", ""),
                                                "gerekçe": str(a.get("reasoning", ""))[:150],
                                            })
                            if agent_debates:
                                st.dataframe(pd.DataFrame(agent_debates).head(8), hide_index=True, use_container_width=True)
                            else:
                                st.caption(f"{name} henüz bu pencerede debate'e katılmadı")
                        else:
                            st.caption("debate kaydı yok")

                    with tabs[2]:
                        if not decs.empty:
                            cols_show = [c for c in ["timestamp", "decision"] if c in decs.columns]
                            st.dataframe(decs[cols_show].head(5), hide_index=True, use_container_width=True)
                        else:
                            st.caption("karar kaydı yok")

# ---------------------------------------------------------------------------
# 🏛️ ORG ŞEMASI
# ---------------------------------------------------------------------------

elif PAGE == "🏛️ Org Şeması":
    st.title("🏛️ Organizasyon şeması")
    st.caption("Atlas CEO'dan departmanlara raporlama hattı · düğümleri sürükle/tıkla")

    org = load_org()
    depts = org.get("departments", {})

    nodes: list[Node] = []
    edges: list[Edge] = []

    DEPT_COLORS = {
        "Executive": "#fbbf24",
        "Research": "#60a5fa",
        "Simulation": "#f59e0b",
        "Governance": "#ef4444",
        "Execution": "#34d399",
        "Knowledge": "#a78bfa",
        "Analytics": "#f472b6",
    }

    nodes.append(Node(
        id="CEO",
        label="Atlas CEO\n(Head of Trading)",
        size=45,
        color="#fbbf24",
        shape="dot",
    ))

    for dept_name in depts:
        if dept_name == "Executive":
            continue
        nodes.append(Node(
            id=f"DEPT_{dept_name}",
            label=dept_name,
            size=30,
            color=DEPT_COLORS.get(dept_name, "#64748b"),
            shape="box",
        ))
        edges.append(Edge(source="CEO", target=f"DEPT_{dept_name}"))

    for dept_name, agent_names in depts.items():
        if not isinstance(agent_names, list):
            continue
        parent = "CEO" if dept_name == "Executive" else f"DEPT_{dept_name}"
        for ag_name in agent_names:
            if ag_name == "Atlas CEO":
                continue
            nodes.append(Node(
                id=ag_name,
                label=ag_name,
                size=20,
                color=DEPT_COLORS.get(dept_name, "#64748b"),
                shape="dot",
            ))
            edges.append(Edge(source=parent, target=ag_name))

    config = Config(
        width=1100, height=650, directed=True,
        nodeHighlightBehavior=True, highlightColor="#F7A7A6",
        physics=True, hierarchical=False,
    )
    agraph(nodes=nodes, edges=edges, config=config)
    st.caption("📌 Düğümleri sürükle, tıkla, yakınlaştır. Her renk bir departman.")

    with st.expander("📋 Departman detayları"):
        for dept_name, agent_names in depts.items():
            if isinstance(agent_names, list):
                st.markdown(f"**{dept_name}** — {len(agent_names)} ajan")
                st.markdown(" · ".join(f"`{a}`" for a in agent_names))
                st.markdown("")

    with st.expander("⚖️ Komiteler"):
        committees = org.get("committees", {})
        for cname, cinfo in committees.items():
            st.markdown(f"**{cname}** — *başkan:* {cinfo.get('chair', '?')}, *ritim:* {cinfo.get('cadence', '?')}")
            st.caption(cinfo.get("mandate", ""))
            members = cinfo.get("members", [])
            st.markdown(" · ".join(f"`{m}`" for m in members))
            st.markdown("")

# ---------------------------------------------------------------------------
# 🔄 İŞ AKIŞI
# ---------------------------------------------------------------------------

elif PAGE == "🔄 İş Akışı":
    st.title("🔄 Araştırma iş akışı")
    st.caption("n8n tarzı · sürüklenebilir · tıklanabilir node graph")

    nodes = [
        Node(id="N_Vega", label="Vega\nMarket Intel", size=26, color="#60a5fa", shape="dot"),
        Node(id="N_Mercury", label="Mercury\nMacro", size=26, color="#60a5fa", shape="dot"),
        Node(id="N_Regime", label="Regime\nOracle", size=26, color="#60a5fa", shape="dot"),
        Node(id="N_Nova", label="Nova\nHipotez", size=32, color="#3b82f6", shape="box"),
        Node(id="N_Helix", label="Helix\nBacktest", size=32, color="#f59e0b", shape="box"),
        Node(id="N_Shock", label="Shockwave\nStres Lab", size=26, color="#f59e0b", shape="dot"),
        Node(id="N_Sentinel", label="Sentinel\nRisk Gate", size=26, color="#ef4444", shape="dot"),
        Node(id="N_Apex", label="Apex\nPortfolio Risk", size=28, color="#ef4444", shape="hexagon"),
        Node(id="N_Cassandra", label="Cassandra\nPre-Mortem", size=26, color="#ef4444", shape="dot"),
        Node(id="N_Debate", label="8-sesli\nPANEL", size=38, color="#a78bfa", shape="hexagon"),
        Node(id="N_CEO", label="Atlas CEO\nkarar", size=42, color="#fbbf24", shape="dot"),
        Node(id="N_Ledger", label="Ledger\nSermaye", size=26, color="#34d399", shape="dot"),
        Node(id="N_Forge", label="Forge\nPaper Exec", size=26, color="#34d399", shape="dot"),
        Node(id="N_Tariq", label="Tariq\nTCA", size=22, color="#34d399", shape="dot"),
        Node(id="N_Pulse", label="Pulse\nAnalitik", size=24, color="#f472b6", shape="dot"),
        Node(id="N_Attr", label="Ledger\nPnL Attribution", size=22, color="#f472b6", shape="dot"),
        Node(id="N_Archive", label="Archive\nHafıza", size=26, color="#a78bfa", shape="box"),
    ]
    edges = [
        Edge(source="N_Vega", target="N_Nova"),
        Edge(source="N_Mercury", target="N_Nova"),
        Edge(source="N_Regime", target="N_Nova"),
        Edge(source="N_Nova", target="N_Helix"),
        Edge(source="N_Helix", target="N_Shock"),
        Edge(source="N_Helix", target="N_Sentinel"),
        Edge(source="N_Helix", target="N_Apex"),
        Edge(source="N_Helix", target="N_Cassandra"),
        Edge(source="N_Sentinel", target="N_Debate"),
        Edge(source="N_Apex", target="N_Debate"),
        Edge(source="N_Cassandra", target="N_Debate"),
        Edge(source="N_Shock", target="N_Debate"),
        Edge(source="N_Mercury", target="N_Debate"),
        Edge(source="N_Debate", target="N_CEO"),
        Edge(source="N_CEO", target="N_Ledger"),
        Edge(source="N_Ledger", target="N_Forge"),
        Edge(source="N_Forge", target="N_Tariq"),
        Edge(source="N_Tariq", target="N_Pulse"),
        Edge(source="N_Pulse", target="N_Attr"),
        Edge(source="N_Attr", target="N_Archive"),
        Edge(source="N_Archive", target="N_Nova", color="#666"),
        Edge(source="N_CEO", target="N_Nova", color="#666"),
    ]
    config = Config(
        width=1200, height=700, directed=True,
        nodeHighlightBehavior=True, highlightColor="#fbbf24",
        physics=True, hierarchical=False,
    )
    agraph(nodes=nodes, edges=edges, config=config)

    st.divider()
    cols = st.columns(3)
    cols[0].markdown("""
    **1. Araştırma**
    Nova hipotez üretir, Vega piyasayı tarar, Mercury makro bağlam verir, Regime Oracle rejim etiketler.
    """)
    cols[1].markdown("""
    **2. Simülasyon + Yönetişim**
    Helix backtest, Shockwave 6 stres senaryosu, Sentinel risk gate, Apex kitap-seviyesi VaR (veto), Cassandra pre-mortem.
    """)
    cols[2].markdown("""
    **3. Panel + Karar**
    8-sesli debate → CEO karar. Promote edilen strateji Ledger'da pod açar, Forge'da paper execute edilir.
    """)

# ---------------------------------------------------------------------------
# 🧪 BOT PROJELERİ
# ---------------------------------------------------------------------------

elif PAGE == "🧪 Bot Projeleri":
    st.title("🧪 Bot projeleri ve denemeleri")
    st.caption("Geliştirilen bot adayları · denemeler · ROI / WR / trade / komisyon")

    df = experiments_df(1500)
    if df.empty:
        st.info("Henüz deney yok. Otonom loop çalışıyor mu?")
    else:
        family_opts = sorted(df["strategy_family"].dropna().unique()) if "strategy_family" in df.columns else []
        market_opts = sorted(df["market"].dropna().unique()) if "market" in df.columns else []
        c1, c2, c3, c4 = st.columns(4)
        family = c1.selectbox("Strateji ailesi", ["tümü"] + list(family_opts))
        market = c2.selectbox("Market", ["tümü"] + list(market_opts))
        promoted_only = c3.checkbox("Sadece promote", value=False)
        min_trades = c4.number_input("Min. trade", value=0, step=10)

        sub = df.copy()
        if family != "tümü" and "strategy_family" in sub.columns:
            sub = sub[sub["strategy_family"] == family]
        if market != "tümü" and "market" in sub.columns:
            sub = sub[sub["market"] == market]
        if promoted_only and "promoted" in sub.columns:
            sub = sub[sub["promoted"] == True]  # noqa: E712
        if "total_trades" in sub.columns:
            sub = sub[sub["total_trades"].fillna(0) >= min_trades]

        st.caption(f"Gösterilen: **{len(sub)}** / toplam **{len(df)}**")

        if not sub.empty:
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            k1.metric("Ort. Sharpe", f"{sub['sharpe'].mean():.2f}" if "sharpe" in sub else "—")
            k2.metric("Ort. ROI", f"{sub['roi'].mean():.1%}" if "roi" in sub else "—")
            k3.metric("Ort. WR", f"{sub['win_rate'].mean():.1%}" if "win_rate" in sub else "—")
            k4.metric("Toplam trade", f"{int(sub['total_trades'].fillna(0).sum()):,}" if "total_trades" in sub else "—")
            wins_sum = int(sub['kazanan_trade'].sum()) if 'kazanan_trade' in sub else 0
            losses_sum = int(sub['kaybeden_trade'].sum()) if 'kaybeden_trade' in sub else 0
            k5.metric("Kazanan / Kaybeden", f"{wins_sum:,} / {losses_sum:,}")
            k6.metric("Tahmini komisyon ($)", f"{sub['komisyon_tahmini_usd'].sum():,.0f}" if "komisyon_tahmini_usd" in sub else "—")

        rename_map = {
            "timestamp": "zaman",
            "hypothesis_title": "başlık",
            "strategy_family": "aile",
            "market": "market",
            "roi": "ROI",
            "win_rate": "WR",
            "profit_factor": "PF",
            "sharpe": "Sharpe",
            "sortino": "Sortino",
            "max_drawdown": "DD",
            "cagr": "CAGR",
            "total_trades": "trade",
            "kazanan_trade": "kazanan",
            "kaybeden_trade": "kaybeden",
            "komisyon_tahmini_usd": "komisyon $",
            "regime": "rejim",
            "promoted": "promote",
            "notes": "not",
        }
        display_cols = [c for c in rename_map if c in sub.columns]
        st.dataframe(sub[display_cols].rename(columns=rename_map), hide_index=True, use_container_width=True, height=500)

        st.divider()
        st.subheader("🔍 Tek deney detayı")
        titles = sub["hypothesis_title"].dropna().tolist() if "hypothesis_title" in sub.columns else []
        if titles:
            pick = st.selectbox("Bir deney seç", titles[:200])
            if pick:
                row = sub[sub["hypothesis_title"] == pick].iloc[0].to_dict()
                cc1, cc2 = st.columns(2)
                with cc1:
                    st.markdown("**Tüm metrikler**")
                    # Compact table format, not raw JSON
                    display_rows = {k: v for k, v in row.items() if k != "notes" and v is not None}
                    st.dataframe(
                        pd.DataFrame([(k, v) for k, v in display_rows.items()], columns=["alan", "değer"]),
                        hide_index=True, use_container_width=True, height=450,
                    )
                with cc2:
                    st.markdown("**Notlar**")
                    notes_txt = str(row.get("notes", "")) or "(not yok)"
                    st.text_area("notes", value=notes_txt, height=450, label_visibility="collapsed")

# ---------------------------------------------------------------------------
# 🎯 HEDEFLER
# ---------------------------------------------------------------------------

elif PAGE == "🎯 Hedefler":
    st.title("🎯 Hedefler ve ilerleme")
    st.caption("Ana hedef: yıllık %60 CAGR · Sharpe ≥ 1.5 · DD ≥ -%15 · trade ≥ 100")

    exps = experiments_df(1000)

    target_found = len(winners)
    target_goal = 1
    best_cagr = exps["cagr"].max() if "cagr" in exps.columns and not exps.empty else 0
    best_sharpe = exps["sharpe"].max() if "sharpe" in exps.columns and not exps.empty else 0
    best_dd = exps["max_drawdown"].max() if "max_drawdown" in exps.columns and not exps.empty else 0

    st.subheader("🏆 Ana hedef: kazanan bir strateji")
    pct = min(100, int((target_found / target_goal) * 100)) if target_goal else 0
    st.markdown(f"<div class='goal-track'><div class='goal-fill' style='width:{pct}%'></div></div>", unsafe_allow_html=True)
    st.caption(f"{target_found} / {target_goal} kazanan bulundu")

    st.divider()
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("📈 En iyi CAGR")
        cagr_pct = min(100, int((best_cagr / 0.60) * 100)) if best_cagr > 0 else 0
        st.markdown(f"<div class='goal-track'><div class='goal-fill' style='width:{cagr_pct}%'></div></div>", unsafe_allow_html=True)
        st.caption(f"{best_cagr:.1%} / 60% hedef")
    with c2:
        st.subheader("⚖️ En iyi Sharpe")
        sh_pct = min(100, int((best_sharpe / 1.5) * 100)) if best_sharpe > 0 else 0
        st.markdown(f"<div class='goal-track'><div class='goal-fill' style='width:{sh_pct}%'></div></div>", unsafe_allow_html=True)
        st.caption(f"{best_sharpe:.2f} / 1.50 hedef")
    with c3:
        st.subheader("🛡️ En iyi DD")
        dd_ok = best_dd >= -0.15
        st.markdown(f"<div class='goal-track'><div class='goal-fill' style='width:{100 if dd_ok else 50}%'></div></div>", unsafe_allow_html=True)
        st.caption(f"{best_dd:.1%} / -15% sınır")

    st.divider()
    st.subheader("🏆 Kazanan stratejiler")
    if not winners:
        st.info("Henüz kazanan yok. Otonom loop arıyor.")
    else:
        st.dataframe(pd.DataFrame(winners), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# 📦 POD'LAR
# ---------------------------------------------------------------------------

elif PAGE == "📦 Pod'lar":
    st.title("📦 Pod sermaye dağılımı")
    st.caption("Millennium/Citadel tarzı · %5 DD → halve · %7.5 DD → retire")

    if not pods:
        st.info("Henüz pod yok. Bir strateji promote edilince pod açılır.")
    else:
        df = pd.DataFrame(pods)
        status_counts = df["status"].value_counts().to_dict() if "status" in df.columns else {}
        c1, c2, c3 = st.columns(3)
        c1.metric("Aktif", status_counts.get("active", 0))
        c2.metric("Halve'lenmiş", status_counts.get("halved", 0))
        c3.metric("Emekli", status_counts.get("retired", 0))

        rename_map = {
            "strategy_family": "aile", "market": "market",
            "allocated_capital": "tahsis $", "current_capital": "mevcut $",
            "peak_capital": "zirve $", "drawdown_pct": "DD %",
            "sharpe_30d": "Sharpe 30g", "sortino_30d": "Sortino 30g",
            "trades_30d": "trade 30g", "win_rate_30d": "WR 30g",
            "correlation_to_book": "korelasyon", "status": "durum",
        }
        cols = [c for c in rename_map if c in df.columns]
        st.dataframe(df[cols].rename(columns=rename_map), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# 📡 CANLI AKIŞ
# ---------------------------------------------------------------------------

elif PAGE == "📡 Canlı Akış":
    st.title("📡 Canlı aktivite")

    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader("autonomous.log (son 120 satır)")
        log_text = tail_log(120)
        st.text_area("log", value=log_text, height=620, label_visibility="collapsed")

    with c2:
        st.subheader("🧠 Son hipotezler")
        h = hypotheses_df(8)
        if not h.empty:
            cols = [c for c in ["timestamp", "title", "strategy_family", "market"] if c in h.columns]
            st.dataframe(h[cols].rename(columns={
                "timestamp": "zaman", "title": "başlık",
                "strategy_family": "aile", "market": "market"
            }), hide_index=True, use_container_width=True)
        else:
            st.caption("henüz yok")

        st.subheader("⚖️ Son kararlar")
        d = decisions_df(8)
        if not d.empty:
            cols = [c for c in ["timestamp", "decision"] if c in d.columns]
            st.dataframe(d[cols].rename(columns={
                "timestamp": "zaman", "decision": "karar"
            }), hide_index=True, use_container_width=True)
        else:
            st.caption("henüz yok")

        st.subheader("🗣️ Son debate oturumları")
        b = debates_df(5)
        if not b.empty:
            for _, row in b.iterrows():
                with st.expander(f"{str(row.get('topic', ''))[:70]}"):
                    st.markdown(f"**Sonuç:** {row.get('conclusion', '')}")
                    args = row.get("arguments", [])
                    if isinstance(args, list):
                        for a in args:
                            if isinstance(a, dict):
                                st.markdown(
                                    f"- **{a.get('agent_name', '?')}** `[{a.get('position', '?')}]` — {a.get('reasoning', '')}"
                                )
        else:
            st.caption("henüz yok")

# ---------------------------------------------------------------------------
# Otomatik yenileme
# ---------------------------------------------------------------------------

if auto_refresh:
    import time as _t
    _t.sleep(15)
    st.rerun()
