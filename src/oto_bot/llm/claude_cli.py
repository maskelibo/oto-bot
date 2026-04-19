"""Claude Code CLI wrapper.

Kullanımı:
    from oto_bot.llm import query
    response = query("2+2 nedir?", timeout=30)
    # response: str veya None (CLI yoksa / timeout)

Tüm ajanlar rule-based kalıp istediğinde LLM'i opsiyonel destek olarak kullanır.
Sistem LLM çağrısı yapamazsa loop hiç etkilenmemeli.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("oto_bot.llm")


# ---------------------------------------------------------------------------
# Budget tracker (subscription'ı kötüye kullanma — günlük çağrı sayısını log'la)
# ---------------------------------------------------------------------------

_BUDGET_FILE = Path("artifacts/llm_budget.json")


def _read_budget() -> dict[str, Any]:
    if not _BUDGET_FILE.exists():
        return {"today": "", "daily_calls": 0, "total_calls": 0}
    try:
        return json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"today": "", "daily_calls": 0, "total_calls": 0}


def _write_budget(data: dict[str, Any]) -> None:
    _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BUDGET_FILE.write_text(json.dumps(data), encoding="utf-8")


def _inc_budget() -> None:
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    b = _read_budget()
    if b.get("today") != today:
        b["today"] = today
        b["daily_calls"] = 0
    b["daily_calls"] = int(b.get("daily_calls", 0)) + 1
    b["total_calls"] = int(b.get("total_calls", 0)) + 1
    _write_budget(b)


def budget_status() -> dict[str, Any]:
    return _read_budget()


# ---------------------------------------------------------------------------
# CLI client
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCLIResult:
    ok: bool
    text: str
    error: str | None = None
    duration_s: float = 0.0


class ClaudeCLI:
    """Claude Code CLI üzerinden tek-seferlik çağrı."""

    def __init__(
        self,
        binary: str | None = None,
        default_timeout: int = 120,
        daily_cap: int = 100,
    ) -> None:
        # Locate claude binary
        self.binary = binary or shutil.which("claude") or self._common_path()
        self.default_timeout = default_timeout
        self.daily_cap = daily_cap

    @staticmethod
    def _common_path() -> str | None:
        # Windows npm path
        candidates = [
            str(Path.home() / "AppData/Roaming/npm/claude.cmd"),
            str(Path.home() / "AppData/Roaming/npm/claude"),
            "/usr/local/bin/claude",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return None

    def is_available(self) -> bool:
        return bool(self.binary)

    def budget_ok(self) -> bool:
        b = _read_budget()
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if b.get("today") != today:
            return True
        return int(b.get("daily_calls", 0)) < self.daily_cap

    def ask(
        self,
        prompt: str,
        timeout: int | None = None,
        cwd: str | None = None,
        allow_exceed_budget: bool = False,
    ) -> ClaudeCLIResult:
        import time
        if not self.is_available():
            return ClaudeCLIResult(ok=False, text="", error="claude CLI not found")
        if not allow_exceed_budget and not self.budget_ok():
            return ClaudeCLIResult(ok=False, text="", error="daily budget exceeded")
        if not prompt.strip():
            return ClaudeCLIResult(ok=False, text="", error="empty prompt")

        t0 = time.time()
        try:
            # Claude CLI -p flag: print mode, stdin prompt
            result = subprocess.run(
                [self.binary, "-p"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout or self.default_timeout,
                cwd=cwd,
                encoding="utf-8",
                errors="replace",
            )
            _inc_budget()
            duration = time.time() - t0
            if result.returncode != 0:
                return ClaudeCLIResult(
                    ok=False, text=result.stdout or "",
                    error=f"exit {result.returncode}: {result.stderr[:200]}",
                    duration_s=round(duration, 2),
                )
            return ClaudeCLIResult(
                ok=True, text=result.stdout.strip(),
                duration_s=round(duration, 2),
            )
        except subprocess.TimeoutExpired:
            return ClaudeCLIResult(ok=False, text="", error="timeout", duration_s=timeout or 0)
        except Exception as e:
            return ClaudeCLIResult(ok=False, text="", error=str(e))


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


_default_client: ClaudeCLI | None = None


def _get_client() -> ClaudeCLI:
    global _default_client
    if _default_client is None:
        _default_client = ClaudeCLI()
    return _default_client


def query(prompt: str, timeout: int = 120) -> str | None:
    """Basit API: prompt ver, str yanıt al (veya None)."""
    r = _get_client().ask(prompt, timeout=timeout)
    return r.text if r.ok else None
