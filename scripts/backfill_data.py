"""5 yıllık veri backfill CLI.

Kullanım:
    .venv/Scripts/python.exe scripts/backfill_data.py
    .venv/Scripts/python.exe scripts/backfill_data.py --years 10
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oto_bot.data.downloader import backfill_all


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=float, default=5.0, help="Geri gidilecek yıl sayısı (default 5)")
    args = p.parse_args()

    print(f"=== Backfill başlıyor: {args.years} yıl ===\n")
    summary = backfill_all(years=args.years, verbose=True)
    print()
    print("=== Özet ===")
    print(f"Toplam bar indirildi: {summary['total_bars']:,}")
    print(f"Başarılı market: {summary['market_counts']}")
    if summary["failed"]:
        print(f"Başarısız: {len(summary['failed'])}")
        for f in summary["failed"][:10]:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
