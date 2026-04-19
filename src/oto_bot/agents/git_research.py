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

Faz 7 — daimi mode:
    EducatorLoop her tetiklemede ``discover_loop_step()`` çağırır. Sınıf,
    ``LOOP_QUERIES`` üzerinde rotasyon yapar — state file
    ``artifacts/git_research_state.json`` hangi query / repo işlendiğini
    saklar. Her step: 1 query × top-5 repo × en değerli 3 finding → lesson
    + proposal. Aynı repo tekrar tekrar incelenmez.

GitHub unauth API rate limit: 60 request/saat. Yeterli.
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
from urllib.parse import quote

import requests

from oto_bot.agents.proposals import Proposal, ProposalQueue
from oto_bot.core.models import Lesson
from oto_bot.memory.journal import LearningJournal


# State file: hangi query çalıştırıldı, hangi repo işlendi
_STATE_PATH = Path("artifacts/git_research_state.json")


def _load_state() -> dict[str, Any]:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    """State'i atomik olarak diske yaz."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Loop query rotasyonu — daimi mode
# ---------------------------------------------------------------------------

LOOP_QUERIES: list[str] = [
    "trading bot python mean reversion",
    "algorithmic trading strategy crypto",
    "forex price action python",
    "swing trading indicators backtest",
    "machine learning trading github",
    "quantitative trading library python",
    "high frequency trading python",
    "momentum strategy backtest python",
    "options trading python",
    "portfolio optimization python",
    "reinforcement learning trading",
    "binance trading bot python",
    "metatrader python strategy",
    "factor investing python",
    "statistical arbitrage python",
    "market making bot python",
    "trading signals python neural network",
    "risk management trading python",
    "vectorbt strategy",
    "backtrader strategy python",
]


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
        journal: LearningJournal | None = None,
        use_llm: bool = False,
        user_agent: str = "oto-bot-hermes-git-researcher",
    ) -> None:
        self.queue = queue or ProposalQueue()
        # journal opsiyonel — daimi mode'da finding'ler lesson olarak da yazılır.
        self.journal = journal or LearningJournal()
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

    # ------------------------------------------------------------------
    # Faz 7 — Daimi mode: rotated single-query step
    # ------------------------------------------------------------------

    def _emit_finding_lesson(
        self,
        repo: RepoCard,
        insight: dict[str, str],
        query: str,
    ) -> str | None:
        """README finding'ini journal'a kompakt bir lesson olarak yaz."""
        try:
            content = (
                f"[{repo.full_name} ⭐{repo.stars}] {insight['benefit']} — "
                f"Uygulama: {insight['action']}"
            )[:600]
            lesson = Lesson(
                lesson_id="",
                author_agent="Hermes GitResearcher",
                content=content,
                tags=[
                    "source:git_research",
                    f"source_domain:github.com",
                    f"repo:{repo.full_name}",
                    f"query:{query[:60]}",
                ],
                market="*",
                strategy_family="*",
                regime="*",
                symbol="*",
                severity="info",
                evidence_experiment_id=None,
                source_cycle=0,
            )
            return self.journal.save(lesson)
        except Exception:
            return None

    def discover_loop_step(
        self,
        max_repos: int = 5,
        max_findings_per_repo: int = 3,
    ) -> dict[str, Any]:
        """Tek bir query rotasyonu — EducatorLoop her tetiklemede bunu çağırır.

        State: artifacts/git_research_state.json
            {"cursor": int, "processed_repos": {full_name: ts_iso, ...},
             "history": [...]}.
        Aynı repo daha önce işlendiyse skip (yeni README değişikliği için
        cooldown 30 gün — pratikte loop bu süreden önce repo listesini
        bitirip dönmez).
        """
        state = _load_state()
        cursor = int(state.get("cursor", 0)) % max(len(LOOP_QUERIES), 1)
        processed: dict[str, str] = state.setdefault("processed_repos", {})
        history: list[dict[str, Any]] = state.setdefault("history", [])

        query = LOOP_QUERIES[cursor]
        started_at = datetime.now(timezone.utc).isoformat()
        repos_seen = 0
        repos_skipped = 0
        proposals_created = 0
        lessons_created = 0
        sample: list[dict[str, Any]] = []
        error: str | None = None

        try:
            repos = self.search_repos(query, per_page=max_repos)
        except Exception as exc:  # noqa: BLE001
            repos = []
            error = f"search_failed: {type(exc).__name__}: {exc}"

        for repo in repos[:max_repos]:
            if not repo.full_name:
                continue
            repos_seen += 1
            if repo.full_name in processed:
                repos_skipped += 1
                continue
            try:
                readme = self.fetch_readme(repo.full_name)
            except Exception:
                readme = ""
            if not readme or len(readme) < 200:
                processed[repo.full_name] = datetime.now(timezone.utc).isoformat()
                continue
            insights = self.extract_insights_rule_based(readme)
            if self.use_llm:
                try:
                    insights.extend(self.extract_insights_llm(readme, repo.full_name))
                except Exception:
                    pass

            for insight in insights[:max_findings_per_repo]:
                # Lesson kaydı
                lid = self._emit_finding_lesson(repo, insight, query)
                if lid:
                    lessons_created += 1
                # Proposal kaydı
                try:
                    proposal = Proposal(
                        proposal_id="",
                        proposal_type="integration",
                        title=f"{insight['benefit']} — {repo.full_name}"[:200],
                        author_agent="Hermes GitResearcher",
                        summary=(
                            f"Star {repo.stars:,} · {repo.description[:200]}\n"
                            f"Fayda: {insight['benefit']}\n"
                            f"Uygulama: {insight['action']}"
                        ),
                        detail_markdown=(
                            f"## {repo.full_name} ({repo.stars:,} stars)\n\n"
                            f"**Query**: {query}\n\n"
                            f"**URL**: {repo.url}\n\n"
                            f"### Fayda\n{insight['benefit']}\n\n"
                            f"### Uygulama\n{insight['action']}\n\n"
                            f"### Kanıt\n> {insight['evidence']}\n"
                        ),
                        estimated_benefit=insight["benefit"],
                        estimated_risk="IP/lisans kontrol; entegrasyon iş gücü; overfitting riski.",
                        action_steps=[
                            f"Repo incele: {repo.url}",
                            insight["action"],
                            "Cassandra pre-mortem",
                        ],
                        source_url=repo.url,
                        metadata={
                            "repo": repo.full_name,
                            "stars": repo.stars,
                            "language": repo.language,
                            "topics": repo.topics,
                            "loop_query": query,
                        },
                    )
                    self.queue.submit(proposal)
                    proposals_created += 1
                except Exception:
                    pass

                if len(sample) < 5:
                    sample.append({
                        "repo": repo.full_name,
                        "stars": repo.stars,
                        "benefit": insight["benefit"],
                    })
            processed[repo.full_name] = datetime.now(timezone.utc).isoformat()

        # Cursor ilerlet (bir sonraki tetiklemede yeni query)
        state["cursor"] = (cursor + 1) % len(LOOP_QUERIES)
        state["last_query"] = query
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["processed_repos"] = processed
        history.append({
            "ts": started_at,
            "query": query,
            "repos_seen": repos_seen,
            "repos_skipped": repos_skipped,
            "proposals_created": proposals_created,
            "lessons_created": lessons_created,
            "error": error,
        })
        # Son 50 step'i tut — state dosyası şişmesin
        state["history"] = history[-50:]
        _save_state(state)

        return {
            "query": query,
            "cursor_next": state["cursor"],
            "repos_seen": repos_seen,
            "repos_skipped": repos_skipped,
            "proposals_created": proposals_created,
            "lessons_created": lessons_created,
            "sample": sample,
            "error": error,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    # Faz 7 — task brief'te kullanılan alternatif isim.
    def search_github(self, query: str, max_repos: int = 5) -> list[RepoCard]:
        """Tek bir query çalıştır — alias of search_repos."""
        return self.search_repos(query, per_page=max_repos)


# Faz 7 — kısa alias (EducatorLoop ve smoke testler için).
GitResearcher = HermesGitResearcher

__all__ = [
    "GitResearcher",
    "HermesGitResearcher",
    "LOOP_QUERIES",
    "RepoCard",
]
