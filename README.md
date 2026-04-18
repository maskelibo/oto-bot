# oto-bot

Anahtar teslim çok ajanlı trade araştırma ve otomasyon laboratuvarı.

Bu repo, Claude Code içinde çalışacak şekilde tasarlanmış bir proje iskeletidir. Amaç; kripto, Forex, ABD hisseleri ve BIST için üç strateji ailesi üzerinde (day trader, swing trader, scalper) sürekli araştırma, backtest, deney kaydı, hafıza yönetimi ve paper trading akışını tek bir organizasyon altında yürütmektir.

## Çekirdek fikir
- **CEO ajan** ana muhataptır.
- CEO, ihtiyaca göre yeni ajan yaratabilir, mevcut ajanların görevini değiştirebilir veya ajanları devreden çıkarabilir.
- Tüm deneyler kayıt altına alınır.
- Geçmiş başarısız denemeler silinmez; özetlenir ve yeniden kullanım için indekslenir.
- Varsayılan mod **research / backtest / paper trading** odaklıdır.
- Canlı işlem entegrasyonları için arayüzler hazırdır, fakat varsayılan olarak kapalıdır.

## Hızlı başlangıç
```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m oto_bot.main bootstrap
PYTHONPATH=src python -m oto_bot.main cycle --market crypto --strategy day
```

## Önemli not
Bu repo üretime hazır kârlılık garantili bir trade motoru değil, güçlü bir araştırma ve otomasyon omurgasıdır. Gerçek para ile işlem açmadan önce broker adaptörleri, veri kalitesi, regülasyon ve risk limitleri ayrıca doğrulanmalıdır.
