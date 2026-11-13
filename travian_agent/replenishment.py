"""Smart Troop Replenishment: rimpiazzo intelligente delle truppe perse.

Il modulo si attiva a valle del parsing dei report di combattimento e decide,
per ogni villaggio, se e quante unita' riaddestrare in base a:

- **Target per unita'** configurati dall'utente per villaggio
  (``VillageConfig.troop_targets``: ``{t5: 500}``), con la formula

      da_addestrare = target - (truppe_attuali + truppe_in_coda)

- **Filtro anti-suicidio (Scenario 1, perdite in attacco/farming)**:
  un target che infligge perdite con bottino nullo e difese rilevate viene
  marcato ``blocked_targets``; dopo ``fail_streak`` fallimenti consecutivi le
  unita' offensive coinvolte vengono congelate (``frozen_units``) per non
  produrre truppe destinate a morire allo stesso modo.

- **Sostenibilita' difesa (Scenario 2, perdite in rinforzo)**: se un villaggio
  risulta sotto assedio (``incoming_attacks >= siege_waves``) viene marcato
  ``frozen_villages``: niente rimpiazzo ne' consumo di risorse finche'
  l'assedio non rientra. Se la difesa e' stabile il rimpiazzo e' autorizzato.

- **Bilanciamento risorse**: l'addestramento cede il passo alle code di
  sviluppo economico (``skip_while_building``) e non intacca il grano oltre
  la riserva configurata (``crop_reserve_pct`` del granaio).

Stato persistito in ``state/replenish_<account_id>.json`` e letto dalla
dashboard per mostrare blocchi e congelamenti attivi.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_DIR

if TYPE_CHECKING:
    from .config import Settings, VillageConfig
    from .models import GameState

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Unita' usate tipicamente per raid/farming (in ordine di preferenza, come
# l'euristica dello Scout Brain): sono quelle congelate in caso di
# fallimenti ripetuti se il report non indica la composizione delle perdite.
RAID_UNIT_PREFERENCE = ("t11", "t24", "t5", "t1", "t22", "t13", "t15", "t6")

# Chiavi risorsa usate nei costi di addestramento
_RES_KEYS = ("wood", "clay", "iron", "crop")

# Coloni per tribu' (t10 Romani, t20 Teutoni, t30 Galli) e numero richiesto
# per la fondazione: la riserva anti-saturazione e' 3 x costo colono.
SETTLER_UNITS = ("t10", "t20", "t30")
SETTLERS_REQUIRED = 3


def settler_reserve() -> dict[str, int]:
    """Riserva risorse anti-saturazione: ``3 x Costo Colono``.

    La tribu' del villaggio non e' sempre nota al planner: per sicurezza si
    prende, risorsa per risorsa, il costo massimo fra i tre tipi di colono.
    """
    try:
        from .actions import STATIC_TROOP_COSTS  # import ritardato: no cicli
    except Exception:  # noqa: BLE001
        return {}
    reserve: dict[str, int] = {}
    for unit in SETTLER_UNITS:
        cost = STATIC_TROOP_COSTS.get(unit) or {}
        for res in _RES_KEYS:
            reserve[res] = max(
                reserve.get(res, 0), SETTLERS_REQUIRED * int(cost.get(res, 0) or 0)
            )
    return reserve


def _troop_costs() -> dict[str, dict[str, int]]:
    """Costi unitari delle truppe (tabella statica in actions.py)."""
    try:
        from .actions import STATIC_TROOP_COSTS  # import ritardato: no cicli
        return STATIC_TROOP_COSTS
    except Exception:  # noqa: BLE001
        return {}


def clamp_plan_to_budget(
    plan: dict[str, int],
    budget: dict[str, int],
) -> dict[str, int]:
    """Riduce le quantita' del piano al massimo sostenibile dal budget.

    Itera le unita' in ordine e, per ciascuna, limita la quantita' al
    minimo intero consentito dalle risorse disponibili (costo unitario x
    quantita' <= budget residuo). Unita' senza costo noto restano invariate.
    """
    if not budget:
        return {u: q for u, q in plan.items() if q > 0}
    costs = _troop_costs()
    remaining = {r: max(0, int(v or 0)) for r, v in budget.items()}
    out: dict[str, int] = {}
    for unit, qty in plan.items():
        if qty <= 0:
            continue
        cost = costs.get(normalize_unit_key(unit))
        if not cost:
            out[unit] = qty
            continue
        affordable = qty
        for res in _RES_KEYS:
            unit_cost = int(cost.get(res, 0) or 0)
            if unit_cost > 0:
                affordable = min(affordable, remaining.get(res, 0) // unit_cost)
        if affordable > 0:
            out[unit] = affordable
            for res in _RES_KEYS:
                remaining[res] = remaining.get(res, 0) - (
                    affordable * int(cost.get(res, 0) or 0)
                )
    return out


def normalize_unit_key(key: str) -> str:
    """Normalizza 'u5', 'T5', 'unit5' nel formato canonico 't5'."""
    k = str(key or "").lower().strip()
    if k.startswith("unit"):
        k = "u" + k[4:]
    if k.startswith("u") and k[1:].isdigit():
        return "t" + k[1:]
    return k


def unit_label_to_key(label: str) -> str:
    """Risolve un nome unita' ('Imperiani', 't5', 'u21') nella chiave 'tN'.

    Ritorna "" se il nome non e' riconoscibile. Le chiavi tN/uN passano
    direttamente; i nomi sono confrontati su STATIC_TROOP_LABELS con match
    esatto case-insensitive o singolare/plurale semplice.
    """
    from .models import STATIC_TROOP_LABELS  # import ritardato: no cicli

    k = normalize_unit_key(label)
    if k.startswith("t") and k[1:].isdigit() and k in STATIC_TROOP_LABELS:
        return k
    low = str(label or "").strip().lower()
    if not low:
        return ""
    candidates: list[str] = []
    for code, name in STATIC_TROOP_LABELS.items():
        n = name.lower()
        # Match esatto o plurale italiano: Imperiano->Imperiani,
        # Falange->Falangi, Mazziere->Mazzieri (vocale finale -> 'i')
        if low == n or low == n + "i" or (len(n) > 1 and low == n[:-1] + "i"):
            candidates.append(code)
    # In caso di omonimia tra tribu' (Ariete, Colono) vince la chiave piu' bassa;
    # il villaggio potra' addestrare solo le unita' della propria tribu'.
    return min(candidates, key=lambda c: int(c[1:])) if candidates else ""


@dataclass
class TroopReplenishmentManager:
    """Gestore per-account del rimpiazzo truppe, con stato persistente."""

    account_id: str
    state_path: Path | None = None
    # "x_y" -> {"reason": str, "ts": float, "fail_streak": int}
    blocked_targets: dict[str, dict] = field(default_factory=dict)
    # "tN" -> motivo del congelamento (unita' offensive)
    frozen_units: dict[str, str] = field(default_factory=dict)
    # nome_villaggio -> motivo del congelamento (villaggio sotto assedio)
    frozen_villages: dict[str, str] = field(default_factory=dict)
    # "x_y" -> conteggio fallimenti consecutivi (per la soglia dinamica)
    fail_streaks: dict[str, int] = field(default_factory=dict)
    last_events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state_path is None:
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in self.account_id
            )
            self.state_path = STATE_DIR / f"replenish_{safe}.json"
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.blocked_targets = {
                    str(k): dict(v)
                    for k, v in (data.get("blocked_targets") or {}).items()
                    if isinstance(v, dict)
                }
                self.frozen_units = {
                    normalize_unit_key(k): str(v)
                    for k, v in (data.get("frozen_units") or {}).items()
                }
                self.frozen_villages = {
                    str(k): str(v)
                    for k, v in (data.get("frozen_villages") or {}).items()
                }
                self.fail_streaks = {
                    str(k): int(v)
                    for k, v in (data.get("fail_streaks") or {}).items()
                }
                self.last_events = list(data.get("last_events") or [])[-30:]
        except Exception:
            logger.debug("Caricamento stato replenishment fallito", exc_info=True)

    def _save(self) -> None:
        try:
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "blocked_targets": self.blocked_targets,
                        "frozen_units": self.frozen_units,
                        "frozen_villages": self.frozen_villages,
                        "fail_streaks": self.fail_streaks,
                        "last_events": self.last_events[-30:],
                        "updated_at": time.time(),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Salvataggio stato replenishment fallito", exc_info=True)

    def _log_event(self, kind: str, detail: str) -> None:
        self.last_events.append(
            {"ts": time.time(), "kind": kind, "detail": detail}
        )
        self.last_events = self.last_events[-30:]

    # ------------------------------------------------------------------
    # Scenario 1: analisi post-battaglia (attacco / farming)
    # ------------------------------------------------------------------
    def process_combat_reports(
        self,
        reports: list[dict],
        *,
        auto_block: bool = True,
        fail_streak_threshold: int = 2,
        raid_units: list[str] | None = None,
    ) -> None:
        """Analizza i report e decide blocchi target / congelamento unita'.

        Un report con ``losses > 0`` e ``loot_ratio`` basso indica un attacco
        suicida: il target viene bloccato e, raggiunta la soglia di streak,
        le unita' offensive coinvolte vengono congelate. Le perdite
        fisiologiche (bottino comunque portato a casa) non bloccano nulla:
        il rimpiazzo resta autorizzato e sara' coperto dal deficit.
        """
        changed = False
        for r in reports:
            x, y = r.get("x"), r.get("y")
            if x is None or y is None:
                continue
            key = f"{int(x)}_{int(y)}"
            losses = int(r.get("losses", 0) or 0)
            defenders = int(r.get("defenders", 0) or 0)
            loot = float(r.get("loot_ratio", 1.0) or 0.0)

            if losses <= 0:
                # Esito positivo: azzera la streak e sblocca eventuale target
                if key in self.fail_streaks:
                    self.fail_streaks.pop(key, None)
                    changed = True
                if key in self.blocked_targets:
                    self.blocked_targets.pop(key, None)
                    self._log_event(
                        "unblock", f"Target ({x}|{y}) sbloccato dopo esito positivo"
                    )
                    changed = True
                continue

            self.fail_streaks[key] = self.fail_streaks.get(key, 0) + 1
            streak = self.fail_streaks[key]
            suicidal = defenders > 0 and loot <= 0.2
            heavy = losses >= streak or defenders >= 3

            if auto_block and (suicidal or (heavy and streak >= fail_streak_threshold)):
                self.blocked_targets[key] = {
                    "reason": (
                        f"Perdite {losses} vs difensori {defenders}, "
                        f"bottino {int(loot * 100)}% (streak {streak})"
                    ),
                    "ts": time.time(),
                    "fail_streak": streak,
                }
                self._log_event(
                    "block",
                    f"Target ({x}|{y}) bloccato: perdite {losses}, difensori {defenders}",
                )
                # Congelamento unita' offensive: quelle perse se note,
                # altrimenti le unita' da raid configurate/preferite.
                # unit_label_to_key risolve anche le label ('Imperiani'->'t3'),
                # normalize copre i codici ('u5','t5') non riconosciuti.
                def _uk(u: str) -> str:
                    return unit_label_to_key(u) or normalize_unit_key(u)

                lost = {
                    _uk(u) for u in (r.get("units_lost") or [])
                }
                units_to_freeze = lost or {
                    _uk(u) for u in (raid_units or RAID_UNIT_PREFERENCE)
                }
                for u in units_to_freeze:
                    if u not in self.frozen_units:
                        self.frozen_units[u] = (
                            f"Congelata dopo {streak} fallimenti su ({x}|{y})"
                        )
                        self._log_event(
                            "freeze",
                            f"Unita' {u} congelata (target suicida {x}|{y})",
                        )
                changed = True
            else:
                self._log_event(
                    "loss",
                    f"Perdita fisiologica su ({x}|{y}): {losses} caduti, "
                    f"bottino {int(loot * 100)}% — rimpiazzo autorizzato",
                )
                changed = True

        if changed:
            self._save()

    # ------------------------------------------------------------------
    # Scenario 2: sostenibilita' difesa / rinforzi
    # ------------------------------------------------------------------
    def update_siege_state(
        self,
        village_name: str,
        incoming_attacks: int,
        *,
        siege_waves: int = 2,
    ) -> None:
        """Marca/sblocca un villaggio 'sotto assedio'.

        Villaggio che cede (ondate >= soglia): niente rimpiazzo e niente
        rinforzi — si preserva l'esercito. Difesa stabile: sblocco e
        rimpiazzo autorizzato nelle unita' difensive.
        """
        changed = False
        name = village_name or ""
        if incoming_attacks >= siege_waves:
            if name not in self.frozen_villages:
                self.frozen_villages[name] = (
                    f"Sotto assedio ({incoming_attacks} ondate in arrivo): "
                    "rimpiazzo e rinforzi sospesi per preservare l'esercito"
                )
                self._log_event(
                    "siege",
                    f"Villaggio '{name}' congelato: {incoming_attacks} ondate in arrivo",
                )
                changed = True
        elif name in self.frozen_villages:
            self.frozen_villages.pop(name, None)
            self._log_event(
                "unsiege", f"Villaggio '{name}' sbloccato: assedio rientrato"
            )
            changed = True
        if changed:
            self._save()

    def record_defense_report(
        self,
        village_name: str,
        losses: int,
        *,
        village_collapsing: bool,
        defensive_units: list[str] | None = None,
    ) -> None:
        """Registra perdite subite dalle truppe in rinforzo.

        Se il villaggio protetto sta cedendo congela il rimpiazzo difensivo;
        se la linea tiene, autorizza il rimpiazzo delle unita' perse.
        """
        if losses <= 0:
            return
        if village_collapsing:
            self.frozen_villages[village_name] = (
                f"Difesa compromessa ({losses} perdite in rinforzo): "
                "rimpiazzo difensivo sospeso"
            )
            self._log_event(
                "siege",
                f"'{village_name}' cede: blocco rimpiazzo difensivo ({losses} perdite)",
            )
        else:
            self._log_event(
                "reinforce",
                f"'{village_name}' tiene: autorizzato rimpiazzo di {losses} "
                f"unita' difensive {defensive_units or ''}",
            )
        self._save()

    # ------------------------------------------------------------------
    # Calcolo deficit e piano di addestramento
    # ------------------------------------------------------------------
    def is_unit_frozen(self, unit_key: str) -> bool:
        return normalize_unit_key(unit_key) in self.frozen_units

    def is_village_frozen(self, village_name: str) -> bool:
        return (village_name or "") in self.frozen_villages

    def is_target_blocked(self, x: int, y: int) -> bool:
        return f"{int(x)}_{int(y)}" in self.blocked_targets

    def compute_deficit(
        self,
        troop_targets: dict[str, int],
        troops_home: dict[str, int],
        troops_queued: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Applica la formula: da_addestrare = target - (attuali + in_coda).

        Ritorna {tN: quantita'} solo per le unita' con deficit positivo e
        non congelate. Le chiavi dei target possono essere 'tN' o nomi
        unita'; quelle di troops_home/'uN' vengono normalizzate a 'tN'.
        """
        queued = troops_queued or {}
        home_norm: dict[str, int] = {}
        for k, v in (troops_home or {}).items():
            nk = normalize_unit_key(k)
            home_norm[nk] = home_norm.get(nk, 0) + int(v or 0)
        queue_norm: dict[str, int] = {}
        for k, v in queued.items():
            nk = normalize_unit_key(k)
            queue_norm[nk] = queue_norm.get(nk, 0) + int(v or 0)

        deficit: dict[str, int] = {}
        for target_key, target_qty in (troop_targets or {}).items():
            qty_target = int(target_qty or 0)
            if qty_target <= 0:
                continue
            key = normalize_unit_key(target_key)
            if not (key.startswith("t") and key[1:].isdigit()):
                key = unit_label_to_key(target_key)
            if not key:
                continue
            if self.is_unit_frozen(key):
                continue
            need = qty_target - (home_norm.get(key, 0) + queue_norm.get(key, 0))
            if need > 0:
                deficit[key] = need
        return deficit

    def plan_training(
        self,
        deficit: dict[str, int],
        state: "GameState",
        settings: "Settings",
        *,
        village_name: str = "",
        reserve_settlers: bool = False,
    ) -> dict[str, int]:
        """Filtra il deficit in un piano sostenibile {tN: qty}.

        - villaggio congelato (sotto assedio): niente piano;
        - ``skip_while_building``: cede il passo alle code di sviluppo;
        - guardia grano: se il bilancio netto ufficiale (crop_balance da
          production.php) scende sotto la soglia configurata, l'addestramento
          che consuma grano viene sospeso (Grain Safety Net, spec 1.1);
        - ``reserve_settlers``: il villaggio e' il fondatore (o la rete
          feeder e' attiva) — la riserva ``3 x costo colono`` non puo'
          essere spesa in addestramento (spec 2.1);
        - il piano e' comunque limitato al surplus disponibile: mai spendere
          oltre le scorte meno la riserva coloni.
        """
        if not deficit:
            return {}
        if village_name and self.is_village_frozen(village_name):
            logger.info(
                "[%s] Replenishment: villaggio '%s' sotto assedio, rimpiazzo sospeso",
                self.account_id,
                village_name,
            )
            return {}

        cfg = settings.replenishment
        res = state.resources

        # Priorita' alle code di sviluppo economico
        if cfg.skip_while_building and (
            state.resource_queue() or state.interior_queue()
        ):
            logger.debug(
                "[%s] Replenishment: coda costruzioni attiva in '%s', "
                "addestramento rimandato",
                self.account_id,
                village_name,
            )
            return {}

        # Priorita' anche agli upgrade critici in attesa (spec 3): se il
        # planner ha un upgrade Storage / Grain Safety / Bootstrap che non
        # e' ancora partito (tipicamente per mancanza di risorse), la
        # caserma non deve drenare il budget che serve a sbloccarlo.
        if cfg.skip_while_building:
            try:
                from .builder import select_upgrade  # no cicli a import

                pending = select_upgrade(state, settings)
            except Exception:  # noqa: BLE001
                pending = None
            if pending is not None and pending.kind in (
                "storage", "grain_safety", "bootstrap",
            ):
                logger.debug(
                    "[%s] Replenishment: upgrade prioritario pendente in "
                    "'%s' (%s) — addestramento sospeso",
                    self.account_id,
                    village_name,
                    pending.kind,
                )
                return {}

        # Grain Safety Net: usa il bilancio netto ufficiale letto da
        # production.php (crop_balance). None = dato non letto: si ripiega
        # sulla produzione oraria lorda come stima prudenziale.
        net_crop = res.crop_balance
        if net_crop is None:
            prod = res.production_per_hour or {}
            try:
                from .models import ResourceType

                net_crop = int(prod.get(ResourceType.CROP, 0) or 0)
            except Exception:  # noqa: BLE001
                net_crop = int(prod.get("crop", 0) or 0)
        vcfg = settings.village_config_for(
            village_name or state.village_name, state.village_id
        )
        threshold = (
            float(vcfg.min_net_crop_threshold)
            if vcfg is not None and vcfg.min_net_crop_threshold > 0
            else float(getattr(settings, "min_net_crop_threshold", 10.0))
        )
        if net_crop <= threshold:
            logger.debug(
                "[%s] Replenishment: margine grano netto %d/h <= soglia %g/h "
                "in '%s': addestramento sospeso (Grain Safety Net)",
                self.account_id,
                net_crop,
                threshold,
                village_name,
            )
            return {}

        # Guardia scorte: mai intaccare la riserva di grano configurata
        granary = res.granary_capacity or 0
        crop_reserve = int(granary * max(0.0, cfg.crop_reserve_pct))
        if res.crop < crop_reserve:
            logger.debug(
                "[%s] Replenishment: grano insufficiente in '%s' "
                "(%d < riserva %d, prod netta %d/h)",
                self.account_id,
                village_name,
                res.crop,
                crop_reserve,
                net_crop,
            )
            return {}

        plan = {u: q for u, q in deficit.items() if q > 0}
        if not plan:
            return {}

        # Anti-saturazione: limita la spesa al surplus. Se il villaggio e'
        # il fondatore (rete feeder attiva), la riserva 3x colono non si tocca.
        budget = {
            "wood": res.wood,
            "clay": res.clay,
            "iron": res.iron,
            "crop": max(0, res.crop - crop_reserve),
        }
        if reserve_settlers:
            reserve = settler_reserve()
            for k, v in reserve.items():
                budget[k] = max(0, budget.get(k, 0) - v)
            logger.debug(
                "[%s] Replenishment: riserva coloni attiva in '%s' (%s) — "
                "budget addestramento %s",
                self.account_id,
                village_name,
                reserve,
                budget,
            )
        clamped = clamp_plan_to_budget(plan, budget)
        if len(clamped) < len(plan) or any(
            clamped.get(u, 0) < q for u, q in plan.items()
        ):
            logger.info(
                "[%s] Replenishment: piano per '%s' ridotto al surplus %s "
                "(richiesto %s)",
                self.account_id,
                village_name,
                clamped,
                plan,
            )
        elif clamped:
            logger.info(
                "[%s] Replenishment: piano rimpiazzo per '%s': %s",
                self.account_id,
                village_name,
                clamped,
            )
        return clamped
