"""Bayesian (TPE) param optimizer — Faz 3.

Eski `_generate_smart_params` notes string'inden `params=` substring parse
edip ~%40 fail oluyordu; düştüğünde rastgele aramaya geri çekiliyordu.
700+ cycle aslında saf rastgele arama yaptı. Bu modül bunu temizler:

    - Per (family, market) Optuna study cache'i.
    - `suggest()` -> TPE sampler ile bir parametre seti üretir.
    - `report()` -> sonucu Optuna'ya geri besler (tell).
    - Persistent storage: `artifacts/optuna_studies.db` (sqlite).

Stratejilere makul bir search space sunan `STRATEGY_PARAM_SPACE` constant'ı
da burada tutulur — orchestrator bu space'i kullanarak `suggest()` çağırır.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import optuna
from optuna.samplers import TPESampler
from optuna.trial import Trial, TrialState

# Optuna'nın gürültülü stdout log'larını sustur.
optuna.logging.set_verbosity(optuna.logging.WARNING)

logger = logging.getLogger("oto_bot.bayesian_optimizer")


# ---------------------------------------------------------------------------
# Strateji ailesi başına makul Bayesian search space
# Tuple formatı:
#     ("int",   low, high)               -> suggest_int(low, high)
#     ("float", low, high)               -> suggest_float(low, high)
#     ("float", low, high, "log")        -> suggest_float(low, high, log=True)
#     ("cat",   [v1, v2, ...])           -> suggest_categorical(choices)
# ---------------------------------------------------------------------------

STRATEGY_PARAM_SPACE: dict[str, dict[str, tuple]] = {
    "day": {
        "fast_ma": ("int", 3, 30),
        "slow_ma": ("int", 20, 100),
        "rsi_period": ("int", 7, 30),
        "rsi_overbought": ("int", 60, 85),
        "rsi_oversold": ("int", 15, 40),
        "atr_period": ("int", 7, 30),
        "atr_multiplier_sl": ("float", 0.8, 3.5),
        "atr_multiplier_tp": ("float", 1.2, 5.0),
    },
    "swing": {
        "ma_short": ("int", 10, 60),
        "ma_long": ("int", 80, 250),
        "macd_fast": ("int", 6, 20),
        "macd_slow": ("int", 18, 40),
        "macd_signal": ("int", 5, 14),
        "atr_period": ("int", 7, 30),
        "atr_multiplier_sl": ("float", 1.0, 4.0),
        "atr_multiplier_tp": ("float", 1.5, 6.0),
    },
    "scalp": {
        "lookback": ("int", 5, 30),
        "z_entry": ("float", 0.5, 2.5),
        "z_exit": ("float", 0.05, 0.6),
        "bb_period": ("int", 10, 35),
        "bb_std": ("float", 1.2, 3.0),
        "atr_period": ("int", 7, 30),
        "atr_multiplier_sl": ("float", 0.5, 2.5),
    },
}


# ---------------------------------------------------------------------------
# BayesianOptimizer
# ---------------------------------------------------------------------------


class BayesianOptimizer:
    """TPE-tabanlı per-(family, market) param arayıcısı.

    Optuna study'leri persistent SQLite storage'da tutulur — bot restart'larında
    geçmiş cycle'ların öğrendiği posterior korunur. Trial objeleri "running"
    durumunda kalır ve `report()` çağrısında `tell()` ile sonuçlandırılır.
    """

    DEFAULT_STORAGE = "sqlite:///artifacts/optuna_studies.db"

    def __init__(
        self,
        storage: str | None = None,
        sampler_seed: int | None = None,
    ) -> None:
        # Storage path: artifacts/optuna_studies.db
        if storage is None:
            artifacts_dir = Path("artifacts")
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            storage = self.DEFAULT_STORAGE
        self.storage = storage
        self._sampler_seed = sampler_seed
        # Per (family, market) study cache
        self._studies: dict[tuple[str, str], optuna.Study] = {}
        # Bekleyen trial'lar: (family, market, params_tuple) -> trial_number
        # `report()` doğru trial'ı bulup `tell()` etmek için kullanır.
        self._pending_trials: dict[tuple[str, str, tuple], int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Study yönetimi
    # ------------------------------------------------------------------

    def _study_name(self, family: str, market: str, study_key: str | None = None) -> str:
        if study_key:
            return f"oto_bot__{study_key}"
        return f"oto_bot__{family}__{market}"

    def _get_study(
        self,
        family: str,
        market: str,
        study_key: str | None = None,
    ) -> optuna.Study:
        # Faz 6 — slot başına ayrı study (`study_key`) destekli.
        # study_key verilmezse eski (family, market) cache'i kullanılır.
        key = (study_key, family, market) if study_key else (family, market)
        with self._lock:
            study = self._studies.get(key)
            if study is not None:
                return study
            sampler = TPESampler(seed=self._sampler_seed) if self._sampler_seed is not None else TPESampler()
            study = optuna.create_study(
                study_name=self._study_name(family, market, study_key),
                storage=self.storage,
                sampler=sampler,
                direction="maximize",  # composite_score / sharpe — büyük iyi
                load_if_exists=True,
            )
            self._studies[key] = study
            return study

    # ------------------------------------------------------------------
    # Search space -> Optuna trial.suggest_*
    # ------------------------------------------------------------------

    @staticmethod
    def _suggest_one(trial: Trial, name: str, spec: tuple) -> Any:
        """`spec`'i Optuna trial.suggest_* çağrısına çevir."""
        if not spec or len(spec) < 2:
            raise ValueError(f"Invalid space spec for {name}: {spec!r}")
        kind = spec[0]
        if kind == "int":
            low, high = int(spec[1]), int(spec[2])
            return trial.suggest_int(name, low, high)
        if kind == "float":
            low, high = float(spec[1]), float(spec[2])
            log = bool(len(spec) >= 4 and spec[3] == "log")
            return trial.suggest_float(name, low, high, log=log)
        if kind == "cat":
            choices = list(spec[1])
            return trial.suggest_categorical(name, choices)
        raise ValueError(f"Unknown space kind '{kind}' for {name}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suggest(
        self,
        family: str,
        market: str,
        base_space: dict[str, tuple],
        study_key: str | None = None,
        context_lessons: list | None = None,
    ) -> dict[str, Any]:
        """TPE'den yeni bir trial iste; üretilen parametreleri döndür.

        `base_space` örn: `{"fast_ma": ("int", 3, 30), ...}`.
        Üretilen trial "running" durumunda kalır — `report()` çağrısı
        onu `tell()` ile sonuçlandırır. `report()` yapılmazsa trial sızıntı
        oluşturmaz; Optuna ileri ki sürümlerde temizler.

        ``study_key`` verilmişse, study `oto_bot__<study_key>` adıyla
        yaratılır (Faz 6 slot başına posterior). Yoksa
        ``oto_bot__<family>__<market>`` fallback'i kullanılır.

        ``context_lessons`` (Faz 7) — orchestrator'ın retriever'dan
        çektiği ilgili dersler. **Prior'a etki ETMEZ** (TPE saf
        data-driven kalır), sadece trial.user_attrs'a iz bırakır:
        sonradan hangi cycle'da hangi ders'in tetiklendiğini debug
        edebiliriz.
        """
        study = self._get_study(family, market, study_key=study_key)
        trial = study.ask()
        params: dict[str, Any] = {}
        for name, spec in base_space.items():
            try:
                params[name] = self._suggest_one(trial, name, spec)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"suggest({family}/{market}/{name}) failed: {exc}")
        # Faz 7 — lesson hint'i trial user_attrs'a yaz (prior'a etki etmez).
        if context_lessons:
            try:
                hint = []
                for lesson in context_lessons[:5]:
                    hint.append({
                        "id": getattr(lesson, "lesson_id", "")[:36],
                        "author": getattr(lesson, "author_agent", "")[:32],
                        "tags": list(getattr(lesson, "tags", []))[:6],
                    })
                trial.set_user_attr("lesson_hint", hint)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"suggest({family}/{market}) lesson_hint failed: {exc}")
        # Bekleyen kayda al — report() bu trial'ı bulup tell etsin.
        ptk = self._params_key(family, market, params, study_key=study_key)
        with self._lock:
            self._pending_trials[ptk] = trial.number
        return params

    def report(
        self,
        family: str,
        market: str,
        params: dict[str, Any],
        score: float,
        study_key: str | None = None,
    ) -> None:
        """Cycle bitince composite_score'u Optuna'ya geri besle.

        `params` aynı `suggest()`'in döndürdüğü dict olmalı (key-set match).
        Bulunmazsa, hiç ask edilmemiş bir trial olarak `add_trial()` ile
        koy — yine de Optuna posterior'una bilgi verir.
        """
        study = self._get_study(family, market, study_key=study_key)
        ptk = self._params_key(family, market, params, study_key=study_key)
        with self._lock:
            trial_no = self._pending_trials.pop(ptk, None)

        try:
            if trial_no is not None:
                # Beklenen yol: ask() ile alınan trial'ı tell ile kapat.
                study.tell(trial_no, float(score), state=TrialState.COMPLETE)
                return
            # Fallback: dış dünyadan gelen bir paramı doğrudan ekle.
            distributions = self._infer_distributions(family, params)
            study.add_trial(
                optuna.trial.create_trial(
                    params=params,
                    distributions=distributions,
                    value=float(score),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"report({family}/{market}) failed: {exc}")

    def best_params(self, family: str, market: str) -> dict[str, Any] | None:
        """O ana kadar görülen en iyi trial'ın paramları (yoksa None)."""
        try:
            study = self._get_study(family, market)
            best = study.best_trial
            return dict(best.params)
        except (ValueError, optuna.exceptions.OptunaError):
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"best_params({family}/{market}) failed: {exc}")
            return None

    def study_summary(self, family: str, market: str) -> dict[str, Any]:
        """Dashboard / debug için kompakt özet."""
        study = self._get_study(family, market)
        n_trials = len(study.trials)
        best_value = None
        best_params: dict[str, Any] | None = None
        try:
            best_value = float(study.best_value)
            best_params = dict(study.best_params)
        except (ValueError, optuna.exceptions.OptunaError):
            pass
        return {
            "study_name": self._study_name(family, market),
            "n_trials": n_trials,
            "best_value": best_value,
            "best_params": best_params,
        }

    # ------------------------------------------------------------------
    # İç yardımcılar
    # ------------------------------------------------------------------

    @staticmethod
    def _params_key(
        family: str,
        market: str,
        params: dict[str, Any],
        study_key: str | None = None,
    ) -> tuple:
        """`(study_key | family, market, sorted-params-tuple)` -> dict key."""
        items = tuple(sorted((k, _hashable(v)) for k, v in params.items()))
        if study_key:
            return (study_key, family, market, items)
        return (family, market, items)

    @staticmethod
    def _infer_distributions(
        family: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Fallback `add_trial()` için param dağılımlarını yeniden inşa et."""
        space = STRATEGY_PARAM_SPACE.get(family, {})
        dists: dict[str, Any] = {}
        for name, value in params.items():
            spec = space.get(name)
            if spec is None:
                # Bilinmeyen param için: tipinden dağılım üret.
                if isinstance(value, bool):
                    dists[name] = optuna.distributions.CategoricalDistribution([True, False])
                elif isinstance(value, int):
                    dists[name] = optuna.distributions.IntDistribution(value, value)
                elif isinstance(value, float):
                    dists[name] = optuna.distributions.FloatDistribution(value, value)
                else:
                    dists[name] = optuna.distributions.CategoricalDistribution([value])
                continue
            kind = spec[0]
            if kind == "int":
                dists[name] = optuna.distributions.IntDistribution(int(spec[1]), int(spec[2]))
            elif kind == "float":
                log = bool(len(spec) >= 4 and spec[3] == "log")
                dists[name] = optuna.distributions.FloatDistribution(
                    float(spec[1]), float(spec[2]), log=log
                )
            elif kind == "cat":
                dists[name] = optuna.distributions.CategoricalDistribution(list(spec[1]))
            else:
                dists[name] = optuna.distributions.CategoricalDistribution([value])
        return dists


def _hashable(v: Any) -> Any:
    """Liste/dict gibi unhashable değerleri tuple'a çevir (key kullanımı için)."""
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(val)) for k, val in v.items()))
    return v
