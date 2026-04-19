"""Hermes GitResearcher — GitHub'dan trading bot repo'larını tarar, öneri üretir.

İki mod:
    - rule_based (default, ÜCRETSİZ): README'lerde keyword/pattern matching ile
      potansiyel değerli özellikleri bulur. Sınırlı ama sonsuz çalışır.
    - llm_enhanced (opsiyonel, ANTHROPIC_API_KEY): Claude'a README'yi verir,
      "bu repo'dan bizim botumuza faydalı olabilecek 3-5 fikir çıkar" diye sorar.

Akış:
    1. GitHub API'ye search query (ör. "trading bot python mean reversion")
    2. En yüksek starlı N repo'yu al
    3. Her biri için README fetch
    4. rule_based: keyword extract
    5. llm_enhanced (opsiyonel): Claude ile semantic extract
    6. Her bulgudan bir `Proposal` üret → queue'ya submit

GitHub unauth API rate limit: 60 request/saat. Yeterli.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from oto_bot.agents.proposals import Proposal, ProposalQueue


# ---------------------------------------------------------------------------
# Keyword / pattern extraction rules (rule-based mode)
# ---------------------------------------------------------------------------

VALUABLE_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, benefit_category, suggested_action)
    (r"(?i)(bayesian|optuna|hyperopt|bayes.*optim)", "Bayesian parametre optimizasyonu",
     "Crystal Optimizer ajanı kur, mevcut random search'i değiştir."),
    (r"(?i)(monte.?carlo|bootstrap)", "Monte Carlo simülasyon",
     "BacktestEngine.monte_carlo_dd'yi her cycle'da koş."),
    (r"(?i)(walk[- ]forward|wf[- ]validation)", "Walk-forward validation",
     "Her cycle'da walk_forward koş, Sharpe degradation tespit et."),
    (r"(?i)(kelly|fractional.?kelly)", "Kelly criterion sizing",
     "Position sizing'i Kelly ile değiştir, risk-adjusted sizing."),
    (r"(?i)(hmm|hidden.?markov|regime.?switching)", "HMM rejim tespiti",
     "Regime Oracle'a HMM eklentisi — daha sağlam rejim sınıflama."),
    (r"(?i)(risk.?parity|hrp|hierarchical.?risk)", "HRP / Risk Parity portfolio",
     "Portföy optimizer ekle, pod korelasyonunu aktif yönet."),
    (r"(?i)(lstm|transformer|neural.?network|deep.?learning)", "Deep learning model",
     "Feature set + ML sinyal üreticisi. DIKKAT: overfitting riski yüksek, Cassandra pre-mortem zorunlu."),
    (r"(?i)(genetic.?algorithm|evolution.*strategy)", "Evolutionary optimizer",
     "Strategy genome + crossover/mutation — mevcut overnight_scalper ile entegre."),
    (r"(?i)(sentiment|news.?analysis|nlp)", "News/sentiment features",
     "Market context'e sentiment layer ekle."),
    (r"(?i)(order.?book|depth.?imbalance|microstructure)", "Order book microstructure",
     "Binance depth stream → feature. Scalper için güçlü edge."),
    (r"(?i)(volume.?profile|vwap.?bands?)", "Volume profile / VWAP bands",
     "Feature engineering layer'a ekle."),
    (r"(?i)(ichimoku|supertrend|heikin)", "Ek teknik indikatör",
     "Nova'nın indikatör alfabesine ekle."),
    (r"(?i)(backtesting\.py|vectorbt|zipline|backtrader)", "Backtest framework referansı",
     "Mevcut engine ile karşılaştır — eksik feature varsa al."),
    (r"(?i)(paper.?trading|testnet)", "Paper trading kullanım pattern'i",
     "Forge execution pipeline'ı doğrula."),
    (r"(?i)(risk.?manage|drawdown.?control|circuit.?break)", "Risk management detayı",
     "Apex PortfolioRisk'e potansiyel eklenti."),
    (r"(?i)(multi.?time.?frame|mtf|higher.?timeframe)", "Multi-timeframe confluence",
     "Strategy'ye üst-TF onay kuralı ekle."),
    (r"(?i)(machine.?learning.*(feature|engineering))", "ML feature engineering",
     "Feature pipeline — Volume profile, sentiment, macro."),
]


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------


@dataclass
class RepoCard:
    full_name: str
    description: str
    stars: int
    url: str
    language: str
    updated_at: str
    topics: list[str]


class HermesGitResearcher:
    def __init__(
        self,
        queue: ProposalQueue | None = None,
        use_llm: bool = False,
        user_agent: str = "oto-bot-hermes-git-researcher",
    ) -> None:
        self.queue = queue or ProposalQueue()
        self.use_llm = use_llm
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": user_agent}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.api = "https://api.github.com"

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    DEFAULT_QUERIES: list[str] = [
        "trading bot python stars:>100",
        "crypto trading strategy python stars:>50",
        "quantitative trading python stars:>200",
        "mean reversion python stars:>30",
        "momentum strategy python stars:>30",
        "backtesting framework python stars:>100",
        "algo trading python stars:>100",
    ]

    def search_repos(self, query: str, per_page: int = 10) -> list[RepoCard]:
        url = f"{self.api}/search/repositories"
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_page}
        try:
            r = requests.get(url, headers=self.headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return []
        out: list[RepoCard] = []
        for item in data.get("items", []):
            out.append(RepoCard(
                full_name=item.get("full_name", ""),
                description=item.get("description") or "",
                stars=int(item.get("stargazers_count") or 0),
                url=item.get("html_url", ""),
                language=item.get("language") or "",
                updated_at=item.get("updated_at", ""),
                topics=item.get("topics") or [],
            ))
        return out

    # ------------------------------------------------------------------
    # Fetch README
    # ------------------------------------------------------------------

    def fetch_readme(self, full_name: str) -> str:
        # Try main + master branches + common README filenames
        for branch in ("main", "master"):
            for fname in ("README.md", "README.rst", "README"):
                raw = f"https://raw.githubusercontent.com/{full_name}/{branch}/{fname}"
                try:
                    r = requests.get(raw, timeout=12)
                    if r.status_code == 200 and r.text:
                        return r.text[:40_000]  # cap at 40KB
                except Exception:
                    continue
        return ""

    # ------------------------------------------------------------------
    # Rule-based insight extraction
    # ------------------------------------------------------------------

    def extract_insights_rule_based(self, readme: str) -> list[dict[str, str]]:
        insights = []
        seen = set()
        for pattern, benefit, action in VALUABLE_PATTERNS:
            matches = re.findall(pattern, readme)
            if matches and benefit not in seen:
                seen.add(benefit)
                # Extract a short surrounding context
                match = re.search(pattern, readme)
                start = max(0, match.start() - 80) if match else 0
                end = min(len(readme), match.end() + 200) if match else 300
                context = readme[start:end].strip().replace("\n", " ")
                insights.append({
                    "benefit": benefit,
                    "action": action,
                    "evidence": context[:300],
                })
        return insights

    # ------------------------------------------------------------------
    # LLM-enhanced extraction (optional, requires ANTHROPIC_API_KEY)
    # ------------------------------------------------------------------

    def extract_insights_llm(self, readme: str, repo_name: str) -> list[dict[str, str]]:
        """Claude Code CLI çağrısı — subscription kullanır, API key gerektirmez."""
        from oto_bot.llm import query as claude_query

        prompt = f"""Aşağıda "{repo_name}" adlı bir open-source trading bot repo'sunun README'si var.
Bizim kendi trading bot laboratuvarımız var (Atlas CEO + 18 rule-based ajan, memory pipeline,
risk-adjusted promotion doktrini). Bu repo'dan BİZİM sistemimize eklenebilecek EN FAZLA 3 somut
fikir çıkar. SADECE JSON array döndür (açıklama yazma), şu şemayla:
[{{"benefit": "...", "action": "...", "evidence": "..."}}]

- benefit: faydası (1 cümle)
- action: bizim sistemde nasıl uygulanır (somut, ajan/modül ismiyle; örn: "Crystal Optimizer
  ajanına Bayesian search eklensin")
- evidence: README'deki kanıt cümle (kısa)

README:

{readme[:8000]}
"""
        response = claude_query(prompt, timeout=180)
        if not response:
            return []
        # JSON extract from response (CLI may wrap in markdown fences)
        match = re.search(r"\[\s*\{.*?\}\s*\]", response, re.DOTALL)
        if not match:
            return []
        try:
            items = json.loads(match.group(0))
            # Sanity check shape
            out = []
            for it in items[:3]:
                if isinstance(it, dict) and "benefit" in it:
                    out.append({
                        "benefit": str(it.get("benefit", ""))[:200],
                        "action": str(it.get("action", ""))[:400],
                        "evidence": str(it.get("evidence", ""))[:300],
                    })
            return out
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Full scan
    # ------------------------------------------------------------------

    def scan(self, queries: list[str] | None = None, max_repos: int = 20) -> dict[str, Any]:
        """Birden fazla query ile tarama yapar, insight'lardan proposal üretir."""
        queries = queries or self.DEFAULT_QUERIES
        all_repos: list[RepoCard] = []
        seen_names: set[str] = set()

        for q in queries:
            time.sleep(0.3)  # rate limit friendly
            repos = self.search_repos(q, per_page=5)
            for r in repos:
                if r.full_name not in seen_names and len(all_repos) < max_repos:
                    all_repos.append(r)
                    seen_names.add(r.full_name)

        proposals_created = 0
        for repo in all_repos:
            readme = self.fetch_readme(repo.full_name)
            if not readme or len(readme) < 200:
                continue
            insights = self.extract_insights_rule_based(readme)
            if self.use_llm:
                insights.extend(self.extract_insights_llm(readme, repo.full_name))

            for insight in insights[:3]:  # top 3 per repo
                title = f"{insight['benefit']} — {repo.full_name}"
                summary = (
                    f"⭐ {repo.stars:,} stars · {repo.description[:200]}\n\n"
                    f"Fayda: {insight['benefit']}\n"
                    f"Uygulama: {insight['action']}"
                )
                detail = (
                    f"## {repo.full_name} ({repo.stars:,} stars)\n\n"
                    f"**Açıklama**: {repo.description}\n\n"
                    f"**URL**: {repo.url}\n\n"
                    f"**Topics**: {', '.join(repo.topics)}\n\n"
                    f"### Bulunan fayda\n{insight['benefit']}\n\n"
                    f"### Önerilen uygulama\n{insight['action']}\n\n"
                    f"### Kanıt (README'den)\n> {insight['evidence']}\n"
                )
                proposal = Proposal(
                    proposal_id="",
                    proposal_type="integration",
                    title=title[:200],
                    author_agent="Hermes GitResearcher",
                    summary=summary,
                    detail_markdown=detail,
                    estimated_benefit=insight["benefit"],
                    estimated_risk=(
                        "Dış kod; IP/lisans kontrol gerekli. Mevcut sisteme adaptation "
                        "iş gücü gerekir. Overfitting/regime-fit riski kontrol edilmeli."
                    ),
                    action_steps=[
                        f"Repo incele: {repo.url}",
                        insight["action"],
                        "Cassandra pre-mortem: entegrasyon sonrası overfitting/edge-kaybı senaryoları",
                        "Eğer implement edilirse: stres senaryolarını geçir, 30 gün paper takip",
                    ],
                    source_url=repo.url,
                    metadata={
                        "repo": repo.full_name,
                        "stars": repo.stars,
                        "language": repo.language,
                        "topics": repo.topics,
                    },
                )
                self.queue.submit(proposal)
                proposals_created += 1

        return {
            "repos_scanned": len(all_repos),
            "proposals_created": proposals_created,
            "use_llm": self.use_llm,
        }
