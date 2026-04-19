# OTO-BOT Mimari Evrim Planı

> Opus 4.7 tarafından hazırlandı · 2026-04-19

---

## 1. Şimdiki durum — dürüst tanı

### Sağlam yanları
- **Kurumsal doktrin**: Head of Trading zihniyeti, 8-sesli panel, pod modeli, committee yapısı — az laboratuvarda bu kalitede bir governance modeli var.
- **Memory pipeline**: LearningJournal + Retriever + InsightExtractor — token-ekonomik ve context-aware.
- **Backtest motoru**: margin, slippage, ATR trailing, circuit breaker, partial TP, funding rate — gerçekçi.
- **Gözlemlenebilirlik**: FastAPI dashboard, live heartbeat, graph'lar, çoklu-horizon projeksiyon — development UX güçlü.
- **HR motoru**: şirket gibi otomatik hire/fire heuristiği.

### Zayıf yanları (öncelikli)
| Sorun | Etki | Zorluk |
|---|---|---|
| Parametre araması random | Edge'i bulmak 10x daha yavaş | orta |
| Ajanlar rule-based — yaratıcılık yok | Novel strateji üretilmiyor, sadece template mutasyonu | büyük |
| Promotion eşikleri statik (Sharpe≥1.2) | Gerçek forward-performance'a göre uyarlanmıyor | orta |
| `walkforward_sharpe` ve `montecarlo_95_drawdown` her zaman `None` | Robustness gate gerçekten koşmuyor | kolay |
| Stres lab var ama orchestrator çağırmıyor | Dead code, promote'lar stres geçmiyor | kolay |
| Rejim ölçülüyor ama hipotez seçiminde kullanılmıyor | Scalp trend'de test ediliyor, boşa cycle | orta |
| Korelasyon limiti var ama hesaplanmıyor | Portföy diversifikasyonu hayali | orta |
| CEO rule-based — novel durumlara cevap yok | Aynı kalıptan çıkan öneriler | büyük |
| Paper_trader mevcut ama orchestrator'a bağlı değil | Promote olanlar "live paper" aşamasına geçmiyor | orta |
| Memory retrieval tag-only | "range mean-reversion" ile "sideways fade" ilişkisi görülmüyor | orta |

### Eksik (henüz yok)
- Gerçek **walk-forward validation** her cycle'da
- Gerçek **Monte Carlo drawdown** simülasyonu
- **Bayesian optimization** yerine random search
- **Ensemble/voting** — birden fazla stratejinin aynı sembolde konsensüsü
- **Feature engineering** — sadece standart indikatörler var
- **Meta-learner** — ne tür pattern'lerin winner olduğunu izleyen bir ajan
- **Live paper trading loop** — promote sonrası 30-günlük forward watch
- **LLM augmentation hook** — Claude/Opus ile novel hipotez üretimi (kontrollü, budget-aware)
- **External curriculum** — borsanınizinden.com, babypips, investopedia PDF ingest
- **Multi-asset portfolio optimizer** — HRP, risk parity, Markowitz
- **Kayıp stopları arası hiyerarşi** — pod / market / book seviyesi entegre değil

---

## 2. Hedef durum — nereye gidiyor

Oto-bot bir **self-improving research lab** olmalı:
- **Agent'lar yaratıcı**: LLM ile novel hipotez + Bayesian optimize ile hızlı arama
- **Promosyon gerçek**: Backtest → Walk-forward → Monte Carlo → Stres → Paper (30 gün) → Approval
- **Portföy seviyesinde**: pod'lar gerçekten korelasyonsuz, VaR-bounded, risk-budgeted
- **Sürekli öğrenen**: Forward performance statik eşiği güncelliyor, başarısızlık pattern'leri otomatik filtrelere dönüşüyor
- **Hızlı iterasyon**: Cycle'lar kısa, pipeline paralel, bottleneck yok
- **Dış kaynaklardan besleniyor**: İnternet'teki TA bilgisi journal'a akıyor
- **Gözlemlenebilir**: Her agent'ın ne ürettiği, neyin geliştiğini görüyorsun

---

## 3. Dört fazlı evrim yol haritası

Her faz ~1-3 hafta kod. Toplam 8-12 hafta. Öncelik: **risk-adjusted kazanım sağlayan doğru kararlar ver, hızlı bul, canlı kanıtla**.

### Faz 1 — Robustness ve gerçek promotion (1 hafta)
**Hedef**: mevcut promote'lar gerçekten güvenilir olsun.

1. `BacktestEngine.walk_forward()` **her cycle'da** çağrılsın → `walkforward_sharpe` dolsun.
2. `BacktestEngine.monte_carlo_dd()` eklensin (trade shuffling 1000x) → `montecarlo_95_drawdown` dolsun.
3. Orchestrator her promote adayı için `StressLab.run_all()` çağırsın → promote için min 4/6 senaryo `survived`.
4. RiskGate WF + MC kontrolünü de uygulasın.

**Etkisi**: Promote edilen stratejiler forward'da daha tutarlı. Yanlış pozitifler %50+ azalır.

### Faz 2 — Akıllı parametre ve hipotez üretimi (2 hafta)
**Hedef**: random search'ten çık, 10x daha hızlı edge bul.

1. **Bayesian optimizer** modülü (`skopt` veya `optuna`) — her (strategy, market) için param alanını model-guided ara.
2. **Regime-conditioned hypothesis**: Nova yalnızca o piyasanın güncel rejimine uygun stratejileri önersin.
3. **Meta-learner agent** (yeni ajan: Echo MetaLearner): her ayın sonunda hangi param region'larının winner ürettiğini öğrenip Nova'ya öncelik haritası versin.
4. **Ensemble combiner**: aynı sembolde 3 stratejinin çoğunluk oyu → yeni "sinyal sınıfı".

**Etkisi**: Cycle başına expected winner oranı %5 → %20'ye çıkabilir.

### Faz 3 — Paper trading loop + portföy optimizasyonu (2 hafta)
**Hedef**: backtest cenneti değil, forward kanıt.

1. **Promote edilen strateji → otomatik pod açılır** (zaten PodAllocator var ama tetiklenmiyor). 30 gün boyunca paper trading'de izlenir.
2. **Live metric sapması alarmı**: paper Sharpe backtest Sharpe'ın %50'sinin altına düşerse CEO otomatik pod halve.
3. **Portföy optimizer** (HRP veya risk parity): pod'lar arası korelasyon matrisini aktif hesaplayıp sermaye kayar.
4. **Apex'e real VaR** entegrasyonu: mevcut book getiri serisi üzerinden günlük VaR limiti.

**Etkisi**: Kağıt üstü güzel ama live'da çöken stratejiler erken yakalanır. Portföy gerçekten diversifiye.

### Faz 4 — LLM augmentation + external data (3 hafta, opsiyonel budget)
**Hedef**: Yaratıcılık sınırını kır.

1. **Claude API hook** (budget-aware): Nova'nın hipotez üretimini ayda N kez Claude'a sor — "mevcut memory'deki dersler + şu an winner'lar verildi, yeni bir hipotez archetype öner".
2. **Debate LLM augmentation**: panel debate'lerine ayda N kez Claude katılsın (Opus) — daha derin çelişki.
3. **ResearchScraper**: borsanınizinden.com + babypips + investopedia → TA kuralları journal'a "curriculum:" tag'li dersler olarak.
4. **News/sentiment layer** (hafif): finans haberleri → günlük market_context features.

**Etkisi**: Novel strateji archetypeleri üretmek mümkün. Dış bilgi birikimi avantaja dönüşür.

---

## 4. Agent mimarisi yeniden tasarımı

### Mevcut kadro
18 ajan, hepsi rule-based Python. Her biri deterministic fonksiyon çağrısı. Debate = 8 perspektif şablonu.

### Önerilen yenilikler

#### Yeni ajanlar
| Ajan | Görev | Katman |
|---|---|---|
| **Echo MetaLearner** | "Ne tür parametre kümeleri winner üretti?" → Nova'ya öneri haritası | Research |
| **Crystal Optimizer** | Bayesian parametre araması (Optuna backend) | Research |
| **Zephyr ForwardWatch** | Paper trading stage'indeki pod'ları forward-track eder | Execution |
| **Talos CurriculumLoader** | External TA kaynaklarını çekip journal'a dönüştürür | Knowledge |
| **Sable Ensembler** | Aynı sembolde çoklu strateji sinyallerini birleştirir | Analytics |
| **Lumina Causal** | Bir trade'in hangi feature'larca açıklandığını analiz eder | Analytics |

#### Mevcut ajanların evrimi
- **Nova StrategyRND**: Artık regime-conditioned + MetaLearner yönlendirmesi + (opsiyonel) LLM augmentation.
- **Atlas CEO**: Promotion eşikleri **adaptive** olur — forward performance'tan öğrenerek kendi eşiklerini günceller.
- **Apex PortfolioRisk**: Gerçek VaR + canlı korelasyon matrisi + Risk Parity pod sizing.
- **Helix Backtest**: Her cycle'da WF + MC otomatik çalıştırır.
- **Sigma Quant**: Statistical significance testing güçlendirilir (bootstrap CI, deflated Sharpe).
- **Mercury Macro**: Gerçek macro data feeds (FRED API) — şu an proxy'ler var.

#### Debate evrim
8 perspektif şablonundan → **dinamik debate**:
- Her cycle'da 3 rastgele perspektif "devil's advocate" rolü alır (mevcut görüşünün tersini savunur).
- Sonuç daha robust olur çünkü herkes kendi söyleminin sağlamlığını test etmeli.
- LLM opsiyonu: ayda N kez Claude debate'e katılır, novel bir çelişki çıkarır.

---

## 5. Pipeline yeniden yapılanma

### Mevcut cycle
```
pick_experiment → fetch_data → backtest → risk_gate → score → memory → CEO review (→ debate) → log
```

### Önerilen cycle (v2)
```
pick_experiment (regime-conditioned + Bayesian next-best)
  → fetch_data
  → backtest
  → walk_forward (YENİ, her cycle)
  → monte_carlo_dd (YENİ, her cycle)
  → risk_gate (mevcut + WF/MC)
  → stress_lab (YENİ, promote aday ise)
  → score (composite + forward-weighted)
  → memory (mevcut + insight)
  → CEO review + dynamic debate
  → IF promoted:
      open_pod (Ledger)
      start_paper_watch (Zephyr, 30 gün)
      rebalance_portfolio (HRP)
  → attribution + learning_curve update
```

Ek olarak **asenkron** çalışacak iki loop:
- **Curriculum loop** (haftalık): Talos external content çeker, yeni dersler ekler.
- **Meta-learning loop** (günlük): Echo son N cycle'ı analiz eder, Nova'ya öncelik haritası günceller.

---

## 6. Memory mimarisi evrim

### Şu an
- `lessons` tablosu
- Tag tabanlı retrieval
- Recency + severity + reference scoring

### Eklenecek
1. **Semantic retrieval** (opsiyonel, lokal embed model veya Claude embed):
   - Her lesson için 384-boyutlu embed
   - Cosine similarity ile "aynı fikir farklı kelimelerde" yakalanır
   - Örn: "WR yüksek PF düşük" ~ "çok küçük kazanç az büyük kayıp" aynı cluster'a düşer.

2. **Causal memory**: trade → PnL ilişkisini feature bazında öğren.
   - "RSI<30 + düşen hacim → sonraki 24 saatte %-2 beklenti"

3. **Strategy genome store**: her winner strateji bir "genome" olur (family + params + constraints). Yeni hipotezler mevcut genome'lardan crossover/mutasyon ile üretilir (genetik algoritma).

4. **Curriculum store**: dış kaynaklardan çekilen TA kuralları ayrı tag'lı memory alanında ("source:curriculum"). Nova hipotez üretirken curriculum + own-lessons karışımından besleniyor.

5. **Forgetting policy evrim**: şu an "90 gün + 0 reference → sil". Yeni: "forward validation'da başarısız çıkmış dersler yüksek severity'de kalır; success lessons daha agresif prune".

---

## 7. LLM entegrasyon stratejisi

**Anti-prensip**: Her şeye LLM değil. Çoğu iş rule-based hızlı ve ücretsiz.

**LLM değer katan 5 nokta** (budget-aware, günde/ayda X çağrı limiti):
1. **Novel hipotez archetype üretimi** (Nova'ya destek, ayda 50-100 çağrı): "Aşağıdaki memory + winner pattern + market context verildi, yeni bir strateji archetype öner."
2. **Debate derinleşmesi** (haftalık, 20-30 çağrı): bir promote adayı için Claude "ne kötü gidebilir?" sorusunu derinlemesine açar.
3. **Post-mortem**: retired pod'lar için LLM "neden başarısız oldu?" analizi yazar → lesson olarak kaydet.
4. **Curriculum interpretation**: scrape edilen TA articles'ı LLM ile structured rule'a dönüştür.
5. **Committee sunumları**: haftalık investment committee'nin yazılı özeti LLM tarafından yazılır (insana okunur).

**Maliyet kontrol**: `LLMBudgetManager` modülü günlük/aylık token limiti tutar. Limit dolarsa sessizce fallback yap (rule-based version'a).

---

## 8. Veri + research katmanı

### Şu an
- ccxt (crypto)
- yfinance (forex + equities)
- Sentetik fallback

### Eklenecek
1. **Binance websocket** (gerçek zamanlı paper trading için)
2. **FRED API** (macro: Fed rate, DXY, VIX)
3. **Borsanınizinden.com PDF scraper** (TA eğitim içeriği)
4. **Babypips modules scraper** (forex temel TA)
5. **News feeds** (RSS/Atom — Reuters FX, Cointelegraph) → sentiment-lite features
6. **On-chain data** (Glassnode free tier — BTC dominance, exchange flows)

### Feature engineering layer (yeni)
Raw OHLCV yetersiz. Ekleyeceğimiz:
- Volume profile + VWAP bantları
- Order book imbalance (binance free feed)
- Funding rate + OI (perp)
- Market breadth (S&P için)
- Volatility smile proxy
- Regime features (HMM state probabilities — Regime Oracle'dan)

---

## 9. Execution katmanı evrim

### Mevcut
- Forge paper_trader mevcut
- Orchestrator'a bağlı değil
- Live execution yok

### Fazlara göre execution path

**Faz 1**: Promote olanlar otomatik pod açar (Ledger), 30 gün forward paper'da izlenir.
**Faz 2**: Paper vs backtest sapma alarmı — live degradation tespiti.
**Faz 3**: **Broker adapter interface** — şu an fake. Yeni: Binance testnet adapter (emir gönderen, ama sadece testnet). Gerçek ordera geçmek için **açık bir human approval flag**.
**Faz 4** (ileri): Küçük tutarla live trading (örn. $500) — stress-tested strategies only, kill-switch hyper-tight.

**Hiçbir zaman**:
- Emir göndermeden 30 gün paper kanıtı
- User explicit olarak "go live" flag'ini elle set etmeden
- Kill-switch %5 günlük kayıp

---

## 10. Risk + anti-goal

**Yapmayacaklarımız:**
1. ❌ **Aşırı otomasyon** — CEO her yaratıcı kararı LLM'e devretmeyecek. Rule-based 80% zaten yeterli.
2. ❌ **"En karlı" stratejiye sürü davranışı** — risk-adjusted skora ekstra **diversity bonus** ekle: portföy korelasyonu düşük olanlar ödüllendirilir.
3. ❌ **Overfitting'e karşı kör** — WF + MC + stress ALL mandatory. Tek birinde başarı promote için yetmez.
4. ❌ **Tek market bağımlılığı** — her promote için min 2 market koşulması önerilir (robustness tagging).
5. ❌ **LLM'i kritik yola koyma** — LLM down olduğunda loop hiç etkilenmemeli. LLM sadece "enrichment" katmanı.
6. ❌ **Gerçek para oynama** — tüm evrim paper trading içinde. "Go live" ayrı approval.

---

## 11. Somut sıradaki 5 adım (önerim)

Bu turda yapmak istersen, en yüksek impact-per-effort:

1. **`BacktestEngine.walk_forward()` ve `monte_carlo_dd()` çağrılarını her cycle'a bağla** (1-2 gün)
   - Impact: `walkforward_sharpe` ve `montecarlo_95_drawdown` artık dolu olacak
   - Cassandra pre-mortem + Sigma Quant gerçekten iş yapacak

2. **Stress Lab'i orchestrator'a bağla** (1 gün)
   - Impact: promote adayları stres geçmeden promote olmuyor
   - Dead code diriliyor

3. **Regime-conditioned hypothesis picker** (1 gün)
   - `_pick_next_experiment` güncel rejim okur, uymayan (strategy, regime) kombinasyonlarını skip eder
   - Impact: wasted cycles %30+ azalır

4. **Bayesian parametre optimizer** (Optuna, 2-3 gün)
   - Random search yerine model-guided. Aynı kompuların %5-10'unda winner.
   - Impact: 10x hızlanma

5. **Adaptive promotion thresholds** (1-2 gün)
   - Atlas CEO son 500 cycle'ın forward performance'ına göre kendi Sharpe eşiğini güncellesin
   - Impact: Eşik artık evrenseli değil, lab'in gerçek edge seviyesi

Bu 5 madde toplam ~7 gün kod = sistemin promotion kalitesi dramatik artar.

---

## Özet

Oto-bot şu an **kaliteli bir araştırma iskeleti**. Eksik olan: gerçek **robustness gating** (WF/MC/stres), **akıllı arama** (Bayesian), **forward doğrulama** (paper watch), **dış bilgi** (curriculum). Bunlar eklendiğinde bir **kendi başına gelişen risk-ayarlı bot üretim laboratuvarı** oluyor — %60 yıllık ROI değil, **sürdürülebilir %20-30 risk-ayarlı** hedefi için uygun altyapı.

%60 ROI olursa da sistem onu yakalar; **olmazsa gürültüyü alfa diye sunmaz**. Mevcut doktrin zaten bunu söylüyor.
