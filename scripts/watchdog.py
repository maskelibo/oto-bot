"""Watchdog — orchestrator + webui'yi izler, düşerse otomatik restart.

Faz 8: gece boyunca PC açık olduğu sürece sistem ayakta kalsın.

İzlenenler:
* **Orchestrator**: ``artifacts/current_cycle.json`` heartbeat dosyası 90 sn'den
  eskiyse process ölmüş kabul edilir → restart.
* **Webui**: ``http://127.0.0.1:8501/api/current`` HTTP 200 mü? Değilse restart.

Periyot: 30 sn. İlk 10 sn'de süreçleri spawn et, sonra döngüye gir.
KeyboardInterrupt ile temiz çıkış (child process'ler de durdurulur).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
HEARTBEAT = ARTIFACTS / "current_cycle.json"
WEBUI_HEALTH_URL = "http://127.0.0.1:8501/api/current"
HEARTBEAT_STALE_SEC = 90.0
CHECK_INTERVAL_SEC = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOGS / "watchdog.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watchdog")


def _spawn_orchestrator() -> subprocess.Popen:
    log_path = LOGS / f"orchestrator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    cmd = [
        PYTHON, "-m", "oto_bot.main", "autonomous",
        "--markets", "crypto,forex,us_equities,bist",
        "--strategies", "day,swing,scalp",
        "--max-cycles", "100000",
        "--pause", "2.0",
    ]
    logger.info(f"spawn orchestrator → {log_path.name}")
    return subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=open(log_path, "w", encoding="utf-8", buffering=1),
        stderr=subprocess.STDOUT, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def _spawn_webui() -> subprocess.Popen:
    log_path = LOGS / f"webui_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    # Doğrudan .venv'in uvicorn.exe'sini çalıştır — `python -m uvicorn` Windows'ta
    # PATH resolver yüzünden ekstra sistem-python child spawn ediyor (port çakışması).
    uvicorn_exe = ROOT / ".venv" / "Scripts" / "uvicorn.exe"
    if uvicorn_exe.exists():
        cmd = [str(uvicorn_exe), "webui.server:app", "--host", "127.0.0.1", "--port", "8501"]
    else:
        cmd = [PYTHON, "-m", "uvicorn", "webui.server:app", "--host", "127.0.0.1", "--port", "8501"]
    logger.info(f"spawn webui → {log_path.name}")
    return subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=open(log_path, "w", encoding="utf-8", buffering=1),
        stderr=subprocess.STDOUT, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def _orchestrator_alive() -> bool:
    """Heartbeat dosyası 90 sn'den eski değilse alive."""
    if not HEARTBEAT.exists():
        return False
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ts = data.get("timestamp", "")
        if not ts:
            return False
        # Z, +00:00 formatlarını tolere et
        ts = ts.replace("Z", "+00:00")
        last = datetime.fromisoformat(ts)
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < HEARTBEAT_STALE_SEC
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"heartbeat parse failed: {exc}")
        return False


def _webui_alive() -> bool:
    """HTTP 200 mü?"""
    try:
        with urllib.request.urlopen(WEBUI_HEALTH_URL, timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"webui health check error: {exc}")
        return False


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    logger.info("=== watchdog start ===")
    orch = _spawn_orchestrator()
    time.sleep(3)
    web = _spawn_webui()
    time.sleep(5)

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SEC)

            # Orchestrator
            if not _orchestrator_alive():
                logger.warning(f"orchestrator stale (heartbeat>{HEARTBEAT_STALE_SEC}s) → restart")
                _kill(orch)
                orch = _spawn_orchestrator()
                time.sleep(5)
            elif orch.poll() is not None:
                logger.warning(f"orchestrator process exited code={orch.returncode} → restart")
                orch = _spawn_orchestrator()
                time.sleep(5)

            # Webui
            if not _webui_alive():
                logger.warning("webui not responding → restart")
                _kill(web)
                web = _spawn_webui()
                time.sleep(5)
            elif web.poll() is not None:
                logger.warning(f"webui process exited code={web.returncode} → restart")
                web = _spawn_webui()
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("watchdog stopping (Ctrl+C)")
        _kill(orch)
        _kill(web)
        return 0


if __name__ == "__main__":
    sys.exit(main())
