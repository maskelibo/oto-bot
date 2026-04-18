#!/usr/bin/env bash
set -e
PYTHONPATH=src python -m oto_bot.main bootstrap
PYTHONPATH=src python -m oto_bot.main cycle --market crypto --strategy day
PYTHONPATH=src python -m oto_bot.main hire --name "Orion RegimeScout" --role "Regime Specialist" --department "Research" --mandate "Track market regimes and session transitions."
