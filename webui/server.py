"""FastAPI dashboard — premium tema (Linear/Vercel tarzı).

Endpoints:
    GET  /                    — ana dashboard HTML
    GET  /api/stats           — KPI özet
    GET  /api/agents          — ajan listesi + profil detayları
    GET  /api/org             — organizasyon şeması (node + edge)
    GET  /api/workflow        — iş akışı graph
    GET  /api/experiments     — son 500 deney (filtrelenebilir)
    GET  /api/winners         — kazanan stratejiler
    GET  /api/pods            — pod state'leri
    GET  /api/activity        — son hipotezler / kararlar / debate'ler
    GET  /api/log             — autonomous.log tail
    GET  /api/goals           — hedef ilerleme

Çalıştırma:
    .venv\\Scripts\\python.exe -m uvicorn webui.server:app --port 8501 --host 0.0.0.0
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

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from oto_bot.memory.journal import LearningJournal
from oto_bot.memory.curve import LearningCurve
from oto_bot.agents.hr import HRManager
from oto_bot.agents.registry import AgentRegistry
from oto_bot.agents.robustness import RobustnessQueue, generate_variants
from oto_bot.agents.proposals import Proposal, ProposalQueue
from oto_bot.agents.git_research import HermesGitResearcher
from oto_bot.agents.curriculum import TalosCurriculumLoader
from oto_bot.agents.educator_loop import EducatorLoop
from oto_bot.agents.settings import load_settings, save_settings, AppSettings
from oto_bot.agents.auto_approver import AutoApprover
from dataclasses import asdict

ROOT = _ROOT
AGENTS_FILE = ROOT / "memories" / "agents.json"
PODS_FILE = ROOT / "memories" / "pods.json"
DB_FILE = ROOT / "artifacts" / "experiments.sqlite3"
WINNERS_FILE = ROOT / "artifacts" / "winners.jsonl"
LOG_FILE = ROOT / "logs" / "autonomous.log"
ORG_YAML = ROOT / "configs" / "organization.yaml"
CLAUDE_AGENTS_DIR = ROOT / ".claude" / "agents"

WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))

app = FastAPI(title="oto-bot control room")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")


# ---------------------------------------------------------------------------
# Ajan profilleri (hardcoded — her ajan için ne yaptığı + temel özellikleri)
# ---------------------------------------------------------------------------

AGENT_PROFILES: dict[str, dict[str, Any]] = {
    "Atlas CEO": {
        "icon": "👑", "color": "#fbbf24",
        "what_does": "Laboratuvarın Head of Trading'i. Tüm ajanlardan gelen bilgiyi birleştirir, sermaye dağıtır, promote/reject kararını verir. BofA / Citadel doktrininde düşünür.",
        "features": [
            "Sabah briefing üretir (rejim + makro + book riski)",
            "Investment committee + Risk committee başkanı",
            "Pod sermaye dağılımını Sharpe/DD/korelasyona göre rebalance",
            "Panel debate sonucu + eşik + trend → promote / hold / iterate / reject",
            "Hire/fire yetkisi: specialist oluşturur, performans düşeni retire eder",
        ],
    },
    "Iris ChiefOfStaff": {
        "icon": "📋", "color": "#fbbf24",
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
        "icon": "🔭", "color": "#60a5fa",
        "what_does": "Piyasa taraması yapar. 25+ sembolü her döngüde rejim, volatilite, ADX, BB genişliği ve hacim açısından değerlendirir; hangi coin'in/pazarın hangi stratejiye uygun olduğunu söyler.",
        "features": [
            "Her sembol için FAVORABLE / NEUTRAL / UNFAVORABLE",
            "Rejim (bull/bear/sideways) + ATR% + ADX",
            "Bollinger genişliği + hacim trendi",
            "Çıktı: artifacts/market_intel.json",
        ],
    },
    "Nova StrategyRND": {
        "icon": "💡", "color": "#60a5fa",
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
        "icon": "📊", "color": "#60a5fa",
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
        "icon": "🌐", "color": "#60a5fa",
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
        "icon": "🎯", "color": "#60a5fa",
        "what_does": "Her pazarın rejimini sınıflar (trend_up/down, range, high_vol, crisis). ADX + EMA + ATR + BB kombinasyonunu kullanır, önceki 5 barı pekiştirme için okur.",
        "features": [
            "6 rejim etiketi + 0-1 güven skoru",
            "Strateji-rejim fit matrisi",
            "prior_regime takibi — değişim CEO'ya bildirilir",
            "regime_age_bars: rejim ne kadar sürdü",
            "Çıktı: artifacts/regime_snapshot.json",
        ],
    },
    "Helix Backtest": {
        "icon": "⚗️", "color": "#f59e0b",
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
        "icon": "💥", "color": "#f59e0b",
        "what_does": "İsimli stres senaryolarını çalıştırır. 6 tarihsel şok (COVID-2020, Luna-2022, FTX, Flash Crash, Slow Bleed, Vol Compression) uygular ve hayatta kalma kriterini test eder.",
        "features": [
            "Her senaryo: price shock + vol multiplier + correlation jump",
            "Hayatta kalma: DD ≥ -25% + kill-switch atmamış",
            "Promote için min 3 senaryo SURVIVED şart",
            "Çıktı: artifacts/stress_results/",
        ],
    },
    "Sentinel Risk": {
        "icon": "🛡️", "color": "#ef4444",
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
        "icon": "⚖️", "color": "#ef4444",
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
        "icon": "🔮", "color": "#ef4444",
        "what_does": "Promosyon öncesi başarısızlık senaryoları üretir. 'Bu strateji 3 ay sonra nasıl çöker?' — 10 başlıklı failure taksonomisi tarar.",
        "features": [
            "Overfitting / regime fragility / sample size",
            "Low-edge / WR trap / correlation leak",
            "Execution naivety / tail blindness",
            "Capacity / data leakage",
            "0-100 risk skoru → RED / AMBER / GREEN",
        ],
    },
    "Forge Execution": {
        "icon": "🔥", "color": "#34d399",
        "what_does": "Onaylanan stratejileri paper trading olarak çalıştırır. Margin takibi, slippage/latency simülasyonu, kill-switch, real-time P&L.",
        "features": [
            "Sabit $600 notional, prob-based leverage",
            "+0.05% ekstra slippage",
            "Real-time pozisyon + P&L tracking",
            "Kill-switch kitap DD -%10'u aşarsa",
            "Çıktı: artifacts/paper_trading_state.json",
        ],
    },
    "Tariq TCA": {
        "icon": "📐", "color": "#34d399",
        "what_does": "Transaction cost analysis. Her emir için arrival slippage, implementation shortfall, market impact, fees, latency ölçer.",
        "features": [
            "Her emir için ExecutionReport",
            "Avg slippage 15bps üstü → uyarı",
            "Avg market impact 25bps üstü → alert",
            "Simüle vs gerçek fill karşılaştırması",
            "Çıktı: artifacts/tca/<date>.jsonl",
        ],
    },
    "Ledger Allocator": {
        "icon": "💰", "color": "#34d399",
        "what_does": "Pod bazlı sermaye dağıtıcı. Her stratejiye sleeve açar, Sharpe/DD'ye göre günlük rebalance yapar, %5 DD'de yarıya indirir, %7.5'te kapatır.",
        "features": [
            "Millennium/Citadel tarzı pod modeli",
            "Otomatik halve / retire tetikleri",
            "Sharpe-ağırlıklı, DD-cezalı dağıtım",
            "< %5'lik mikro değişiklik atlanır",
            "Çıktı: memories/pods.json",
        ],
    },
    "Archive Memory": {
        "icon": "📚", "color": "#a78bfa",
        "what_does": "Kurumsal hafıza yöneticisi. Eski deneyleri özetler, sıkıştırır; hiçbir bilgi kaybolmasın ama token da boşa gitmesin.",
        "features": [
            "Son 100 deneyi tam ayrıntıyla tutar",
            "30 gün öncesi → aggregate özet",
            "artifacts/experiment_index.json",
            "Duplicate / bozuk kayıt temizliği",
            "Çıktı: SQLite + JSON index",
        ],
    },
    "Pulse Analytics": {
        "icon": "📈", "color": "#f472b6",
        "what_does": "Performans analitik ajanı. Strateji versiyonlarını karşılaştırır, improvement trendleri çıkarır, scorecard üretir.",
        "features": [
            "Versiyon karşılaştırma",
            "Coin rank (en kârlı/zararlı)",
            "Rejim bazlı performans",
            "Risk-adjusted ranking (Sharpe birinci)",
            "Çıktı: artifacts/scorecard.json",
        ],
    },
    "Ledger Attribution": {
        "icon": "🧾", "color": "#f472b6",
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


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _conn() -> sqlite3.Connection | None:
    if not DB_FILE.exists():
        return None
    con = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _parse_rows(rows: list[sqlite3.Row]) -> list[dict]:
    out = []
    for r in rows:
        rdict = dict(r)
        try:
            data = json.loads(rdict["data"]) if isinstance(rdict.get("data"), str) else rdict.get("data")
        except Exception:
            data = {}
        merged = {
            "id": rdict.get("id"),
            "timestamp": rdict.get("timestamp"),
            "category": rdict.get("category"),
            "agent_id": rdict.get("agent_id"),
        }
        if isinstance(data, dict):
            # Orchestrator sarmalını aç: `data.result` içindeki ExperimentResult'ı top-level'a taşı
            merged.update(data)
            inner = data.get("result")
            if isinstance(inner, dict):
                # Result alanları override eder (daha doğru)
                for k, v in inner.items():
                    merged[k] = v
            # Eğer strategy_params result'ta yoksa ama wrapper'da "params" varsa o'nu kullan
            if not merged.get("strategy_params") and isinstance(data.get("params"), dict):
                merged["strategy_params"] = data["params"]
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/stats")
async def stats():
    con = _conn()
    out = {"experiments": 0, "decisions": 0, "hypotheses": 0, "debates": 0}
    if con:
        for table, key in [
            ("experiments", "experiments"),
            ("decisions", "decisions"),
            ("hypotheses", "hypotheses"),
            ("debate_records", "debates"),
        ]:
            out[key] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    agents = _load_json(AGENTS_FILE, default=[])
    pods = _load_json(PODS_FILE, default=[])
    winners = _load_jsonl(WINNERS_FILE)

    out["agents_total"] = len(agents)
    out["agents_active"] = sum(1 for a in agents if a.get("active"))
    out["pods_total"] = len(pods)
    out["pods_active"] = sum(1 for p in pods if p.get("status") == "active")
    out["winners"] = len(winners)

    # Promoted experiment count + avg sharpe (last 500)
    if con:
        rows = _parse_rows(con.execute("SELECT * FROM experiments ORDER BY timestamp DESC LIMIT 500").fetchall())
        promoted = sum(1 for r in rows if r.get("promoted"))
        sharpes = [float(r["sharpe"]) for r in rows if r.get("sharpe") is not None]
        out["experiments_promoted"] = promoted
        out["avg_sharpe_recent"] = round(sum(sharpes) / len(sharpes), 3) if sharpes else 0.0
    else:
        out["experiments_promoted"] = 0
        out["avg_sharpe_recent"] = 0.0

    return out


def _specialist_profile(name: str, role: str, department: str, mandate: str, metadata: dict) -> dict:
    """Auto-hired specialist için dinamik profil üret. name formatı: Nova-day-us_equities gibi."""
    parts = name.split("-")
    prefix = parts[0] if parts else "Specialist"

    PREFIX_META = {
        "Nova": ("💡", "#E3703C", "Strateji R&D"),
        "Oracle": ("🎯", "#7FB0D0", "Rejim Uzmanı"),
        "Sigma": ("📊", "#B8A7D0", "Kantitatif Analiz"),
        "Atlas": ("👑", "#E8B464", "Yönetici"),
    }
    icon, color, category = PREFIX_META.get(prefix, ("🤖", "#8F897D", "Uzman"))

    scope_str = " / ".join(parts[1:]).upper() if len(parts) > 1 else "genel"
    reason = metadata.get("reason") or metadata.get("scope") or "İş yükü yoğunluğu"
    hired_cycle = metadata.get("hired_at_cycle", "?")

    # Mandate'ten feature üret
    features = [
        f"Scope: {scope_str}",
        f"Departman: {department}",
        f"Cycle {hired_cycle}'da CEO tarafından işe alındı",
    ]
    if metadata.get("failure_rate") is not None:
        features.append(f"Gözlemlenen başarısızlık oranı: %{int(metadata['failure_rate'] * 100)}")
    if metadata.get("scope"):
        features.append(f"Odak: {metadata['scope']} kombinasyonu")
    if metadata.get("regime"):
        features.append(f"Rejim: {metadata['regime']}")
    if metadata.get("strategy_family"):
        features.append(f"Strateji ailesi: {metadata['strategy_family']}")

    what_does = mandate or (
        f"{prefix} soyundan otomatik oluşturulmuş {category.lower()}. "
        f"Ana ajan yetersiz kaldığı {scope_str} dar alanına odaklanır."
    )

    return {
        "icon": icon, "color": color,
        "what_does": what_does,
        "features": features,
    }


@app.get("/api/agents")
async def api_agents():
    agents = _load_json(AGENTS_FILE, default=[])
    out = []
    for a in agents:
        name = a.get("name", "")
        if name in AGENT_PROFILES:
            profile = AGENT_PROFILES[name]
        else:
            # Auto-hired specialist — dinamik profil üret
            profile = _specialist_profile(
                name=name,
                role=a.get("role", ""),
                department=a.get("department", ""),
                mandate=a.get("mandate", ""),
                metadata=a.get("metadata", {}),
            )
        out.append({
            "agent_id": a.get("agent_id"),
            "name": name,
            "role": a.get("role"),
            "department": a.get("department"),
            "mandate": a.get("mandate"),
            "active": a.get("active", True),
            "created_at": a.get("created_at"),
            "icon": profile["icon"],
            "color": profile["color"],
            "what_does": profile["what_does"],
            "features": profile["features"],
        })
    return out


@app.get("/api/org")
async def api_org():
    org = {}
    if ORG_YAML.exists():
        org = yaml.safe_load(ORG_YAML.read_text(encoding="utf-8")) or {}

    DEPT_COLORS = {
        "Executive": "#fbbf24", "Research": "#60a5fa", "Simulation": "#f59e0b",
        "Governance": "#ef4444", "Execution": "#34d399",
        "Knowledge": "#a78bfa", "Analytics": "#f472b6",
    }
    nodes = [{"id": "CEO", "label": "Atlas CEO\n(Head of Trading)", "group": "Executive", "color": "#fbbf24", "size": 40}]
    edges = []

    depts = org.get("departments", {})
    for dept_name, members in depts.items():
        if dept_name == "Executive" or not isinstance(members, list):
            continue
        color = DEPT_COLORS.get(dept_name, "#9ca3af")
        nodes.append({
            "id": f"DEPT_{dept_name}", "label": dept_name,
            "group": dept_name, "color": color, "size": 28, "shape": "box",
        })
        edges.append({"from": "CEO", "to": f"DEPT_{dept_name}"})
        for m in members:
            if m == "Atlas CEO":
                continue
            nodes.append({
                "id": m, "label": m, "group": dept_name, "color": color, "size": 18,
            })
            edges.append({"from": f"DEPT_{dept_name}", "to": m})
    # Executive members
    for m in depts.get("Executive", []):
        if m == "Atlas CEO":
            continue
        nodes.append({
            "id": m, "label": m, "group": "Executive", "color": "#fbbf24", "size": 22,
        })
        edges.append({"from": "CEO", "to": m})

    return {"nodes": nodes, "edges": edges, "committees": org.get("committees", {})}


@app.get("/api/workflow")
async def api_workflow():
    nodes = [
        {"id": "vega",      "label": "Vega\nMarket Intel",   "color": "#60a5fa", "size": 24, "group": "research"},
        {"id": "mercury",   "label": "Mercury\nMacro",       "color": "#60a5fa", "size": 24, "group": "research"},
        {"id": "regime",    "label": "Regime\nOracle",       "color": "#60a5fa", "size": 24, "group": "research"},
        {"id": "nova",      "label": "Nova\nHipotez",        "color": "#3b82f6", "size": 30, "group": "research", "shape": "box"},
        {"id": "helix",     "label": "Helix\nBacktest",      "color": "#f59e0b", "size": 30, "group": "sim", "shape": "box"},
        {"id": "shock",     "label": "Shockwave\nStres",     "color": "#f59e0b", "size": 24, "group": "sim"},
        {"id": "sentinel",  "label": "Sentinel\nRisk Gate",  "color": "#ef4444", "size": 24, "group": "gov"},
        {"id": "apex",      "label": "Apex\nPortfolio Risk", "color": "#ef4444", "size": 28, "group": "gov", "shape": "hexagon"},
        {"id": "cassandra", "label": "Cassandra\nPre-Mortem","color": "#ef4444", "size": 24, "group": "gov"},
        {"id": "debate",    "label": "8-sesli\nPANEL",       "color": "#a78bfa", "size": 34, "group": "panel", "shape": "hexagon"},
        {"id": "ceo",       "label": "Atlas CEO\nkarar",     "color": "#fbbf24", "size": 38, "group": "exec"},
        {"id": "ledger",    "label": "Ledger\nSermaye",      "color": "#34d399", "size": 24, "group": "exec"},
        {"id": "forge",     "label": "Forge\nPaper Exec",    "color": "#34d399", "size": 24, "group": "exec"},
        {"id": "tariq",     "label": "Tariq\nTCA",           "color": "#34d399", "size": 22, "group": "exec"},
        {"id": "pulse",     "label": "Pulse\nAnalitik",      "color": "#f472b6", "size": 22, "group": "analytics"},
        {"id": "attr",      "label": "Ledger\nPnL Attr",     "color": "#f472b6", "size": 22, "group": "analytics"},
        {"id": "archive",   "label": "Archive\nHafıza",      "color": "#a78bfa", "size": 24, "group": "knowledge", "shape": "box"},
    ]
    edges = [
        {"from": "vega", "to": "nova"}, {"from": "mercury", "to": "nova"}, {"from": "regime", "to": "nova"},
        {"from": "nova", "to": "helix"},
        {"from": "helix", "to": "shock"}, {"from": "helix", "to": "sentinel"},
        {"from": "helix", "to": "apex"}, {"from": "helix", "to": "cassandra"},
        {"from": "sentinel", "to": "debate"}, {"from": "apex", "to": "debate"},
        {"from": "cassandra", "to": "debate"}, {"from": "shock", "to": "debate"},
        {"from": "mercury", "to": "debate"},
        {"from": "debate", "to": "ceo"},
        {"from": "ceo", "to": "ledger"},
        {"from": "ledger", "to": "forge"}, {"from": "forge", "to": "tariq"},
        {"from": "tariq", "to": "pulse"}, {"from": "pulse", "to": "attr"},
        {"from": "attr", "to": "archive"},
        {"from": "archive", "to": "nova", "color": "#555", "dashes": True},
        {"from": "ceo", "to": "nova", "color": "#555", "dashes": True},
    ]
    return {"nodes": nodes, "edges": edges}


import hashlib

BOT_NICKNAMES = [
    "Atlas", "Nova", "Orion", "Vega", "Sirius", "Polaris", "Andromeda", "Lyra",
    "Phoenix", "Hydra", "Leo", "Draco", "Apollo", "Helios", "Zephyr", "Titan",
    "Odin", "Thor", "Hermes", "Kairos", "Pallas", "Ceres", "Juno", "Minerva",
]


def _bot_fingerprint(strategy_family: str, params: dict | None) -> str:
    """Her (aile + param) setine benzersiz stabil hash."""
    params = params or {}
    # Stable serialisation
    payload = f"{strategy_family}|" + "|".join(
        f"{k}={params[k]}" for k in sorted(params.keys())
    )
    h = hashlib.md5(payload.encode("utf-8")).hexdigest()
    return h[:10]


def _bot_name(strategy_family: str, params: dict | None) -> str:
    fp = _bot_fingerprint(strategy_family, params)
    # Deterministic nickname selection from hash
    idx = int(fp[:4], 16) % len(BOT_NICKNAMES)
    nickname = BOT_NICKNAMES[idx]
    fam_cap = (strategy_family or "bot").capitalize()
    return f"{nickname}-{fam_cap}-{fp[:6]}"


def _project_12m(cagr: float, current_capital: float = 10_000.0) -> dict[str, float]:
    """CAGR'a göre 12, 36, 60 aylık projeksiyon."""
    out = {}
    for months in (1, 3, 6, 12, 24, 60):
        years = months / 12.0
        # CAGR could be 0 or negative; handle edge
        try:
            factor = (1 + cagr) ** years if (1 + cagr) > 0 else 0
        except Exception:
            factor = 0
        out[f"m{months}"] = round(current_capital * factor, 2)
    return out


def _enrich_experiment(r: dict, start_capital: float = 10_000.0) -> dict:
    """Her deney için USD simulation + duration + derived tradestats."""
    trades = int(r.get("total_trades") or 0)
    wr = float(r.get("win_rate") or 0)
    roi = float(r.get("roi") or 0)
    cagr = float(r.get("cagr") or 0)
    tf = (r.get("bar_timeframe") or "1h").lower()
    bars = int(r.get("duration_bars") or 0)

    r["winning_trades"] = round(trades * wr)
    r["losing_trades"] = max(0, trades - r["winning_trades"])
    r["fees_est_usd"] = round(trades * 2 * 600 * 0.0015, 2)

    # $X initial → final
    r["start_capital_usd"] = start_capital
    r["final_capital_usd"] = round(start_capital * (1 + roi), 2)
    r["profit_usd"] = round(r["final_capital_usd"] - start_capital, 2)

    # Duration (months) — either from duration_bars + timeframe OR via cagr/roi
    tf_minutes = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "4h": 240, "1d": 1440,
    }.get(tf, 60)
    if bars > 0:
        total_minutes = bars * tf_minutes
        months = total_minutes / (60 * 24 * 30)
        r["duration_months_est"] = round(months, 1)
    elif cagr != 0 and abs(cagr + 1) > 1e-9 and abs(roi + 1) > 1e-9:
        import math
        try:
            years = math.log(1 + roi) / math.log(1 + cagr) if (1 + roi) > 0 and (1 + cagr) > 0 else 0
            r["duration_months_est"] = round(years * 12, 1) if years > 0 else 0
        except Exception:
            r["duration_months_est"] = 0
    else:
        r["duration_months_est"] = 0

    # Dollar-denominated trade stats (based on $10k capital)
    # Approximate: largest_win_pct already "per-trade return" pct; convert to USD on notional $600/trade
    notional = 600.0
    r["largest_win_usd_est"] = round(float(r.get("largest_win_pct") or 0) * notional, 2)
    r["largest_loss_usd_est"] = round(float(r.get("largest_loss_pct") or 0) * notional, 2)
    r["avg_win_usd_est"] = round(float(r.get("avg_win_pct") or 0) * notional, 2)
    r["avg_loss_usd_est"] = round(float(r.get("avg_loss_pct") or 0) * notional, 2)

    # Duration breakdown: days + hours
    months = r.get("duration_months_est", 0) or 0
    r["duration_days"] = round(months * 30.4, 1)
    r["duration_hours"] = round(months * 30.4 * 24, 1)
    if months >= 12:
        r["duration_human"] = f"{months/12:.1f} yıl"
    elif months >= 1:
        r["duration_human"] = f"{months:.1f} ay"
    elif months * 30.4 >= 1:
        r["duration_human"] = f"{months*30.4:.1f} gün"
    else:
        r["duration_human"] = f"{months*30.4*24:.1f} saat"

    # Bot fingerprint + name
    r["bot_id"] = _bot_fingerprint(r.get("strategy_family", ""), r.get("strategy_params"))
    r["bot_name"] = _bot_name(r.get("strategy_family", ""), r.get("strategy_params"))

    # 12-month projections
    r["projection"] = _project_12m(float(r.get("cagr") or 0), start_capital)

    return r


@app.get("/api/experiments")
async def api_experiments(limit: int = Query(500, le=2000), start_capital: float = 10_000):
    con = _conn()
    if con is None:
        return []
    rows = con.execute("SELECT * FROM experiments ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    parsed = _parse_rows(rows)
    for r in parsed:
        _enrich_experiment(r, start_capital)
    return parsed


STRATEGY_PHILOSOPHIES: dict[str, dict[str, Any]] = {
    "day": {
        "title": "Day Trader — Momentum günlük",
        "tagline": "1-4 saatlik bar'da momentum sürer; günlük kapatır.",
        "how_it_works": (
            "Fast MA (20) yavaş MA (50) üzerine geçtiğinde alır, RSI aşırı alımda tepki alır. "
            "ATR × multiplier ile stop-loss ve take-profit koyar. Golden cross sonrası sinyal "
            "sıklığı artar; death cross'ta short tarafı aktif olur."
        ),
        "favorable_regimes": ["trend_up", "trend_down"],
        "killer_regimes": ["range", "high_vol"],
        "typical_holding": "Birkaç saat — bar başına max 1-2 gün",
        "entry_signals": [
            "EMA20 EMA50 üzerinde (uptrend konfirm)",
            "RSI 30-50 arası (momentum'da toparlanma)",
            "Hacim 20-bar ortalamasının üstünde",
            "ATR genişliği normalin altında (stop yeri anlamlı)",
        ],
        "exit_signals": [
            "ATR × 1.5-2 trailing stop",
            "RSI 70 üstü + fiyat ters dönüş",
            "End of trading session",
        ],
        "key_params": ["fast_ma", "slow_ma", "rsi_period", "atr_multiplier_sl", "atr_multiplier_tp"],
    },
    "swing": {
        "title": "Swing Trader — Trend takibi",
        "tagline": "4h + 1d timeframe'de orta-vadeli trend.",
        "how_it_works": (
            "SMA crossover + RSI divergence + support/resistance. Haftalarca pozisyonda kalır. "
            "Trend teyit edilmeden giriş yapmaz; stop'u ATR × 3 koyar (gürültüden kaçın). "
            "Max 3 concurrent swing tutar."
        ),
        "favorable_regimes": ["trend_up", "trend_down"],
        "killer_regimes": ["range", "crisis"],
        "typical_holding": "3-10 gün",
        "entry_signals": [
            "SMA50 SMA200 üzerinde (major uptrend)",
            "Bullish RSI divergence veya breakout",
            "Support seviyesinden reaksiyon",
            "Hacim teyidi",
        ],
        "exit_signals": [
            "Trailing ATR × 3",
            "Bearish divergence",
            "SMA50 altına iniş",
        ],
        "key_params": ["sma_fast", "sma_slow", "rsi_period", "atr_multiplier"],
    },
    "scalp": {
        "title": "Scalper — Mean reversion (kısa)",
        "tagline": "5-15 dakikalık BB ekstremlerinden geri dönüş.",
        "how_it_works": (
            "Bollinger Band alt/üst bandına değdiğinde reversal bekler. RSI ekstrem + VWAP sapması "
            "teyit olarak kullanılır. Giriş anından max 1 saat içinde çıkar. ADX <25 olduğu "
            "range rejimlerinde parlar; trend piyasasında öldürücü kayıplar verir."
        ),
        "favorable_regimes": ["range", "high_vol"],
        "killer_regimes": ["trend_up", "trend_down", "crisis"],
        "typical_holding": "5-60 dakika",
        "entry_signals": [
            "BB alt banda değme + RSI < 30 (long)",
            "BB üst banda değme + RSI > 70 (short)",
            "ADX < 25 (range konfirm)",
            "Hacim surge oversold teyidi",
        ],
        "exit_signals": [
            "Orta banda dönüş (mean)",
            "Stop: bandın ötesinde %0.5 daha",
            "Vakit: 1 saat",
        ],
        "key_params": ["lookback", "z_entry", "z_exit", "bb_period", "bb_std", "atr_multiplier_sl"],
    },
}


@app.get("/api/strategy-philosophy/{family}")
async def api_strategy_philosophy(family: str):
    return STRATEGY_PHILOSOPHIES.get(family, {"title": family, "tagline": "(tanım yok)"})


@app.get("/api/strategy-philosophies")
async def api_strategy_philosophies():
    return STRATEGY_PHILOSOPHIES


@app.get("/api/hiring")
async def api_hiring():
    """HR geçmişi + departman kapasite özeti."""
    registry = AgentRegistry()
    hr = HRManager(registry=registry)
    events = hr.recent_events(limit=100)
    summary = hr.summary()

    # Departman kapasite sayıları
    agents = registry.active()
    dept_counts: dict[str, int] = {}
    for a in agents:
        dept_counts[a.department] = dept_counts.get(a.department, 0) + 1

    # Specialists: isimlerinde "-" olan (Nova-Scalp-Forex gibi)
    specialists = [
        {
            "name": a.name, "role": a.role, "department": a.department,
            "mandate": a.mandate, "hired_at_cycle": a.metadata.get("hired_at_cycle", 0),
        }
        for a in agents if "-" in a.name
    ]

    return {
        "summary": summary,
        "events": events,
        "department_counts": dept_counts,
        "specialists": specialists,
        "policy": {
            "review_interval_cycles": 50,
            "rules": [
                "Scope failure rate > 70% over 50+ cycles → hire Nova specialist",
                "Crisis/high_vol regime > 20% share → hire Oracle specialist",
                "Strategy family with ≤3 lessons per 200+ cycles → hire Sigma deep specialist",
                "Specialist with 0 lessons for 200 cycles → retire",
            ],
        },
    }


@app.get("/api/proposals")
async def api_proposals(status: str | None = None, limit: int = 200):
    pq = ProposalQueue()
    data = pq.all(status=status, limit=limit) if status else pq.pending(limit=limit)
    counts = pq.counts()
    return {"proposals": data, "counts": counts}


@app.post("/api/proposals/decide")
async def api_proposals_decide(proposal_id: str, decision: str, reason: str = ""):
    pq = ProposalQueue()
    ok = pq.decide(proposal_id, decision, reason=reason, by="human")
    return {"ok": ok}


@app.post("/api/git-research/scan")
async def api_git_research_scan(use_llm: bool = False, max_repos: int = 15):
    hermes = HermesGitResearcher(use_llm=use_llm)
    result = hermes.scan(max_repos=max_repos)
    return result


@app.post("/api/curriculum/ingest-borsaninizinden")
async def api_curriculum_borsaninizinden(use_llm: bool = False):
    talos = TalosCurriculumLoader(use_llm=use_llm)
    return talos.ingest_borsaninizinden()


# ---------------------------------------------------------------------------
# Faz 7 — Eğitim daemon (Talos + Hermes daimi mode) status endpoint
# ---------------------------------------------------------------------------

# Webui process'i içinde global bir EducatorLoop tutmuyoruz — orchestrator
# kendi process'inde ayrıca bir tane çalıştırıyor (ana laboratuvar). Webui
# kullanıcıya artifacts üzerinden okunabilir state'i ve kendi opsiyonel
# read-only örneğini sunar.

_educator_singleton: EducatorLoop | None = None


def _get_or_create_webui_educator() -> EducatorLoop:
    """Webui için tembelce yaratılan, başlatılmamış educator örneği.

    Sadece status hesaplaması için lesson sayıları ve recent preview üretmek
    üzere kullanılır. Webui process'i ayrı bir thread başlatmaz; arka plan
    eğitim daemon'u orchestrator process'inde koşar.
    """
    global _educator_singleton
    if _educator_singleton is None:
        _educator_singleton = EducatorLoop()
    return _educator_singleton


@app.get("/api/educator/status")
async def api_educator_status() -> dict[str, Any]:
    """Talos + Hermes daimi loop telemetrisi.

    İçerik:
        - thread alive flag'leri (orchestrator çalışıyorsa True)
        - son ingest / discover zamanı
        - toplam ders / proposal sayısı (her ajan için)
        - son 5 ders preview
        - curriculum_state.json + git_research_state.json snapshot
    """
    payload: dict[str, Any] = {}
    try:
        edu = _get_or_create_webui_educator()
        payload.update(edu.status())
    except Exception as exc:
        payload["status_error"] = f"{type(exc).__name__}: {exc}"

    # Orchestrator heartbeat içindeki educator alanı (gerçek thread state)
    try:
        hb_path = ROOT / "artifacts" / "current_cycle.json"
        if hb_path.exists():
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            if "educator" in hb:
                payload["orchestrator_educator"] = hb["educator"]
    except Exception:
        pass

    # State file snapshots
    try:
        cstate = ROOT / "artifacts" / "curriculum_state.json"
        if cstate.exists():
            payload["curriculum_state"] = json.loads(cstate.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        gstate = ROOT / "artifacts" / "git_research_state.json"
        if gstate.exists():
            payload["git_research_state"] = json.loads(gstate.read_text(encoding="utf-8"))
    except Exception:
        pass

    return payload


@app.get("/api/settings")
async def api_settings_get():
    s = load_settings()
    return asdict(s)


@app.post("/api/settings")
async def api_settings_post(
    auto_approve: bool | None = None,
    auto_approve_delay_minutes: int | None = None,
    auto_reject_critical: bool | None = None,
    hermes_auto_scan: bool | None = None,
):
    s = load_settings()
    if auto_approve is not None:
        s.auto_approve = auto_approve
    if auto_approve_delay_minutes is not None:
        s.auto_approve_delay_minutes = int(auto_approve_delay_minutes)
    if auto_reject_critical is not None:
        s.auto_reject_critical = auto_reject_critical
    if hermes_auto_scan is not None:
        s.hermes_auto_scan = hermes_auto_scan
    save_settings(s)
    return asdict(s)


@app.post("/api/auto-approve/trigger")
async def api_auto_approve_trigger():
    aa = AutoApprover()
    return aa.run()


@app.get("/api/executive-summary")
async def api_executive_summary(hours: int = 1, start_capital: float = 10_000):
    """Yönetici özeti."""
    from datetime import datetime, timezone, timedelta
    con = _conn()
    if con is None:
        return {"error": "no db"}

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()

    # Son N saatin aktivitesi
    recent_exp = con.execute(
        "SELECT data FROM experiments WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 500",
        (cutoff,),
    ).fetchall()
    recent_dec = con.execute(
        "SELECT data FROM decisions WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 500",
        (cutoff,),
    ).fetchall()

    cycles_done = len(recent_exp)
    decisions_made = len(recent_dec)
    promoted_ct = 0
    total_pnl_recent = 0.0
    rejected_short = 0
    for r in recent_exp:
        try:
            d = json.loads(r["data"])
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
            if inner.get("promoted"):
                promoted_ct += 1
            roi = inner.get("roi") or 0
            total_pnl_recent += start_capital * roi * 0.01  # approximate
        except Exception:
            pass

    # Bot listesi — en iyi 5 (linear_score descending, sonra avg_sharpe)
    bots_resp = await api_bots(start_capital=start_capital, limit=2000)  # reuse
    top_bots = sorted(
        bots_resp,
        key=lambda b: (b.get("linear_score", 0), b.get("avg_sharpe", 0)),
        reverse=True,
    )[:5]

    # Geliştirme adayları: avg_sharpe >= 0.8 ama linear_qualified=False
    near_miss = [b for b in bots_resp if b.get("avg_sharpe", 0) >= 0.8 and not b.get("linear_qualified")][:5]

    # Eksikler listesi (bu lab genel)
    weaknesses: list[str] = []
    if not any(b.get("horizons", {}).get("1yr") for b in bots_resp):
        weaknesses.append("Hiçbir bot 1+ yıl backtest penceresine erişmiyor — data horizon yetersiz (yfinance 1h 730 gün, 5m 60 gün limit). 1d bazlı stratejilere ağırlık verilmeli.")
    avg_fail = [b for b in bots_resp if (b.get("avg_roi") or 0) < 0.10]
    if len(avg_fail) / max(len(bots_resp), 1) > 0.7:
        weaknesses.append(f"Botların {len(avg_fail) * 100 // max(len(bots_resp),1)}%'i %10 altı ROI üretiyor — edge arama daha akıllı olmalı (Bayesian optimizer eksik).")
    if promoted_ct == 0 and cycles_done >= 20:
        weaknesses.append("Son saatte 0 promote — CEO'nun eşikleri mevcut data için çok sıkı OR stratejiler gerçek edge yakalayamıyor.")

    # Gerçek mevcut performanstan hareketle dinamik tavsiyeler
    # Şu an ortalama bot ROI, en iyi ROI, promote oranı neyse — iyileştirme tahmini ona göre
    current_avg_roi = sum((b.get("avg_roi") or 0) for b in bots_resp) / max(len(bots_resp), 1)
    current_best_roi = max((b.get("avg_roi") or 0) for b in bots_resp) if bots_resp else 0
    current_promotion_rate = promoted_ct / max(cycles_done, 1)

    recommendations: list[dict[str, Any]] = []

    if any("Bayesian" in w for w in weaknesses) or current_avg_roi < 0.05:
        # Rastgele → Bayesian geçişi literatürde ~3-10x winner yield artırır (konservatif: 3x)
        target_avg_roi = min(current_avg_roi * 3, 0.20)
        recommendations.append({
            "title": "Bayesian parametre optimizasyonu (Crystal Optimizer ajanı)",
            "effort": "2-3 gün",
            "expected_gain": "Winner yakalama oranı ~3x (random → model-guided Optuna)",
            "expected_roi_impact": (
                f"Ortalama bot ROI {current_avg_roi:.1%} → ~{target_avg_roi:.1%} tahmini. "
                f"Winner oranı {current_promotion_rate:.1%} → ~{min(current_promotion_rate*3, 0.5):.1%}."
            ),
        })

    if any("horizon" in w.lower() for w in weaknesses):
        recommendations.append({
            "title": "Data horizon genişletme (paid provider: Polygon.io veya IEX)",
            "effort": "1 gün",
            "expected_gain": "1h stratejiler 2+ yıl test edilebilir, 1yr/2yr bucket'ları dolar",
            "expected_roi_impact": (
                f"Mevcut en iyi bot ROI {current_best_roi:.1%}; uzun horizon testinde "
                f"gerçek forward ROI ortaya çıkar — overfitting maskesi kalkar."
            ),
        })

    if current_promotion_rate < 0.15 and cycles_done >= 20:
        recommendations.append({
            "title": "CEO promotion eşiklerini adaptive yap",
            "effort": "1-2 gün",
            "expected_gain": "Eşikler lab'in gerçek edge seviyesine göre güncellenir",
            "expected_roi_impact": (
                f"Promote oranı {current_promotion_rate:.1%} → ~{min(current_promotion_rate*2, 0.3):.1%} "
                f"(yanlış pozitif oluşturmadan)"
            ),
        })

    if not recommendations:
        recommendations.append({
            "title": "Stress Lab + Walk-Forward zaten aktif — şu an kritik bir eksik yok",
            "effort": "—",
            "expected_gain": "Lab sağlıklı, cycle'lar devam etsin",
            "expected_roi_impact": "Uzun dönemde bot havuzu genişledikçe lineer filtreyi geçen adaylar çıkar.",
        })

    # Ana hedef gerçekçi — mevcut en iyi ROI + makul iyileşme
    # Mevcut en iyi ROI'den %30 üste çıkma hedefi (%5 → %35 değil; %5 → %7 realistik)
    realistic_target_roi = round(max(current_best_roi * 1.5, current_best_roi + 0.10), 3)

    # CEO top-line diagnosis
    ceo_diagnosis = (
        f"Son {hours} saatte {cycles_done} cycle tamamlandı, {promoted_ct} promote. "
        f"Toplam {len(bots_resp)} bot üretildi — {sum(1 for b in bots_resp if b.get('linear_qualified'))} tanesi "
        f"%50 yıllık büyüme filtresini geçiyor. "
    )
    if not weaknesses:
        ceo_diagnosis += "Lab sağlıklı çalışıyor."
    else:
        ceo_diagnosis += f"Ana bottleneck: {weaknesses[0][:120]}"

    return {
        "period_hours": hours,
        "timestamp": now.isoformat(),
        "activity": {
            "cycles_done": cycles_done,
            "decisions_made": decisions_made,
            "promoted_count": promoted_ct,
            "net_simulated_pnl_usd": round(total_pnl_recent, 2),
        },
        "ceo_diagnosis": ceo_diagnosis,
        "top_bots": [
            {
                "bot_name": b["bot_name"], "bot_id": b["bot_id"],
                "strategy_family": b["strategy_family"],
                "avg_sharpe": b["avg_sharpe"], "avg_roi": b["avg_roi"],
                "linear_score": b["linear_score"],
                "total_runs": b["total_runs"],
                "symbols_seen": b["symbols_seen"][:3],
            } for b in top_bots
        ],
        "near_miss_candidates": [
            {
                "bot_name": b["bot_name"], "bot_id": b["bot_id"],
                "avg_sharpe": b["avg_sharpe"], "avg_roi": b["avg_roi"],
                "qualified_horizons": b["qualified_horizons"],
                "hint": "Daha uzun horizon backtest'i gerekiyor" if b["avg_sharpe"] >= 0.8 else "Param optimize edilmeli",
            } for b in near_miss
        ],
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "realistic_target_roi": realistic_target_roi,
        "current_best_roi": current_best_roi,
        "current_avg_roi": current_avg_roi,
    }


@app.get("/api/llm/status")
async def api_llm_status():
    from oto_bot.llm.claude_cli import ClaudeCLI, budget_status
    c = ClaudeCLI()
    return {
        "available": c.is_available(),
        "binary": c.binary,
        "daily_cap": c.daily_cap,
        "budget": budget_status(),
    }


@app.post("/api/hiring/trigger")
async def api_hiring_trigger():
    """Manuel HR review tetikle (normalde her 50 cycle'da otomatik)."""
    registry = AgentRegistry()
    hr = HRManager(registry=registry)
    review = hr.review(cycle_number=0)
    return review


@app.get("/api/experiment/{experiment_id}/peers")
async def api_experiment_peers(experiment_id: str):
    """Aynı strateji ailesindeki rank + percentile."""
    con = _conn()
    if con is None:
        return {"error": "no db"}
    rows = con.execute(
        "SELECT data FROM experiments ORDER BY timestamp DESC LIMIT 2000"
    ).fetchall()
    target_fam = None
    target_sharpe = None
    target_roi = None
    peers = []
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        except Exception:
            continue
        if d.get("experiment_id") == experiment_id:
            target_fam = d.get("strategy_family")
            target_sharpe = d.get("sharpe")
            target_roi = d.get("roi")
        peers.append(d)

    if target_fam is None:
        return {"error": "not found"}

    fam_peers = [p for p in peers if p.get("strategy_family") == target_fam and p.get("sharpe") is not None]
    fam_peers.sort(key=lambda p: float(p.get("sharpe") or 0), reverse=True)
    total = len(fam_peers)
    rank = next((i + 1 for i, p in enumerate(fam_peers) if p.get("experiment_id") == experiment_id), total)
    percentile = round(100 * (1 - (rank / max(total, 1))), 1) if total else 0.0

    # Family benchmark
    fam_sharpes = [float(p.get("sharpe") or 0) for p in fam_peers]
    fam_rois = [float(p.get("roi") or 0) for p in fam_peers if p.get("roi") is not None]
    avg_fam_sharpe = round(sum(fam_sharpes) / max(len(fam_sharpes), 1), 3) if fam_sharpes else 0
    avg_fam_roi = round(sum(fam_rois) / max(len(fam_rois), 1), 4) if fam_rois else 0

    return {
        "strategy_family": target_fam,
        "rank_in_family": rank,
        "total_in_family": total,
        "percentile": percentile,
        "target_sharpe": target_sharpe,
        "target_roi": target_roi,
        "family_avg_sharpe": avg_fam_sharpe,
        "family_avg_roi": avg_fam_roi,
        "family_top5": [
            {
                "title": p.get("hypothesis_title"),
                "symbol": p.get("symbol") or "",
                "sharpe": p.get("sharpe"),
                "roi": p.get("roi"),
                "experiment_id": p.get("experiment_id"),
            }
            for p in fam_peers[:5]
        ],
    }


@app.post("/api/bots/prune")
async def api_bots_prune(
    min_avg_sharpe: float = 0.0,
    min_avg_roi: float = -0.05,
    min_runs: int = 2,
    dry_run: bool = False,
):
    """Başarısız botların deneylerini SIL.

    Kriter: avg_sharpe < min_avg_sharpe VE avg_roi < min_avg_roi VE total_runs >= min_runs.
    """
    con = _conn()
    if con is None:
        return {"error": "no db"}

    rows = con.execute("SELECT id, data FROM experiments ORDER BY timestamp DESC LIMIT 10000").fetchall()
    from collections import defaultdict
    by_bot: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
        except Exception:
            continue
        fam = inner.get("strategy_family") or d.get("strategy") or ""
        params = inner.get("strategy_params") or d.get("params") or {}
        bid = _bot_fingerprint(fam, params)
        by_bot[bid].append((r["id"], inner))

    bots_to_kill: list[str] = []
    ids_to_delete: list[str] = []
    for bid, runs in by_bot.items():
        if len(runs) < min_runs:
            continue
        avg_sharpe = sum((r[1].get("sharpe") or 0) for r in runs) / len(runs)
        avg_roi = sum((r[1].get("roi") or 0) for r in runs) / len(runs)
        if avg_sharpe < min_avg_sharpe and avg_roi < min_avg_roi:
            bots_to_kill.append(bid)
            ids_to_delete.extend([r[0] for r in runs])

    if dry_run:
        return {"bots_to_delete": len(bots_to_kill), "experiments_to_delete": len(ids_to_delete)}

    deleted = 0
    if ids_to_delete:
        placeholders = ",".join("?" for _ in ids_to_delete)
        cur = con.execute(f"DELETE FROM experiments WHERE id IN ({placeholders})", ids_to_delete)
        con.commit()
        deleted = cur.rowcount

    return {
        "bots_deleted": len(bots_to_kill),
        "experiments_deleted": deleted,
        "threshold": {"min_avg_sharpe": min_avg_sharpe, "min_avg_roi": min_avg_roi, "min_runs": min_runs},
    }


@app.get("/api/bots")
async def api_bots(start_capital: float = 10_000, limit: int = 2000):
    """FAZ 6 sonrası: bot listesi `bot_registry.json`'dan gelir (5 sabit slot)."""
    try:
        return await _api_bots_impl(start_capital, limit)
    except Exception as exc:
        import traceback, logging
        logging.getLogger("webui").error(f"api_bots crashed: {exc}\n{traceback.format_exc()}")
        return {"error": str(exc), "traceback": traceback.format_exc().splitlines()[-5:]}


async def _api_bots_impl(start_capital: float = 10_000, limit: int = 2000):
    """Asıl bot aggregation — exception fırlatabilir, üst katman yakalar."""
    # Önce sabit 5 slot'u registry'den oku (5 bot her zaman var).
    slots = []
    import sys, logging
    from pathlib import Path
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    log = logging.getLogger("webui.bots")
    log.warning(f"bots: src_dir={src_dir} in sys.path={src_dir in sys.path}")
    try:
        from oto_bot.agents.bot_registry import BotRegistry
        log.warning("bots: BotRegistry import OK")
    except Exception as exc:
        import traceback
        log.error(f"bots: BotRegistry import FAILED: {exc}\n{traceback.format_exc()}")
    try:
        registry = BotRegistry()
        slots = registry.all_slots()
        log.warning(f"bots: registry loaded slots={len(slots)}")
    except Exception as exc:
        import traceback
        log.error(f"bots: registry init/load FAILED: {exc}\n{traceback.format_exc()}")

    # Slot iteration cycle'larını topla (per-slot run history)
    con = _conn()
    rows = []
    if con is not None:
        rows = con.execute(
            "SELECT * FROM experiments WHERE category='slot_iteration' ORDER BY timestamp DESC LIMIT ?",
            (max(limit, 5000),),
        ).fetchall()

    # 5 slot'u her zaman bot listesine ekle (registry otorite, run history opsiyonel).
    bots: dict[str, dict[str, Any]] = {}
    SLOT_TF_DEFAULT = {"day": "1h", "swing": "1d", "scalp": "15m"}
    SLOT_DURATION_DEFAULT = {"1d": 1250, "1h": 24000, "15m": 60000}
    for s in slots:
        sid = s.slot_id
        bid = f"slot{sid}_{s.strategy_family}_{s.market}"
        name = f"{s.strategy_family}/{s.market} (slot {sid})"
        bots[bid] = {
            "bot_id": bid, "bot_name": name,
            "strategy_family": s.strategy_family,
            "strategy_params": s.lifetime_best_params or s.current_params or {},
            "market": s.market, "slot_id": sid,
            "lifetime_best_sharpe": s.lifetime_best_sharpe,
            "lifetime_best_oos_sharpe": s.lifetime_best_oos_sharpe,
            "iterations": s.iterations,
            "accepted_updates": s.accepted_updates,
            "data_window_start": s.data_window_start,
            "status": s.status,
            "runs": [], "symbols_seen": set(), "timeframes_seen": set(),
        }

    # Run iteration kayıtlarını mevcut slot bot dict'lerine ekle (yeni dict tanımı YOK).
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
        except Exception:
            continue
        family = inner.get("strategy_family") or d.get("strategy") or "unknown"
        params = inner.get("strategy_params") or d.get("params") or {}
        # FAZ 6 — slot bazlı bot. Bir slot = bir bot kişiliği. Params her cycle değişir
        # (Bayesian arıyor) ama bot kimliği = (family, market, slot_id).
        slot_id = inner.get("slot_id") or d.get("slot_id")
        market = inner.get("market") or d.get("market") or "unknown"
        if slot_id is not None:
            bid = f"slot{slot_id}_{family}_{market}"
        else:
            bid = _bot_fingerprint(family, params)
        # FAZ 6: registry'deki 5 slot otorite. Eski seed'den (scalp/forex, day/us_eq)
        # gelen ghost kayıtlar atılır — bot listesinde yer kaplamasın.
        if bid not in bots:
            continue
        b = bots[bid]
        b["runs"].append({
            "timestamp": r["timestamp"],
            "symbol": inner.get("symbol") or d.get("symbol") or "",
            "timeframe": inner.get("bar_timeframe") or d.get("timeframe") or "",
            "sharpe": inner.get("sharpe"),
            "roi": inner.get("roi"),
            "cagr": inner.get("cagr"),
            "max_drawdown": inner.get("max_drawdown"),
            "total_trades": inner.get("total_trades"),
            "duration_bars": inner.get("duration_bars"),
            "promoted": inner.get("promoted"),
            "regime": inner.get("regime"),
            "experiment_id": inner.get("experiment_id"),
        })
        if inner.get("symbol"):
            b["symbols_seen"].add(inner["symbol"])
        if inner.get("bar_timeframe"):
            b["timeframes_seen"].add(inner["bar_timeframe"])

    out = []
    import logging as _lg2
    _lg2.getLogger("webui.bots").warning(f"bots: entering loop with bots dict len={len(bots)}")
    for bid, b in bots.items():
        _lg2.getLogger("webui.bots").warning(f"bots: processing {bid} runs={len(b.get('runs',[]))}")
        runs = b["runs"]
        n = len(runs)
        # FAZ 6: registry'den gelen slot'lar runs=0 olsa bile bot olarak kalır.
        if n == 0:
            avg_sharpe = b.get("lifetime_best_sharpe", 0.0)
            avg_roi = 0.0
            avg_cagr = 0.0
            best_sharpe = b.get("lifetime_best_sharpe", 0.0)
            worst_dd = 0.0
            promoted_ct = 0
        else:
            avg_sharpe = sum(r.get("sharpe") or 0 for r in runs) / n
            avg_roi = sum(r.get("roi") or 0 for r in runs) / n
            avg_cagr = sum(r.get("cagr") or 0 for r in runs) / n
            best_sharpe = max((r.get("sharpe") or 0) for r in runs)
            worst_dd = min((r.get("max_drawdown") or 0) for r in runs)
            promoted_ct = sum(1 for r in runs if r.get("promoted"))

        # Horizon breakdown — her run'ın süresini ay cinsinden hesapla
        tf_to_min = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}
        # FAZ 6: slot family'sinden default TF (run'da boş gelirse)
        slot_tf_default = SLOT_TF_DEFAULT.get(b.get("strategy_family"), "1d")
        longest_run_months = 0.0
        for r in runs:
            bars = int(r.get("duration_bars") or 0)
            tf = r.get("timeframe") or slot_tf_default
            mins = tf_to_min.get(tf, 1440)
            months = (bars * mins) / (60 * 24 * 30)
            if months > longest_run_months:
                longest_run_months = months
        # Run yoksa registry slot'undan tahmin et (slot 5 yıl pencere ile koşar).
        if longest_run_months == 0 and n == 0:
            est_bars = SLOT_DURATION_DEFAULT.get(slot_tf_default, 1250)
            est_min = tf_to_min.get(slot_tf_default, 1440)
            longest_run_months = (est_bars * est_min) / (60 * 24 * 30)

        # Horizon buckets
        buckets = {"1yr": [], "2yr": [], "3yr": [], "4yr": [], "5yr": [], "<1yr": []}
        for r in runs:
            bars = int(r.get("duration_bars") or 0)
            tf = r.get("timeframe") or "1h"
            mins = tf_to_min.get(tf, 60)
            months = (bars * mins) / (60 * 24 * 30)
            years = months / 12.0
            if years >= 5:   buckets["5yr"].append(r)
            elif years >= 4: buckets["4yr"].append(r)
            elif years >= 3: buckets["3yr"].append(r)
            elif years >= 2: buckets["2yr"].append(r)
            elif years >= 1: buckets["1yr"].append(r)
            else: buckets["<1yr"].append(r)

        def _bucket_stats(bucket_runs):
            if not bucket_runs:
                return None
            bn = len(bucket_runs)
            bavg_roi = sum(r.get("roi") or 0 for r in bucket_runs) / bn
            bavg_sharpe = sum(r.get("sharpe") or 0 for r in bucket_runs) / bn
            # final $ if $10k invested at avg_roi
            final_usd = start_capital * (1 + bavg_roi)
            return {
                "runs": bn,
                "avg_roi": round(bavg_roi, 4),
                "avg_sharpe": round(bavg_sharpe, 3),
                "final_usd_est": round(final_usd, 2),
                "profit_usd_est": round(final_usd - start_capital, 2),
            }

        horizons = {k: _bucket_stats(v) for k, v in buckets.items()}

        # Lineer yıllık büyüme kontrolü — çoklu tier: %20, %30, %50
        horizon_years = {"1yr": 1, "2yr": 2, "3yr": 3, "4yr": 4, "5yr": 5}

        def _evaluate_tier(target_pct: float) -> tuple[bool, int, list[str]]:
            qualified = []
            failed = []
            for hk, hyears in horizon_years.items():
                stat = horizons.get(hk)
                if not stat:
                    continue
                required = target_pct * hyears
                if (stat.get("avg_roi") or 0) >= required:
                    qualified.append(hk)
                else:
                    failed.append(hk)
            qualified_ok = (len(qualified) >= 1 and len(failed) == 0)
            return qualified_ok, len(qualified), qualified

        q20, s20, _ = _evaluate_tier(0.20)
        q30, s30, _ = _evaluate_tier(0.30)
        q50, s50, qh50 = _evaluate_tier(0.50)

        # Eski alanlar (backward compat) — %50 tier
        linear_qualified = q50
        linear_score = s50
        qualified_horizons = qh50

        out.append({
            "bot_id": bid,
            "bot_name": b["bot_name"],
            "strategy_family": b["strategy_family"],
            "strategy_params": b["strategy_params"],
            "total_runs": n,
            "avg_sharpe": round(avg_sharpe, 3),
            "avg_roi": round(avg_roi, 4),
            "avg_cagr": round(avg_cagr, 4),
            "best_sharpe": round(best_sharpe, 3),
            "worst_dd": round(worst_dd, 4),
            "promoted_count": promoted_ct,
            "promotion_rate": round(promoted_ct / n, 3),
            "symbols_seen": list(b["symbols_seen"]),
            "timeframes_seen": list(b["timeframes_seen"]),
            "longest_run_months": round(longest_run_months, 2),
            "horizons": horizons,  # {1yr: {...}, 2yr: {...}, ...}
            "linear_qualified": linear_qualified,
            "linear_score": linear_score,
            "qualified_horizons": qualified_horizons,
            "qualified_20": q20, "score_20": s20,
            "qualified_30": q30, "score_30": s30,
            "qualified_50": q50, "score_50": s50,
            "last_run_timestamp": runs[0]["timestamp"] if runs else None,
        })

    # Sort by avg_sharpe descending
    out.sort(key=lambda x: x.get("avg_sharpe", 0), reverse=True)
    import logging as _lg
    _lg.getLogger("webui.bots").warning(f"bots: final out len={len(out)} from bots dict len={len(bots)}")
    # JSON-safe: numpy/set/dataclass tipleri primitive'e çevir.
    # FastAPI default jsonable_encoder set/numpy ile sessiz fail oluyor — manuel Response.
    import json as _json
    from fastapi.responses import Response as _FastResp
    def _safe(o):
        if isinstance(o, (set, frozenset)): return list(o)
        if hasattr(o, 'item'): return o.item()  # numpy scalars
        return str(o)
    try:
        body = _json.dumps(out, default=_safe, ensure_ascii=False)
        return _FastResp(content=body, media_type="application/json")
    except Exception as exc:
        import traceback
        _lg.getLogger("webui.bots").error(f"bots: JSON encode failed: {exc}\n{traceback.format_exc()}")
        return _FastResp(content="[]", media_type="application/json")


@app.get("/api/bot/{bot_id}/horizon/{horizon}")
async def api_bot_horizon(bot_id: str, horizon: str, start_capital: float = 10_000):
    """Belirli horizon (1yr, 2yr, 3yr, 4yr, 5yr, sub1yr) için bu bot'un run'ları."""
    con = _conn()
    if con is None:
        return {"error": "no db"}
    rows = con.execute(
        "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT 3000"
    ).fetchall()

    tf_to_min = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}
    horizon_bounds = {
        "1yr": (1, 2), "2yr": (2, 3), "3yr": (3, 4),
        "4yr": (4, 5), "5yr": (5, 100), "sub1yr": (0, 1),
    }
    lo, hi = horizon_bounds.get(horizon, (0, 100))

    runs = []
    bot_name = None
    family = None
    params = None
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
        except Exception:
            continue
        fam = inner.get("strategy_family") or d.get("strategy") or ""
        p = inner.get("strategy_params") or d.get("params") or {}
        bid = _bot_fingerprint(fam, p)
        if bid != bot_id:
            continue
        if bot_name is None:
            bot_name = _bot_name(fam, p)
            family = fam
            params = p

        bars = int(inner.get("duration_bars") or 0)
        tf = inner.get("bar_timeframe") or d.get("timeframe") or "1h"
        years = (bars * tf_to_min.get(tf, 60)) / (60 * 24 * 30 * 12)
        if lo <= years < hi:
            merged = {"timestamp": r["timestamp"]}
            merged.update(inner)
            _enrich_experiment(merged, start_capital)
            runs.append(merged)

    n = len(runs)
    if n == 0:
        return {
            "bot_id": bot_id, "bot_name": bot_name or bot_id,
            "horizon": horizon, "runs": 0, "items": [],
        }

    avg_roi = sum(r.get("roi") or 0 for r in runs) / n
    avg_sharpe = sum(r.get("sharpe") or 0 for r in runs) / n
    total_profit = sum(r.get("profit_usd") or 0 for r in runs)
    best = max(runs, key=lambda r: r.get("sharpe") or 0)
    worst = min(runs, key=lambda r: r.get("sharpe") or 0)

    # Final USD if hypothetically compounded (approximation)
    final_usd = start_capital * (1 + avg_roi)

    items = [{
        "timestamp": r.get("timestamp"),
        "symbol": r.get("symbol"),
        "timeframe": r.get("bar_timeframe"),
        "regime": r.get("regime"),
        "roi": r.get("roi"),
        "sharpe": r.get("sharpe"),
        "profit_usd": r.get("profit_usd"),
        "duration_human": r.get("duration_human"),
        "duration_months_est": r.get("duration_months_est"),
        "total_trades": r.get("total_trades"),
        "win_rate": r.get("win_rate"),
        "promoted": r.get("promoted"),
        "experiment_id": r.get("experiment_id"),
        "avg_trade_duration": r.get("avg_trade_duration"),
    } for r in runs[:100]]

    return {
        "bot_id": bot_id,
        "bot_name": bot_name,
        "strategy_family": family,
        "horizon": horizon,
        "runs": n,
        "avg_roi": round(avg_roi, 4),
        "avg_sharpe": round(avg_sharpe, 3),
        "total_profit_usd": round(total_profit, 2),
        "final_usd_est": round(final_usd, 2),
        "best": {
            "symbol": best.get("symbol"), "timeframe": best.get("bar_timeframe"),
            "sharpe": best.get("sharpe"), "roi": best.get("roi"),
            "experiment_id": best.get("experiment_id"),
        },
        "worst": {
            "symbol": worst.get("symbol"), "timeframe": worst.get("bar_timeframe"),
            "sharpe": worst.get("sharpe"), "roi": worst.get("roi"),
            "experiment_id": worst.get("experiment_id"),
        },
        "items": items,
    }


@app.get("/api/bot/{bot_id}")
async def api_bot(bot_id: str, start_capital: float = 10_000):
    """Bir botun tüm run'larını aggregate et — family + params hash'ine göre."""
    con = _conn()
    if con is None:
        return {"error": "no db"}
    rows = con.execute(
        "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT 3000"
    ).fetchall()
    runs = []
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        except Exception:
            continue
        d_bot_id = _bot_fingerprint(d.get("strategy_family", ""), d.get("strategy_params"))
        if d_bot_id != bot_id:
            continue
        merged = {"timestamp": r["timestamp"]}
        merged.update(d)
        _enrich_experiment(merged, start_capital)
        runs.append(merged)

    if not runs:
        return {"error": "no runs for this bot"}

    # Aggregate
    n = len(runs)
    total_pnl_usd = sum(r.get("profit_usd", 0) for r in runs)
    total_duration_months = sum(r.get("duration_months_est", 0) or 0 for r in runs)
    avg_sharpe = sum(r.get("sharpe", 0) or 0 for r in runs) / n
    avg_roi = sum(r.get("roi", 0) or 0 for r in runs) / n
    avg_cagr = sum(r.get("cagr", 0) or 0 for r in runs) / n
    best = max(runs, key=lambda r: r.get("sharpe", 0) or 0)
    worst = min(runs, key=lambda r: r.get("sharpe", 0) or 0)

    # Breakdown by symbol / timeframe / regime
    by_symbol: dict[str, list[dict]] = {}
    for r in runs:
        by_symbol.setdefault(r.get("symbol", ""), []).append(r)
    by_tf: dict[str, list[dict]] = {}
    for r in runs:
        by_tf.setdefault(r.get("bar_timeframe", ""), []).append(r)
    by_regime: dict[str, list[dict]] = {}
    for r in runs:
        by_regime.setdefault(r.get("regime", "unknown"), []).append(r)

    def _group_stats(group: dict[str, list[dict]]) -> list[dict]:
        out = []
        for k, lst in group.items():
            nk = len(lst)
            out.append({
                "key": k,
                "runs": nk,
                "avg_sharpe": round(sum(r.get("sharpe", 0) or 0 for r in lst) / nk, 3),
                "avg_roi": round(sum(r.get("roi", 0) or 0 for r in lst) / nk, 4),
                "total_profit_usd": round(sum(r.get("profit_usd", 0) for r in lst), 2),
            })
        out.sort(key=lambda x: x["avg_sharpe"], reverse=True)
        return out

    # Portfolio-wide 12m projection: using avg_cagr
    projection = _project_12m(avg_cagr, start_capital)

    first = runs[0]
    return {
        "bot_id": bot_id,
        "bot_name": first.get("bot_name"),
        "strategy_family": first.get("strategy_family"),
        "strategy_params": first.get("strategy_params"),
        "total_runs": n,
        "total_profit_usd": round(total_pnl_usd, 2),
        "total_simulated_months": round(total_duration_months, 2),
        "avg_sharpe": round(avg_sharpe, 3),
        "avg_roi": round(avg_roi, 4),
        "avg_cagr": round(avg_cagr, 4),
        "best_run": {
            "title": best.get("hypothesis_title"),
            "symbol": best.get("symbol"), "timeframe": best.get("bar_timeframe"),
            "sharpe": best.get("sharpe"), "roi": best.get("roi"),
            "duration_human": best.get("duration_human"),
            "experiment_id": best.get("experiment_id"),
        },
        "worst_run": {
            "title": worst.get("hypothesis_title"),
            "symbol": worst.get("symbol"), "timeframe": worst.get("bar_timeframe"),
            "sharpe": worst.get("sharpe"), "roi": worst.get("roi"),
            "duration_human": worst.get("duration_human"),
            "experiment_id": worst.get("experiment_id"),
        },
        "by_symbol": _group_stats(by_symbol),
        "by_timeframe": _group_stats(by_tf),
        "by_regime": _group_stats(by_regime),
        "projection": projection,
        "recent_runs": [
            {
                "timestamp": r.get("timestamp"),
                "experiment_id": r.get("experiment_id"),
                "symbol": r.get("symbol"),
                "timeframe": r.get("bar_timeframe"),
                "regime": r.get("regime"),
                "roi": r.get("roi"),
                "sharpe": r.get("sharpe"),
                "profit_usd": r.get("profit_usd"),
                "duration_human": r.get("duration_human"),
                "promoted": r.get("promoted"),
            }
            for r in runs[:50]
        ],
    }


@app.get("/api/robustness/status")
async def api_robustness_status():
    queue = RobustnessQueue()
    pending = queue.peek_all(limit=1000)
    # Breakdown by reason
    from collections import Counter
    reasons = Counter(t.get("reason", "unknown") for t in pending)
    horizons = Counter(str(t.get("data_window_years") or "default") for t in pending)
    return {
        "queue_size": len(pending),
        "by_reason": dict(reasons),
        "by_horizon": dict(horizons),
        "next_5": pending[:5],
    }


@app.post("/api/robustness/schedule")
async def api_robustness_schedule(experiment_id: str):
    con = _conn()
    if con is None:
        return {"error": "no db"}
    rows = con.execute("SELECT data FROM experiments ORDER BY timestamp DESC LIMIT 2000").fetchall()
    origin = None
    for r in rows:
        try:
            d = json.loads(r["data"])
            if d.get("experiment_id") == experiment_id:
                origin = d
                break
        except Exception:
            continue
    if origin is None:
        return {"error": "experiment not found"}
    tests = generate_variants(origin, max_variants=12, reason="manual_modal")
    queue = RobustnessQueue()
    n = queue.push_many(tests)
    return {"scheduled": n, "tests": [
        {"symbol": t.symbol, "timeframe": t.timeframe} for t in tests
    ]}


@app.post("/api/bots/test-all-long-horizon")
async def api_bots_test_all_long_horizon(
    min_avg_sharpe: float = 0.0,
    horizons: str = "1,3",
    max_bots: int = 300,
):
    """Tüm umut veren botları (avg_sharpe >= min) 1yr + 3yr pencerelerinde test kuyruğuna at.

    horizons: virgülle ayrılmış yıl listesi (default "1,3" — 1yr + 3yr)
    """
    import uuid as _uuid
    from datetime import datetime, timezone
    from oto_bot.agents.robustness import RobustnessQueue, RobustnessTest

    years_list = [float(x.strip()) for x in horizons.split(",") if x.strip()]

    # Tüm bot aggregate'lerini al (/api/bots'un çıktısını yeniden hesapla)
    con = _conn()
    if con is None:
        return {"error": "no db"}

    rows = con.execute("SELECT data FROM experiments ORDER BY timestamp DESC LIMIT 10000").fetchall()
    by_bot: dict[str, dict] = {}
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
        except Exception:
            continue
        fam = inner.get("strategy_family") or d.get("strategy") or ""
        params = inner.get("strategy_params") or d.get("params") or {}
        bid = _bot_fingerprint(fam, params)

        if bid not in by_bot:
            by_bot[bid] = {
                "strategy": fam, "params": params,
                "market": inner.get("market") or d.get("market"),
                "symbols": set(), "timeframes": set(),
                "sharpes": [], "exp_id": inner.get("experiment_id") or "",
                "title": inner.get("hypothesis_title") or "",
            }
        sym = inner.get("symbol") or d.get("symbol")
        tf = inner.get("bar_timeframe") or d.get("timeframe")
        if sym:
            by_bot[bid]["symbols"].add(sym)
        if tf:
            by_bot[bid]["timeframes"].add(tf)
        by_bot[bid]["sharpes"].append(inner.get("sharpe") or 0)

    # Filtrele: avg_sharpe >= min_avg_sharpe
    elig = []
    for bid, b in by_bot.items():
        if not b["sharpes"]:
            continue
        avg_s = sum(b["sharpes"]) / len(b["sharpes"])
        if avg_s < min_avg_sharpe:
            continue
        elig.append((bid, b, avg_s))
    elig.sort(key=lambda x: x[2], reverse=True)
    elig = elig[:max_bots]

    queue = RobustnessQueue()
    tests = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for bid, b, avg_s in elig:
        # Bot'un ilk symbol + ilk tf alınır (birden çoksa en yaygın)
        sym = next(iter(b["symbols"]), None)
        tf = next(iter(b["timeframes"]), None)
        if not sym or not tf:
            continue
        for years in years_list:
            tests.append(RobustnessTest(
                test_id=str(_uuid.uuid4()),
                origin_experiment_id=b["exp_id"],
                origin_title=b["title"],
                market=b["market"], strategy=b["strategy"],
                symbol=sym, timeframe=tf,
                params=dict(b["params"]),
                reason=f"bulk_long_horizon_{years}yr",
                created_at=now_iso,
                data_window_years=years,
            ))

    n = queue.push_many(tests)
    return {
        "eligible_bots": len(elig),
        "scheduled_tests": n,
        "horizons": years_list,
        "total_bots_seen": len(by_bot),
    }


@app.post("/api/bot/{bot_id}/test-long-horizon")
async def api_bot_test_long_horizon(bot_id: str):
    """Botu 1/2/3/5 yıl penceresinde re-test et. Aynı symbol+params, farklı veri boyutu."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from oto_bot.agents.robustness import RobustnessQueue, RobustnessTest

    con = _conn()
    if con is None:
        return {"error": "no db"}
    rows = con.execute("SELECT data FROM experiments ORDER BY timestamp DESC LIMIT 3000").fetchall()

    origin = None
    all_symbols = set()
    all_timeframes = set()
    for r in rows:
        try:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            inner = d.get("result") if isinstance(d.get("result"), dict) else d
        except Exception:
            continue
        fam = inner.get("strategy_family") or d.get("strategy") or ""
        params = inner.get("strategy_params") or d.get("params") or {}
        bid = _bot_fingerprint(fam, params)
        if bid != bot_id:
            continue
        if origin is None:
            origin = inner
            origin["_market"] = inner.get("market") or d.get("market")
            origin["_strategy"] = fam
            origin["_params"] = params
        sym = inner.get("symbol") or d.get("symbol")
        tf = inner.get("bar_timeframe") or d.get("timeframe")
        if sym:
            all_symbols.add(sym)
        if tf:
            all_timeframes.add(tf)

    if origin is None:
        return {"error": "bot not found"}

    queue = RobustnessQueue()
    tests: list = []
    years_options = [1.0, 2.0, 3.0, 5.0]
    now_iso = datetime.now(timezone.utc).isoformat()

    # Her symbol/timeframe için 4 horizon
    for sym in list(all_symbols)[:3]:
        for tf in list(all_timeframes)[:2]:
            for years in years_options:
                tests.append(RobustnessTest(
                    test_id=str(_uuid.uuid4()),
                    origin_experiment_id=origin.get("experiment_id") or "",
                    origin_title=origin.get("hypothesis_title") or "",
                    market=origin["_market"],
                    strategy=origin["_strategy"],
                    symbol=sym,
                    timeframe=tf,
                    params=dict(origin["_params"]),
                    reason=f"long_horizon_{years}yr",
                    created_at=now_iso,
                    data_window_years=years,
                ))

    n = queue.push_many(tests)
    return {
        "bot_id": bot_id,
        "scheduled": n,
        "sample_tests": [
            {"symbol": t.symbol, "timeframe": t.timeframe, "years": t.data_window_years}
            for t in tests[:8]
        ],
    }


@app.get("/api/experiment/{experiment_id}")
async def api_experiment_detail(experiment_id: str, start_capital: float = 10_000):
    con = _conn()
    if con is None:
        return JSONResponse({"error": "no db"}, status_code=500)
    # Search by data.experiment_id (JSON field)
    rows = con.execute(
        "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT 2000"
    ).fetchall()
    for r in rows:
        try:
            data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            if data.get("experiment_id") == experiment_id:
                merged = {"id": r["id"], "timestamp": r["timestamp"], "category": r["category"], "agent_id": r["agent_id"]}
                merged.update(data)
                _enrich_experiment(merged, start_capital)
                return merged
        except Exception:
            continue
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/winners")
async def api_winners():
    return _load_jsonl(WINNERS_FILE)


@app.get("/api/pods")
async def api_pods():
    return _load_json(PODS_FILE, default=[])


@app.get("/api/activity")
async def api_activity():
    con = _conn()
    if con is None:
        return {"hypotheses": [], "decisions": [], "debates": []}
    hyp = _parse_rows(con.execute("SELECT * FROM hypotheses ORDER BY timestamp DESC LIMIT 10").fetchall())
    dec = _parse_rows(con.execute("SELECT * FROM decisions ORDER BY timestamp DESC LIMIT 10").fetchall())
    deb = _parse_rows(con.execute("SELECT * FROM debate_records ORDER BY timestamp DESC LIMIT 5").fetchall())
    return {"hypotheses": hyp, "decisions": dec, "debates": deb}


@app.get("/api/log")
async def api_log(lines: int = 120):
    if not LOG_FILE.exists():
        return {"text": "(log yok)"}
    data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"text": "\n".join(data[-lines:])}


@app.get("/api/current")
async def api_current():
    """Şu anki cycle: hangi faz, hangi proje, hangi ajanlar aktif."""
    hb_path = ROOT / "artifacts" / "current_cycle.json"
    if not hb_path.exists():
        return {
            "cycle": 0, "phase": "idle", "phase_label": "Boşta",
            "active_agents": [], "hypothesis": {}, "timestamp": None,
        }
    try:
        return json.loads(hb_path.read_text(encoding="utf-8"))
    except Exception:
        return {"cycle": 0, "phase": "idle", "phase_label": "?", "active_agents": [], "hypothesis": {}}


@app.get("/api/agent-status")
async def api_agent_status():
    """Her ajan için anlık durum: active / learning / idle + son aktivite."""
    agents = _load_json(AGENTS_FILE, default=[])
    hb_path = ROOT / "artifacts" / "current_cycle.json"
    current = {}
    if hb_path.exists():
        try:
            current = json.loads(hb_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    active_now = set(current.get("active_agents", []))
    phase = current.get("phase", "idle")

    con = _conn()
    # last activity per agent from any table
    last_activity: dict[str, str] = {}
    if con:
        # agents with author_agent_id on hypotheses
        for tbl in ("hypotheses", "decisions"):
            rows = con.execute(
                f"SELECT agent_id, MAX(timestamp) as ts FROM {tbl} WHERE agent_id IS NOT NULL GROUP BY agent_id"
            ).fetchall()
            for r in rows:
                aid = r["agent_id"]
                ts = r["ts"]
                if aid and (aid not in last_activity or last_activity[aid] < ts):
                    last_activity[aid] = ts

    # Get agents that appeared in last 20 debates (they're "learning")
    recent_debate_agents: set[str] = set()
    if con:
        rows = con.execute(
            "SELECT data FROM debate_records ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        for r in rows:
            try:
                data = json.loads(r["data"])
                for arg in data.get("arguments", []):
                    if isinstance(arg, dict):
                        recent_debate_agents.add(arg.get("agent_name", ""))
            except Exception:
                pass

    out = []
    for a in agents:
        name = a.get("name", "")
        profile = AGENT_PROFILES.get(name, {})
        if name in active_now:
            status = "active"
            status_label = "görevde"
        elif name in recent_debate_agents:
            status = "learning"
            status_label = "son deneylerden öğreniyor"
        elif a.get("active"):
            status = "standby"
            status_label = "sırada / bekliyor"
        else:
            status = "idle"
            status_label = "emekli"
        out.append({
            "name": name,
            "role": a.get("role"),
            "department": a.get("department"),
            "icon": profile.get("icon", "🤖"),
            "color": profile.get("color", "#9ca3af"),
            "status": status,
            "status_label": status_label,
            "active_in_cycle": name in active_now,
            "last_activity": last_activity.get(a.get("agent_id")),
        })
    return {
        "current_phase": phase,
        "phase_label": current.get("phase_label", ""),
        "cycle": current.get("cycle", 0),
        "agents": out,
    }


@app.get("/api/lessons")
async def api_lessons(
    agent: str | None = None,
    market: str | None = None,
    strategy: str | None = None,
    regime: str | None = None,
    symbol: str | None = None,
    severity: str | None = None,
    limit: int = 200,
):
    journal = LearningJournal()
    lessons = journal.query(
        author_agent=agent, market=market, strategy_family=strategy,
        regime=regime, symbol=symbol, severity=severity, limit=limit,
    )
    out = []
    for lesson in lessons:
        out.append({
            "lesson_id": lesson.lesson_id,
            "author_agent": lesson.author_agent,
            "content": lesson.content,
            "tags": lesson.tags,
            "market": lesson.market,
            "strategy_family": lesson.strategy_family,
            "regime": lesson.regime,
            "symbol": lesson.symbol,
            "severity": lesson.severity,
            "source_cycle": lesson.source_cycle,
            "times_referenced": lesson.times_referenced,
            "created_at": lesson.created_at.isoformat(),
        })
    return out


@app.get("/api/lessons-stats")
async def api_lessons_stats():
    """Ders istatistikleri — total / referenced / CEO-approved / per-severity / per-regime."""
    journal = LearningJournal()
    # Toplam SQL COUNT'tan; agregasyon için son 50k ders (perf cap).
    total = journal.count()
    all_lessons = journal.query(limit=50_000)

    # "CEO-approved" proxy: times_referenced >= 3 (a lesson consulted multiple times
    # during hypothesis generation is de facto endorsed by the agent network).
    # "Used in models" = referenced at least once.
    ceo_approved = sum(1 for l in all_lessons if l.times_referenced >= 3)
    used_in_models = sum(1 for l in all_lessons if l.times_referenced >= 1)
    dormant = total - used_in_models

    # Per-severity distribution
    sev_counts: dict[str, int] = {}
    for l in all_lessons:
        sev_counts[l.severity] = sev_counts.get(l.severity, 0) + 1

    # Per-regime
    regime_counts: dict[str, int] = {}
    for l in all_lessons:
        regime_counts[l.regime or "unknown"] = regime_counts.get(l.regime or "unknown", 0) + 1

    # Per-strategy
    strategy_counts: dict[str, int] = {}
    for l in all_lessons:
        strategy_counts[l.strategy_family or "unknown"] = strategy_counts.get(l.strategy_family or "unknown", 0) + 1

    # Per-agent
    per_agent = journal.all_ids_by_agent()

    # Time-series bucketed by day (last 30 days)
    from collections import defaultdict
    per_day_total: dict[str, int] = defaultdict(int)
    per_day_approved: dict[str, int] = defaultdict(int)
    for l in all_lessons:
        day = l.created_at.strftime("%Y-%m-%d")
        per_day_total[day] += 1
        if l.times_referenced >= 3:
            per_day_approved[day] += 1
    days = sorted(set(per_day_total.keys()))[-30:]
    timeline = [{
        "day": d,
        "total": per_day_total.get(d, 0),
        "approved": per_day_approved.get(d, 0),
    } for d in days]

    # Top-referenced lessons
    top_referenced = sorted(all_lessons, key=lambda l: l.times_referenced, reverse=True)[:10]
    top_list = [{
        "content": l.content,
        "author": l.author_agent,
        "severity": l.severity,
        "times_referenced": l.times_referenced,
        "market": l.market,
        "strategy_family": l.strategy_family,
        "regime": l.regime,
    } for l in top_referenced]

    return {
        "total": total,
        "ceo_approved": ceo_approved,
        "used_in_models": used_in_models,
        "dormant": dormant,
        "per_agent": per_agent,
        "severity_counts": sev_counts,
        "regime_counts": regime_counts,
        "strategy_counts": strategy_counts,
        "timeline": timeline,
        "top_referenced": top_list,
    }


@app.get("/api/learning-curve")
async def api_learning_curve(scope: str = "all", agent: str = "orchestrator"):
    curve = LearningCurve()
    curve.rebuild_from_experiments()
    points = curve.get_points(scope=scope, agent=agent, limit=60)
    return {"scope": scope, "agent": agent, "points": points}


@app.get("/api/learning-scopes")
async def api_learning_scopes():
    curve = LearningCurve()
    curve.rebuild_from_experiments()
    return {"scopes": curve.scopes()}


@app.get("/api/resources")
async def api_resources():
    """Token/kaynak izleme. LLM'e benzer bir tahmini hesap döner."""
    con = _conn()
    total_text_bytes = 0
    rows_count = 0
    if con:
        # Estimate: sum of data column byte sizes across tables
        for table in ("experiments", "decisions", "hypotheses", "debate_records"):
            try:
                row = con.execute(
                    f"SELECT COALESCE(SUM(LENGTH(data)), 0) as b, COUNT(*) as c FROM {table}"
                ).fetchone()
                total_text_bytes += int(row["b"] or 0)
                rows_count += int(row["c"] or 0)
            except Exception:
                pass
    # Rough: 1 token ≈ 4 bytes (OpenAI estimate, English; Turkish ~3)
    est_tokens_stored = int(total_text_bytes / 4)
    db_size_bytes = DB_FILE.stat().st_size if DB_FILE.exists() else 0

    # Avg bytes per cycle → avg tokens per cycle
    experiments_ct = 0
    if con:
        experiments_ct = con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    avg_tokens_per_cycle = int((total_text_bytes / max(experiments_ct, 1)) / 4) if experiments_ct else 0

    return {
        "total_stored_bytes": total_text_bytes,
        "total_stored_tokens_est": est_tokens_stored,
        "total_rows": rows_count,
        "db_file_size_bytes": db_size_bytes,
        "experiments_count": experiments_ct,
        "avg_tokens_per_cycle_est": avg_tokens_per_cycle,
        "note": "Loop'ta LLM çağrısı yok (rule-based). Token sayısı MB cinsinden depolanan metnin OpenAI tokenizer tahminidir.",
    }


@app.get("/api/goals")
async def api_goals():
    winners = _load_jsonl(WINNERS_FILE)
    con = _conn()
    best_cagr = 0.0
    best_sharpe = 0.0
    best_dd = 0.0
    if con:
        rows = _parse_rows(con.execute(
            "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT 2000"
        ).fetchall())
        for r in rows:
            c = r.get("cagr")
            s = r.get("sharpe")
            d = r.get("max_drawdown")
            if c is not None:
                best_cagr = max(best_cagr, float(c))
            if s is not None:
                best_sharpe = max(best_sharpe, float(s))
            if d is not None:
                best_dd = max(best_dd, float(d))
    return {
        "winners": len(winners),
        "target_winners": 1,
        "best_cagr": best_cagr,
        "target_cagr": 0.60,
        "best_sharpe": best_sharpe,
        "target_sharpe": 1.50,
        "best_dd": best_dd,
        "target_dd": -0.15,
    }
