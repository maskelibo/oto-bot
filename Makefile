.PHONY: bootstrap cycle autonomous brief suggest dashboard test

bootstrap:
	PYTHONPATH=src python -m oto_bot.main bootstrap

cycle:
	PYTHONPATH=src python -m oto_bot.main cycle --market crypto --strategy day

autonomous:
	PYTHONPATH=src python -m oto_bot.main autonomous --markets "crypto,forex" --strategies "day,swing,scalp" --max-cycles 50

brief:
	PYTHONPATH=src python -m oto_bot.main brief

suggest:
	PYTHONPATH=src python -m oto_bot.main suggest

dashboard:
	PYTHONPATH=src python -m oto_bot.main dashboard

test:
	pytest -q
