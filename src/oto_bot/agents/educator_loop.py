"""EducatorLoop — Talos Curriculum + Hermes GitResearcher daimi orchestrator.

Faz 7 — eğitim agent'larını ana cycle'ı bloke etmeden sürekli koştur.

İki ayrı thread:
    * **Talos thread** (default 30 dk): ``CurriculumLoader.loop_step()`` —
      borsaninizinden + babypips + investopedia + litefinance kaynaklarından
      birer sayfa çek, lesson olarak journal'a yaz.
    * **Hermes thread** (default 60 dk): ``GitResearcher.discover_loop_step()`` —
      ``LOOP_QUERIES`` üzerinde rotasyonla 1 query × top-5 repo × top-3 finding
      → proposal + lesson.

Tasarım kuralları:
    * Daemon thread (process kapanırken otomatik biter).
    * ``threading.Event`` ile graceful stop — döngü içindeki uzun sleep'ler
      ``stop_event.wait(timeout)`` ile interrupt edilebilir.
    * Hata düşmesi thread'i öldürmez: try/except → log → 5 dk bekle → tekrar.
    * Status payload (``status()``) sürekli güncellenir; webui /api/educator/status
      bunu okur.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from oto_bot.agents.curriculum import TalosCurriculumLoader
from oto_bot.agents.git_research import HermesGitResearcher
from oto_bot.agents.proposals import ProposalQueue
from oto_bot.memory.journal import LearningJournal


logger = logging.getLogger("oto_bot.educator_loop")


# Hata sonrası bekleme süresi — thread düşmesin ama spam loop'a da girmesin.
_ERROR_BACKOFF_SEC = 300.0  # 5 dk


class EducatorLoop:
    """Eğitim agent'larını arka planda daimi koşturur.

    Parameters
    ----------
    journal:
        ``LearningJournal`` — Talos ve Hermes ders kaydı için.
    proposals:
        ``ProposalQueue`` — Hermes finding'ler ve curriculum bildirim önerileri.
    talos:
        ``TalosCurriculumLoader`` örneği. None ise default kurulur.
    hermes:
        ``HermesGitResearcher`` örneği. None ise default kurulur.
    talos_interval_sec:
        Talos loop_step çağrıları arası bekleme (default 30 dk).
    hermes_interval_sec:
        Hermes discover_loop_step çağrıları arası bekleme (default 60 dk).
    initial_delay_sec:
        İlk tetiklemeyi geciktir — process startup spam olmasın (default 60 sn).
    """

    def __init__(
        self,
        journal: LearningJournal | None = None,
        proposals: ProposalQueue | None = None,
        talos: TalosCurriculumLoader | None = None,
        hermes: HermesGitResearcher | None = None,
        talos_interval_sec: float = 1800.0,   # 30 dk
        hermes_interval_sec: float = 3600.0,  # 60 dk
        initial_delay_sec: float = 60.0,
    ) -> None:
        self.journal = journal or LearningJournal()
        self.proposals = proposals or ProposalQueue()
        # Bağımlılıklarda journal/proposals tutarlı olsun
        self.talos = talos or TalosCurriculumLoader(
            journal=self.journal,
            queue=self.proposals,
        )
        self.hermes = hermes or HermesGitResearcher(
            queue=self.proposals,
            journal=self.journal,
        )
        self.talos_interval_sec = max(60.0, float(talos_interval_sec))
        self.hermes_interval_sec = max(60.0, float(hermes_interval_sec))
        self.initial_delay_sec = max(0.0, float(initial_delay_sec))

        self._stop_event = threading.Event()
        self._talos_thread: threading.Thread | None = None
        self._hermes_thread: threading.Thread | None = None

        # Lock altında değişen status alanları
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "started_at": None,
            "stopped_at": None,
            "running": False,
            "talos": {
                "interval_sec": self.talos_interval_sec,
                "last_run_at": None,
                "last_error": None,
                "runs_total": 0,
                "lessons_total": 0,
                "last_summary": None,
            },
            "hermes": {
                "interval_sec": self.hermes_interval_sec,
                "last_run_at": None,
                "last_error": None,
                "runs_total": 0,
                "proposals_total": 0,
                "lessons_total": 0,
                "last_summary": None,
            },
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """İki daemon thread başlat (idempotent — zaten çalışıyorsa no-op)."""
        if self.is_alive():
            return
        self._stop_event.clear()
        with self._status_lock:
            self._status["started_at"] = _now_iso()
            self._status["stopped_at"] = None
            self._status["running"] = True

        self._talos_thread = threading.Thread(
            target=self._talos_loop,
            name="EducatorLoop-Talos",
            daemon=True,
        )
        self._hermes_thread = threading.Thread(
            target=self._hermes_loop,
            name="EducatorLoop-Hermes",
            daemon=True,
        )
        self._talos_thread.start()
        self._hermes_thread.start()
        logger.info(
            "EducatorLoop started — talos=%.0fs hermes=%.0fs initial_delay=%.0fs",
            self.talos_interval_sec, self.hermes_interval_sec, self.initial_delay_sec,
        )

    def stop(self, join_timeout_sec: float = 5.0) -> None:
        """Graceful stop — Event flag set + thread'leri join et."""
        self._stop_event.set()
        for t in (self._talos_thread, self._hermes_thread):
            if t is None:
                continue
            try:
                t.join(timeout=join_timeout_sec)
            except Exception:
                pass
        with self._status_lock:
            self._status["stopped_at"] = _now_iso()
            self._status["running"] = False
        logger.info("EducatorLoop stopped")

    def is_alive(self) -> bool:
        """En az bir thread aktif mi?"""
        for t in (self._talos_thread, self._hermes_thread):
            if t is not None and t.is_alive():
                return True
        return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Webui /api/educator/status için anlık snapshot döndür."""
        with self._status_lock:
            # Shallow + nested copy — tüketici mutate etmesin
            snapshot: dict[str, Any] = {
                "started_at": self._status["started_at"],
                "stopped_at": self._status["stopped_at"],
                "running": self._status["running"],
                "is_alive": self.is_alive(),
                "talos_alive": bool(self._talos_thread and self._talos_thread.is_alive()),
                "hermes_alive": bool(self._hermes_thread and self._hermes_thread.is_alive()),
                "talos": dict(self._status["talos"]),
                "hermes": dict(self._status["hermes"]),
            }
        # Recent lesson preview (best-effort)
        try:
            recent = self.journal.recent(n=10)
            preview: list[dict[str, Any]] = []
            for lesson in recent:
                if lesson.author_agent.startswith("Talos") or lesson.author_agent.startswith("Hermes"):
                    preview.append({
                        "author": lesson.author_agent,
                        "content": lesson.content[:200],
                        "tags": lesson.tags[:5],
                        "created_at": lesson.created_at.isoformat() if lesson.created_at else None,
                    })
                if len(preview) >= 5:
                    break
            snapshot["recent_lessons"] = preview
            snapshot["lesson_counts"] = {
                "Talos Curriculum": self.journal.count("Talos Curriculum"),
                "Talos Curriculum (LLM)": self.journal.count("Talos Curriculum (LLM)"),
                "Hermes GitResearcher": self.journal.count("Hermes GitResearcher"),
            }
        except Exception:
            snapshot["recent_lessons"] = []
            snapshot["lesson_counts"] = {}
        return snapshot

    # ------------------------------------------------------------------
    # Worker loops
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float) -> bool:
        """``stop_event`` set edilirse erken çık — True döner stop için."""
        if seconds <= 0:
            return self._stop_event.is_set()
        return self._stop_event.wait(timeout=seconds)

    def _talos_loop(self) -> None:
        """Curriculum thread — daimi loop."""
        # Initial delay (process boot anında network spam etmeyelim)
        if self._interruptible_sleep(self.initial_delay_sec):
            return
        while not self._stop_event.is_set():
            try:
                summary = self.talos.loop_step()
                lessons = int(summary.get("total_lessons", 0))
                with self._status_lock:
                    t = self._status["talos"]
                    t["last_run_at"] = _now_iso()
                    t["last_error"] = None
                    t["runs_total"] = int(t["runs_total"]) + 1
                    t["lessons_total"] = int(t["lessons_total"]) + lessons
                    t["last_summary"] = summary
                logger.info("Talos loop_step: lessons=%d sources=%s",
                            lessons, list(summary.get("sources", {}).keys()))
                if self._interruptible_sleep(self.talos_interval_sec):
                    return
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                logger.warning("Talos loop error: %s — backoff %.0fs", err, _ERROR_BACKOFF_SEC)
                with self._status_lock:
                    self._status["talos"]["last_error"] = err
                if self._interruptible_sleep(_ERROR_BACKOFF_SEC):
                    return

    def _hermes_loop(self) -> None:
        """GitResearcher thread — daimi loop."""
        if self._interruptible_sleep(self.initial_delay_sec):
            return
        while not self._stop_event.is_set():
            try:
                summary = self.hermes.discover_loop_step(max_repos=5, max_findings_per_repo=3)
                proposals = int(summary.get("proposals_created", 0))
                lessons = int(summary.get("lessons_created", 0))
                with self._status_lock:
                    h = self._status["hermes"]
                    h["last_run_at"] = _now_iso()
                    h["last_error"] = summary.get("error")
                    h["runs_total"] = int(h["runs_total"]) + 1
                    h["proposals_total"] = int(h["proposals_total"]) + proposals
                    h["lessons_total"] = int(h["lessons_total"]) + lessons
                    h["last_summary"] = summary
                logger.info(
                    "Hermes discover_loop_step: query=%r repos=%d skipped=%d "
                    "proposals=%d lessons=%d",
                    summary.get("query"), summary.get("repos_seen"),
                    summary.get("repos_skipped"), proposals, lessons,
                )
                if self._interruptible_sleep(self.hermes_interval_sec):
                    return
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                logger.warning("Hermes loop error: %s — backoff %.0fs", err, _ERROR_BACKOFF_SEC)
                with self._status_lock:
                    self._status["hermes"]["last_error"] = err
                if self._interruptible_sleep(_ERROR_BACKOFF_SEC):
                    return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["EducatorLoop"]
