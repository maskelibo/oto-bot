"""CurriculumLoader — Türkçe/İngilizce açık kaynak trader derslerini ingest eder.

Kaynaklar (başlangıç):
    - borsaninizinden.com : Türkçe, İbrahim Babadağı'nın kaleme aldığı teknik analiz + price action dersleri
    - (opsiyonel) investopedia / babypips : İngilizce
    - (opsiyonel) PDF trader kitapları

İki mod:
    - rule_based : keyword + pattern extraction → kısa lesson
    - llm_enhanced : Claude CLI → 3 yapılandırılmış insight

Her ders → LearningJournal'a `author:Talos Curriculum` + `source:curriculum:<domain>`
tag'leriyle yazılır. Nova hipotez üretirken bu dersleri retriever ile çeker
(memory tarafında source:curriculum match'i ekstra bonus puan alır).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests
try:
    from bs4 import BeautifulSoup  # beautifulsoup4 zaten kurulu (streamlit deps)
except ImportError:
    BeautifulSoup = None  # type: ignore

from oto_bot.agents.proposals import Proposal, ProposalQueue
from oto_bot.core.models import Lesson
from oto_bot.memory.journal import LearningJournal


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
