"""Modulo Farming: invio periodico di raid dalle farm list del Punto di Raduno.

Design:
- uno scheduler per account mantiene il timestamp del prossimo raid per
  ogni villaggio in state/farming_<account>.json (persiste tra i riavvii);
- l'intervallo tra un ciclo e l'altro e' SEMPRE randomizzato con jitter
  uniforme tra interval_min_minutes e interval_max_minutes (requisito
  anti-ban: mai due cicli a distanza identica);
- prima dell'invio si verifica che il villaggio abbia truppe a casa
  (sensore read_troops_home); se il sensore non espone dati si tenta
  comunque l'invio (un raid vuoto fallisce lato gioco senza danni);
- il ritorno delle truppe cariche viene gestito dal normale ciclo di
  smaltimento surplus del brain (storage overflow -> mercato/truppe).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import combat_calc, db
from .config import BASE_DIR, Settings
from .models import Action, ActionType, GID_RALLY_POINT

if TYPE_CHECKING:
    from .actions import ActionExecutor
    from .browser import TravianBrowser
    from .farm_store import FarmTargetStore
    from .sensors import GameObserver
    from .models import Field, Village

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Fallback: se il sensore truppe non legge dati, riprova comunque dopo
# un breve intervallo (l'invio fallito non ha effetti collaterali).
RETRY_MIN_SECONDS = 240.0
RETRY_MAX_SECONDS = 420.0

# Backoff lungo quando non esiste alcuna farm list (Gold Club attivo ma
# 0/100 liste): riprovare ogni pochi minuti non serve, le liste vanno
# create in-game. Controllo orario con jitter per limitare page load e
# warning ripetuti.
NO_LIST_RETRY_MIN_SECONDS = 3600.0
NO_LIST_RETRY_MAX_SECONDS = 5400.0

# Custom Farm Target: cooldown della scansione mappa di scoperta per
# villaggio (i tile trovati entrano nello store come 'pending').
SCAN_MIN_SECONDS = 5400.0   # 1.5h
SCAN_MAX_SECONDS = 9000.0   # 2.5h
# Minimo di unita' per raid quando farm_unit_qty = auto
AUTO_QTY_FLOOR = 3

# Budget anti page-load storm per ciclo (Smart Pre-Raid Intelligence):
# letture scheda tile oasi, apertura report spia e missioni esploratori.
MAX_TILE_READS_PER_CYCLE = 5
MAX_REPORT_READS_PER_CYCLE = 3
MAX_SCOUTS_PER_CYCLE = 3

# Alias delle truppe "leggere da saccheggio" per verificare la presenza
# quando non e' configurata un'unita' specifica: le chiavi del sensore
# sono 'uN' (icona unit u1..u30 del CSS Travian).
_LIGHT_RAIDERS = {
    "u1",   # legionario (romani)
    "u2",   # pretoriano
    "u3",   # imperiano (era assente: villaggi con soli imperiani ->
            # 'nessuna unita' da saccheggio risolta')
    "u4", "u5", "u6",
    "u11",  # clubswinger (teutoni)
    "u12", "u13", "u15", "u16",
    "u22",  # spadaccino (galli)
    "u24", "u25", "u26",
}

# Nome -> set di alias accettati per il check 'unità designata presente'
_UNIT_KEY_ALIASES: dict[str, set[str]] = {
    # --- ROMANI ---
    "legione": {"u1"}, "legionario": {"u1"}, "legionari": {"u1"},
    "pretoriano": {"u2"}, "pretoriani": {"u2"},
    "imperiano": {"u3"}, "imperiani": {"u3"},
    "equites legatus": {"u4"}, "esploratore romano": {"u4"}, "emissario": {"u4"},
    "equites imperatoris": {"u5"}, "cavaliere imperatoris": {"u5"},
    "equites cesaris": {"u6"}, "cavaliere cesaris": {"u6"},
    "ariete": {"u7"}, "ariete romano": {"u7"},
    "catapulta da fuoco": {"u8"}, "catapulta": {"u8"},
    "senatore": {"u9"}, "senatori": {"u9"},
    "colono": {"u10"}, "coloni": {"u10"},

    # --- TEUTONI ---
    "mazziere": {"u11"}, "mazzieri": {"u11"}, "combattente": {"u11"},
    "lanciere": {"u12"}, "lancieri": {"u12"},
    "ascia": {"u13"}, "guerriero con ascia": {"u13"},
    "spia": {"u14"}, "esploratore teutonico": {"u14"},
    "paladino": {"u15"}, "paladini": {"u15"},
    "cavaliere teutonico": {"u16"}, "teutonico": {"u16"},
    "ariete teutonico": {"u17"},
    "catapulta teutonica": {"u18"},
    "capotribù": {"u19"}, "comandante teutonico": {"u19"},
    "colono teutonico": {"u20"},

    # --- GALLI ---
    "falange": {"u21"}, "falangi": {"u21"},
    "spadaccino": {"u22"}, "spadaccini": {"u22"},
    "tracciatore": {"u23"}, "esploratore gallo": {"u23"},
    "fulmine": {"u24"}, "fulmine di teutates": {"u24"}, "teutates": {"u24"},
    "druido": {"u25"}, "cavaliere druido": {"u25"},
    "eduano": {"u26"}, "haeduan": {"u26"},
    "ariete gallo": {"u27"}, "trabucco": {"u27"},
    "catapulta gallo": {"u28"},
    "capotribù gallo": {"u29"}, "comandante": {"u29"},
    "colono gallo": {"u30"},

    # --- MACRO-GRUPPI GENERALI ---
    "cavaliere": {"u4", "u5", "u6", "u15", "u16", "u24", "u25", "u26"},
    "esploratore": {"u4", "u14", "u23"},
    "truppe leggere": set(_LIGHT_RAIDERS),
    "legger": set(_LIGHT_RAIDERS),
}


def find_rally_point(interior: list["Field"]) -> "Field | None":
    """Restituisce il campo del Punto di Raduno (gid 16) negli interni."""
    for f in interior or []:
        if f.gid == GID_RALLY_POINT:
            return f
    return None


@dataclass
class FarmingManager:
    """Scheduler dei raid per un account: timing jittered + stato su disco."""

    account_id: str
    state_path: Path | None = None
    next_due: dict[str, float] = field(default_factory=dict)
    # Prossima scansione mappa di scoperta per villaggio (custom farm)
    next_scan: dict[str, float] = field(default_factory=dict)
    # Buffer Decision Audit Log: eventi decisionali del ciclo in corso,
    # svuotato dall'orchestrator via drain_audit() (non persistito).
    _audit: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._kv_key: str | None = None
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self._kv_key = f"farming_{safe}.json"
            self.state_path = STATE_DIR / self._kv_key
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data: dict | None = None
            if self._kv_key is not None:
                text = db.kv_read(self._kv_key)
                data = json.loads(text) if text else None
            elif self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data:
                self.next_due = {
                    str(k): float(v) for k, v in (data.get("next_due") or {}).items()
                }
                self.next_scan = {
                    str(k): float(v) for k, v in (data.get("next_scan") or {}).items()
                }
        except Exception:
            self.next_due = {}
            self.next_scan = {}

    def _save(self) -> None:
        try:
            payload = json.dumps(
                {
                    "next_due": self.next_due,
                    "next_scan": self.next_scan,
                },
                indent=2,
            )
            if self._kv_key is not None:
                db.kv_write(self._kv_key, payload)
                return
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(payload, encoding="utf-8")
        except Exception:
            logger.debug("Salvataggio stato farming fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Scheduling con jitter (anti-ban)
    # ------------------------------------------------------------------
    def next_interval_seconds(self, settings: Settings) -> float:
        """Intervallo casuale uniforme tra min e max configurati, in secondi."""
        lo = max(60.0, float(settings.farming.interval_min_minutes) * 60.0)
        hi = max(lo, float(settings.farming.interval_max_minutes) * 60.0)
        return random.uniform(lo, hi)

    def is_due(self, village_key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.next_due.get(village_key, 0.0)

    def schedule_next(self, village_key: str, settings: Settings, now: float | None = None) -> float:
        """Pianifica il prossimo raid e ritorna il timestamp pianificato."""
        now = now if now is not None else time.time()
        due = now + self.next_interval_seconds(settings)
        self.next_due[village_key] = due
        self._save()
        return due

    def _schedule_retry(self, village_key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.next_due[village_key] = now + random.uniform(
            RETRY_MIN_SECONDS, RETRY_MAX_SECONDS
        )
        self._save()

    # ------------------------------------------------------------------
    # Verifica truppe
    # ------------------------------------------------------------------
    def _has_raiders(self, troops: dict[str, int], unit_pref: str) -> bool:
        """True se ci sono truppe da saccheggio a casa.

        troops: {u1: n, u5: n, ...} dal sensore. unit_pref: nome config
        ('' = qualsiasi truppa leggera). Se il sensore non ha letto nulla
        (dict vuoto) restituisce True: si tenta comunque l'invio.
        """
        if not troops:
            return True
        keys: set[str]
        if unit_pref:
            norm = unit_pref.strip().lower()
            keys = set()
            for alias, ukeys in _UNIT_KEY_ALIASES.items():
                if alias in norm or norm in alias:
                    keys.update(ukeys)
            if not keys:
                keys = set(_LIGHT_RAIDERS)
        else:
            keys = set(_LIGHT_RAIDERS)
        return any(troops.get(k, 0) > 0 for k in keys)

    # ------------------------------------------------------------------
    # Custom Farm Target: unita' e quantita' per i raid mirati
    # ------------------------------------------------------------------
    @staticmethod
    def _raider_key(troops: dict[str, int], unit_pref: str) -> str:
        """Chiave 'tN' dell'unita' da saccheggio.

        Se `unit_pref` e' configurata la risolve via unit_label_to_key;
        altrimenti sceglie la prima unita' leggera disponibile a casa.
        """
        from .replenishment import unit_label_to_key  # import ritardato

        if unit_pref:
            key = unit_label_to_key(unit_pref)
            if key:
                return key
        # Auto: prima unita' raider presente (uN -> tN)
        for uk in sorted(_LIGHT_RAIDERS, key=lambda k: int(k[1:])):
            if troops.get(uk, 0) > 0:
                return "t" + uk[1:]
        # Letture parziali del sensore non devono azzerare l'intero
        # pacchetto: se restano solo unita' fuori whitelist (assedio,
        # esploratori, coloni) si usa comunque la piu' numerosa — il
        # gioco valida l'invio, non il backend.
        best = max(
            ((v, k) for k, v in troops.items() if v > 0),
            default=None,
        )
        if best is not None:
            return "t" + best[1][1:]
        return ""

    @staticmethod
    def _village_coords_map(settings: Settings) -> dict[str, tuple[int, int]]:
        """nome_lower -> (x, y) dei villaggi configurati con coordinate note.

        Serve al fallback di competenza territoriale quando il target
        approved non ha ``nearest_village`` valorizzato.
        """
        out: dict[str, tuple[int, int]] = {}
        for key, cfg_v in (getattr(settings, "villages", None) or {}).items():
            name = (getattr(cfg_v, "name", "") or str(key)).strip().lower()
            x = getattr(cfg_v, "x", None)
            y = getattr(cfg_v, "y", None)
            if name and x is not None and y is not None:
                out[name] = (int(x), int(y))
        return out

    def _schedule_scan(self, village_key: str, now: float | None = None) -> None:
        """Pianifica la prossima scansione mappa di scoperta target."""
        now = now if now is not None else time.time()
        self.next_scan[village_key] = now + random.uniform(
            SCAN_MIN_SECONDS, SCAN_MAX_SECONDS
        )
        self._save()

    async def _discover_targets(
        self,
        *,
        observer: "GameObserver",
        farm_store: "FarmTargetStore",
        village: "Village",
        vkey: str,
        settings: Settings,
        prefix: str,
        now: float,
    ) -> None:
        """Scansione mappa periodica: i tile razziabili diventano pending."""
        if not settings.farming.auto_scan:
            return
        if village.x is None or village.y is None:
            return
        if now < self.next_scan.get(vkey, 0.0):
            return
        try:
            scanned = await observer.scan_map_area(
                village.x, village.y,
                radius=max(5, settings.scout_brain.scan_radius),
            )
            if scanned:
                new = farm_store.register_scanned(
                    scanned,
                    origin_x=village.x,
                    origin_y=village.y,
                    nearest_village=village.name or "",
                    skip_names={village.name} if village.name else None,
                )
                logger.info(
                    "%s FarmTargets: scansione da '%s' -> %d tile, "
                    "%d nuovi pending.",
                    prefix, village.name, len(scanned), new,
                )
        except Exception as scan_exc:
            logger.debug(
                "%s FarmTargets: scansione fallita (%s)", prefix, scan_exc
            )
        finally:
            self._schedule_scan(vkey, now)

    async def _send_approved_raids(
        self,
        *,
        executor: "ActionExecutor",
        observer: "GameObserver",
        farm_store: "FarmTargetStore",
        settings: Settings,
        village: "Village",
        rally_slot: int,
        troops: dict[str, int],
        unit_pref: str,
        farm_unit_qty: int,
        prefix: str,
    ) -> bool:
        """Invia raid mirati (tt=2, move_type=4) sui target approved.

        Smart Pre-Raid Intelligence: prima di accodare un raid ogni
        target passa la verifica di fattibilita' — le oasi vengono
        lette dalla scheda tile mappa (bestie Natura, annessione,
        protezione) e valutate col combat_calc, i villaggi richiedono
        una spiata valida entro TTL (invio esploratori + parser report)
        e il contingente e' calcolato sul bottino spiato.
        Ritorna True se almeno un raid o una spiata e' partita.
        """
        approved = farm_store.get_approved_targets(
            near_x=village.x, near_y=village.y
        )
        if not approved:
            self._audit_event(
                "RAID_TARGET",
                "SKIPPED",
                "Nessun Custom Farm Target approvato da razziare",
            )
            return False

        # ------------------------------------------------------------------
        # Competenza territoriale + cooldown anti-doppio-invio (stessa
        # logica del gate AI Scout in orchestrator): il villaggio raida
        # solo i target di cui e' il piu' vicino e non colpiti di recente.
        # ------------------------------------------------------------------
        now_ts = time.time()
        min_revisit_sec = max(
            1200.0,
            float(
                getattr(
                    settings.farming,
                    "interval_min",
                    getattr(settings.farming, "interval_min_minutes", 25),
                )
                or 25
            )
            * 60.0,
        )
        vname_norm = (village.name or "").strip().lower()
        coords_map = self._village_coords_map(settings)
        eligible: list = []
        for t_obj in approved:
            tx, ty = int(t_obj.x), int(t_obj.y)

            # 1. Competenza territoriale (nearest_village, con fallback
            #    su distanza minima rispetto ai villaggi dell'account)
            nearest = (
                getattr(t_obj, "nearest_village", "") or ""
            ).strip().lower()
            if not nearest and coords_map:
                nearest = min(
                    coords_map.keys(),
                    key=lambda vn: (
                        (coords_map[vn][0] - tx) ** 2
                        + (coords_map[vn][1] - ty) ** 2
                    ),
                )
            if nearest and nearest != vname_norm:
                continue

            # 2. Cooldown per-coordinata (evita invii ravvicinati allo
            #    stesso target — last_raid_ts si aggiorna solo su
            #    invio confermato)
            last_sent = float(
                getattr(t_obj, "last_raid_ts", 0.0)
                or getattr(t_obj, "last_attempt_ts", 0.0)
                or 0.0
            )
            if last_sent > 0 and (now_ts - last_sent) < min_revisit_sec:
                continue

            eligible.append(t_obj)

        if not eligible:
            logger.info(
                "%s FarmTargets: nessun target approved di competenza "
                "o fuori cooldown per '%s'.",
                prefix, village.name,
            )
            self._audit_event(
                "RAID_TARGET",
                "SKIPPED",
                f"{len(approved)} target valutati, 0 idonei: tutti "
                "fuori competenza (nearest_village) o in cooldown "
                "per-coordinata",
                evaluated=len(approved),
            )
            return False
        approved = eligible

        # --------------------------------------------------------------
        # Configurazione Smart Pre-Raid Intelligence
        # --------------------------------------------------------------
        fcfg = settings.farming
        auto_scout = bool(getattr(fcfg, "auto_scout_villages", True))
        scout_ttl_s = float(getattr(fcfg, "scout_ttl_minutes", 120) or 120) * 60.0
        oasis_clear = bool(getattr(fcfg, "oasis_auto_clear", True))
        max_loss_pct = float(getattr(fcfg, "max_oasis_loss_pct", 0.0) or 0.0)
        min_loot = int(getattr(fcfg, "min_raid_loot", 150) or 150)
        micro_qty = max(2, min(5, int(getattr(fcfg, "micro_raid_qty", 3) or 3)))
        max_per_raid = int(getattr(fcfg, "max_troops_per_raid", 0) or 0)
        if farm_unit_qty and farm_unit_qty > 0:
            # Override per-villaggio: funge da tetto per-raid
            max_per_raid = int(farm_unit_qty)
        scouts_qty = max(1, int(getattr(fcfg, "scouts_per_mission", 1) or 1))
        bl_hours = float(getattr(fcfg, "defended_blacklist_hours", 24.0) or 24.0)
        wall_threshold = int(getattr(fcfg, "scout_wall_threshold", 10) or 10)
        pending_to_s = (
            float(getattr(fcfg, "scout_pending_timeout_minutes", 60) or 60)
            * 60.0
        )
        nature_ttl_s = (
            float(getattr(fcfg, "oasis_rescan_minutes", 30) or 30) * 60.0
        )
        hero_enabled = bool(
            getattr(settings.hero, "tactical_hero_enabled", True)
        )
        pause_hours = 12.0  # protezione principianti: pausa mezza giornata

        unit_key = self._raider_key(troops, unit_pref)
        if not unit_key:
            # Inventario a zero o sensore cieco: niente invio cieco — il
            # vecchio fallback 't1' produceva 'troop_inputs|missed:t1'
            # sui villaggi senza fanteria base.
            logger.info(
                "%s FarmTargets: nessuna unita' da raid disponibile in "
                "'%s' (lettura=%s, unit_pref='%s') — raid saltato.",
                prefix, village.name, troops, unit_pref,
            )
            self._audit_event(
                "RAID_SEND",
                "SKIPPED",
                f"Inventario a zero o sensore cieco: 0 truppe farm "
                f"disponibili in '{village.name}' "
                f"(lettura={troops}, unit_pref='{unit_pref}')",
                troops_read=dict(troops or {}),
                unit_pref=unit_pref,
            )
            return False

        max_wave = max(1, int(fcfg.targets_per_wave or 1))
        candidates = approved[:max_wave]
        available = troops.get("u" + unit_key[1:], troops.get(unit_key, 0))

        # Chiave esploratore per la tribu' dell'account (fallback su
        # qualunque unita' esploratrice presente in casa).
        scout_key = combat_calc.scout_unit_key(
            getattr(settings, "tribe", "") or ""
        )
        if not scout_key or int(troops.get(scout_key, 0) or 0) <= 0:
            scout_key = next(
                (
                    k for k in combat_calc.ALL_SCOUT_UNITS
                    if int(troops.get(k, 0) or 0) > 0
                ),
                scout_key,
            )
        scout_avail = int(troops.get(scout_key, 0) or 0)

        remaining: dict[str, int] = dict(troops or {})
        raid_plan: list[dict] = []
        scout_plan: list[dict] = []
        skipped: list[str] = []
        tile_reads = report_reads = 0

        async def _eval_target(t_obj) -> str:
            """Decide il destino del target: raid | scout | skip."""
            nonlocal scout_avail, tile_reads, report_reads
            tx, ty = int(t_obj.x), int(t_obj.y)

            # ============ Caso A: OASI (is_oasis == True) ============
            if bool(getattr(t_obj, "is_oasis", False)):
                details: dict | None = None
                nature = dict(getattr(t_obj, "nature_troops", {}) or {})
                stale = (
                    not nature
                    or (now_ts - float(getattr(t_obj, "nature_ts", 0.0) or 0.0))
                    > nature_ttl_s
                )
                if stale and tile_reads < MAX_TILE_READS_PER_CYCLE:
                    tile_reads += 1
                    try:
                        details = await observer.read_tile_details(tx, ty)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "%s PreRaid: scheda tile (%s|%s) non letta: %s",
                            prefix, tx, ty, exc,
                        )
                if details and details.get("ok"):
                    if details.get("protection"):
                        farm_store.set_pause_at(
                            tx, ty, pause_hours, "protezione principianti"
                        )
                        return (
                            f"({tx}|{ty}) pausa: protezione principianti"
                        )
                    nature = dict(details.get("nature_troops") or {})
                    farm_store.record_nature_troops(tx, ty, nature)
                    if details.get("oasis_annexed"):
                        # Oasi annessa a un villaggio giocatore ->
                        # si tratta come Caso B (villaggio).
                        pass
                    else:
                        return _eval_free_oasis(t_obj, nature)
                elif details is None and not nature:
                    # Lettura fallita e nessun dato precedente: prudenza,
                    # non si raida un'oasi di cui non si sa nulla.
                    return f"({tx}|{ty}) scheda oasi non leggibile"
                else:
                    return _eval_free_oasis(t_obj, nature)

            # ============ Caso B: VILLAGGIO (o oasi annessa) ============
            if not auto_scout:
                # Ricognizione disattivata: raid legacy su quantita' fissa.
                qty = farm_unit_qty if farm_unit_qty > 0 else AUTO_QTY_FLOOR
                qty = min(qty, remaining.get("u" + unit_key[1:], 0))
                if qty <= 0:
                    return f"({tx}|{ty}) truppe esaurite"
                remaining["u" + unit_key[1:]] -= qty
                raid_plan.append({
                    "x": tx, "y": ty,
                    "target_name": t_obj.name,
                    "troops": {unit_key: qty},
                })
                return "raid"

            t_obj = farm_store.find(t_obj.id) or t_obj  # dati freschi
            scouted_res = dict(
                getattr(t_obj, "scouted_resources", {}) or {}
            )
            scouted_trp = dict(getattr(t_obj, "scouted_troops", {}) or {})
            wall = int(getattr(t_obj, "scouted_wall", 0) or 0)
            last_scout = float(getattr(t_obj, "last_scout_ts", 0.0) or 0.0)
            pending = bool(getattr(t_obj, "scouting_pending", False))
            scout_req = float(
                getattr(t_obj, "last_scout_req_ts", 0.0) or 0.0
            )
            fresh = last_scout > 0 and (now_ts - last_scout) <= scout_ttl_s

            # --- Report spia in arrivo? Prova a raccoglierlo ---
            if not fresh and report_reads < MAX_REPORT_READS_PER_CYCLE:
                report_reads += 1
                try:
                    rep = await observer.parse_scout_report(tx, ty)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "%s PreRaid: report spia (%s|%s) non letto: %s",
                        prefix, tx, ty, exc,
                    )
                    rep = None
                if rep:
                    farm_store.record_scout_result(
                        tx, ty,
                        rep.get("resources") or {},
                        rep.get("troops") or {},
                        int(rep.get("wall_level") or 0),
                        float(rep.get("ts") or 0.0),
                    )
                    if rep.get("scouts_lost"):
                        farm_store.set_temp_blacklist_at(
                            tx, ty, bl_hours,
                            "Scout intercettati — target sorvegliato",
                        )
                        return (
                            f"({tx}|{ty}) scout intercettati: "
                            "blacklist 24h"
                        )
                    scouted_res = dict(rep.get("resources") or {})
                    scouted_trp = dict(rep.get("troops") or {})
                    wall = int(rep.get("wall_level") or 0)
                    last_scout = float(rep.get("ts") or 0.0)
                    fresh = True
                    pending = False
                elif pending:
                    if scout_req > 0 and (now_ts - scout_req) > pending_to_s:
                        # Spiata scaduta senza report: sblocca e rinvia.
                        farm_store._patch_at(
                            tx, ty, {"scouting_pending": False}
                        )
                        pending = False
                    else:
                        return f"({tx}|{ty}) spiata in corso"

            # --- Spiata valida: calcolo dinamico del raid ---
            if fresh:
                if scouted_trp or wall > wall_threshold:
                    farm_store.set_temp_blacklist_at(
                        tx, ty, bl_hours,
                        f"defended (wall={wall}, troops={scouted_trp})",
                    )
                    return f"({tx}|{ty}) difeso: blacklist 24h"
                loot = combat_calc.total_loot(scouted_res)
                if loot < min_loot:
                    farm_store.note_skip(
                        tx, ty, f"bottino {loot} < soglia {min_loot}"
                    )
                    return f"({tx}|{ty}) bottino {loot} < {min_loot}"
                qty = combat_calc.troops_for_loot(
                    loot, unit_key,
                    remaining.get("u" + unit_key[1:], 0),
                    max_troops_per_raid=max_per_raid,
                )
                if qty <= 0:
                    return f"({tx}|{ty}) truppe insufficienti"
                remaining["u" + unit_key[1:]] -= qty
                raid_plan.append({
                    "x": tx, "y": ty,
                    "target_name": t_obj.name,
                    "troops": {unit_key: qty},
                })
                return "raid"

            # --- Nessuna spiata: invio missione di spionaggio ---
            if pending:
                return f"({tx}|{ty}) spiata in corso"
            if scout_avail <= 0:
                farm_store.note_skip(
                    tx, ty, "SKIPPED (nessun esploratore disponibile)"
                )
                return f"({tx}|{ty}) nessun esploratore"
            if len(scout_plan) >= MAX_SCOUTS_PER_CYCLE:
                return f"({tx}|{ty}) quota spiate ciclo esaurita"
            qty = min(scouts_qty, scout_avail)
            scout_avail -= qty
            scout_plan.append({
                "x": tx, "y": ty,
                "target_name": t_obj.name,
                "troops": {"t" + scout_key[1:]: qty},
            })
            return "scout"

        def _eval_free_oasis(t_obj, nature: dict) -> str:
            """Regola zero/minime perdite sull'oasi libera (spec A.2)."""
            tx, ty = int(t_obj.x), int(t_obj.y)
            ukey = "u" + unit_key[1:]
            if not combat_calc.normalize_nature(nature):
                # Oasi vuota: micro-raid per il bottino rigenerato.
                qty = min(
                    micro_qty, remaining.get(ukey, 0), max_per_raid or 99
                )
                if qty <= 0:
                    return f"({tx}|{ty}) truppe esaurite"
                remaining[ukey] -= qty
                raid_plan.append({
                    "x": tx, "y": ty,
                    "target_name": t_obj.name,
                    "troops": {unit_key: qty},
                })
                return "raid"
            if not oasis_clear:
                return f"({tx}|{ty}) oasi presidiata: clear disattivato"
            clear_t, qty = combat_calc.min_troops_for_oasis(
                nature, remaining, unit_key,
                max_loss_pct=max_loss_pct,
                cap=max_per_raid or len(remaining) * 1000 or 0,
            )
            if not clear_t or qty <= 0:
                reason = (
                    f"Skipped: {combat_calc.nature_summary(nature)}"
                    " — forze insufficienti"
                )
                farm_store.note_skip(tx, ty, reason)
                return f"({tx}|{ty}) {reason}"
            remaining["u" + clear_t[1:]] -= qty
            entry: dict = {
                "x": tx, "y": ty,
                "target_name": t_obj.name,
                "troops": {clear_t: qty},
            }
            # Eroe tattico su oasi dense di belve con bottino alto.
            beasts = sum(combat_calc.normalize_nature(nature).values())
            if (
                hero_enabled and beasts >= 15
                and int(getattr(t_obj, "estimated_loot", 0) or 0) >= 1500
            ):
                entry["include_hero"] = True
            raid_plan.append(entry)
            return "raid"

        for t_obj in candidates:
            try:
                verdict = await _eval_target(t_obj)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "%s PreRaid: valutazione (%s|%s) fallita: %s",
                    prefix, t_obj.x, t_obj.y, exc,
                )
                verdict = f"({t_obj.x}|{t_obj.y}) errore valutazione"
            if verdict != "raid" and verdict != "scout":
                skipped.append(verdict)

        if skipped:
            self._audit_event(
                "RAID_TARGET",
                "SKIPPED",
                f"Pre-raid: {len(skipped)} target scartati",
                skipped=skipped[:12],
            )

        sent_any = False

        # ---- Missioni di spionaggio (Caso B step 3) ----
        if scout_plan:
            scout_action = Action(
                type=ActionType.SEND_SCOUT,
                params={
                    "rally_slot": rally_slot,
                    "mission": "scout",
                    "targets": scout_plan,
                },
                reason=(
                    f"Custom Farm Targets: {len(scout_plan)} spiate "
                    f"da '{village.name}' ({scout_key})"
                ),
            )
            sres = await executor.execute(
                scout_action, village_id=village.village_id
            )
            _s = (sres.data or {}).get("sent_coords")
            _s_coords: set[tuple[int, int]] = set()
            if _s is not None:
                for _c in _s:
                    try:
                        _s_coords.add((int(_c[0]), int(_c[1])))
                    except (TypeError, ValueError, IndexError):
                        continue
            for t in scout_plan:
                _ok = (
                    (t["x"], t["y"]) in _s_coords
                    if _s is not None else bool(sres.success)
                )
                if _ok:
                    farm_store.record_scout_sent(t["x"], t["y"])
            self._audit_event(
                "SCOUT_SEND",
                "EXECUTED" if sres.success else "BLOCKED",
                f"{len(scout_plan)} spiate da '{village.name}': "
                f"{sres.message}",
                coords=[[t["x"], t["y"]] for t in scout_plan],
            )
            if sres.success:
                sent_any = True

        # ---- Raid mirati (tt=2) ----
        raid_targets = raid_plan
        if not raid_targets:
            return sent_any
        action = Action(
            type=ActionType.SEND_CUSTOM_RAID,
            params={
                "rally_slot": rally_slot,
                "move_type": "4",  # 4 = saccheggio
                "targets": raid_targets,
            },
            reason=(
                f"Custom Farm Targets: {len(raid_targets)} raid mirati "
                f"da '{village.name}'"
            ),
        )
        result = await executor.execute(action, village_id=village.village_id)
        self._audit_event(
            "RAID_SEND",
            "EXECUTED" if result.success else "BLOCKED",
            f"{len(raid_targets)} raid mirati da "
            f"'{village.name}': {result.message}",
            coords=[[t['x'], t['y']] for t in raid_targets],
            unit=unit_key,
            troops=[t.get("troops") for t in raid_targets],
            expected_loot=int(
                sum(
                    int(getattr(t, 'estimated_loot', 0) or 0)
                    for t in candidates
                )
            ),
        )
        # Anti-ghost (hotfix): last_raid_ts si aggiorna SOLO per i
        # target la cui uscita truppe e' stata confermata visivamente
        # dal browser (result.data['sent_coords']); il successo del
        # batch non basta piu' — un submit su form non valido non deve
        # toccare il timestamp. Senza payload strutturato (executor
        # datato/test) si ricade sul comportamento precedente.
        _sent = (result.data or {}).get("sent_coords")
        _sent_coords: set[tuple[int, int]] = set()
        if _sent is not None:
            for _c in _sent:
                try:
                    _sent_coords.add((int(_c[0]), int(_c[1])))
                except (TypeError, ValueError, IndexError):
                    continue
        _failed = (result.data or {}).get("failed_targets") or []
        for t in raid_targets:
            try:
                _coord = (int(t["x"]), int(t["y"]))
            except (TypeError, ValueError, KeyError):
                continue
            _t_sent = (
                _coord in _sent_coords
                if _sent is not None
                else bool(result.success)
            )
            _reason = result.message
            if not _t_sent:
                for _ft in _failed:
                    try:
                        if (
                            int(_ft.get("x")), int(_ft.get("y"))
                        ) == _coord:
                            _reason = str(
                                _ft.get("reason") or _reason
                            )
                            break
                    except (TypeError, ValueError):
                        continue
            # Protezione principianti: submit rifiutato dal gioco con
            # il messaggio dedicato -> pausa del target, niente retry
            # a catena.
            _low = _reason.lower()
            if (
                "protezione principianti" in _low
                or "beginner" in _low
                or "protection" in _low
            ):
                farm_store.set_pause_at(
                    _coord[0], _coord[1], pause_hours, _reason
                )
            farm_store.record_raid_attempt(
                _coord[0], _coord[1], _t_sent, _reason
            )
        if result.success:
            logger.info(
                "%s FarmTargets: %s", prefix, result.message
            )
            return True
        logger.warning(
            "%s FarmTargets: raid custom non inviato: %s",
            prefix, result.message,
        )
        return False

    # ------------------------------------------------------------------
    # Decision Audit Log: eventi consumati dall'orchestrator
    # ------------------------------------------------------------------
    def _audit_event(
        self, decision: str, outcome: str, reason: str, **context
    ) -> None:
        self._audit.append(
            {
                "decision": decision,
                "outcome": outcome,
                "reason": reason,
                "context": context,
            }
        )

    def drain_audit(self) -> list[dict]:
        """Svuota il buffer degli eventi decisionali del ciclo."""
        events, self._audit = self._audit, []
        return events

    # ------------------------------------------------------------------
    # Ciclo principale per villaggio
    # ------------------------------------------------------------------
    async def run_for_village(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        rally_slot: int,
        prefix: str,
        farm_store: "FarmTargetStore | None" = None,
        now: float | None = None,
    ) -> bool:
        """Se scaduto e truppe disponibili, lancia i raid di farming.

        Priorita' ai Custom Farm Targets (store approved -> raid mirati
        tt=2); in assenza di target approvati ricade sulla farm list
        nativa del Gold Club (tt=99). Ritorna True se un raid e' partito.
        """
        cfg = settings.farming
        if not cfg.enabled:
            self._audit_event(
                "RAID_SEND", "SKIPPED", "Farming disabilitato in config"
            )
            return False
        vcfg = settings.village_config_for(village.name, village.village_id)
        if vcfg is not None and not vcfg.farm_enabled:
            self._audit_event(
                "RAID_SEND",
                "SKIPPED",
                "Farming disabilitato per questo villaggio (farm_enabled)",
            )
            return False
        if not rally_slot:
            self._audit_event(
                "RAID_SEND",
                "SKIPPED",
                "Nessun Punto di Raduno rilevato nel villaggio",
            )
            return False

        vkey = f"{village.name or village.village_id}"
        now = now if now is not None else time.time()
        if not self.is_due(vkey, now):
            self._audit_event(
                "RAID_SEND",
                "SKIPPED",
                f"Prossimo ciclo raid pianificato tra "
                f"{max(0, int(self.next_due.get(vkey, 0.0) - now))}s",
            )
            return False

        # Verifica truppe a casa prima di lanciare il comando
        unit_pref = (vcfg.farm_unit if vcfg else "") or cfg.unit
        troops = await observer.read_troops_home(rally_slot, village.village_id)
        if troops and not self._has_raiders(troops, unit_pref):
            logger.info(
                "%s Farming: nessuna truppa da saccheggio a casa in '%s' "
                "(presenti: %s). Riprovo piu' tardi.",
                prefix, village.name, troops,
            )
            self._audit_event(
                "RAID_SEND",
                "SKIPPED",
                f"0 truppe farm disponibili in '{village.name}' "
                f"(presenti: {troops})",
                troops_read=dict(troops or {}),
                unit_pref=unit_pref,
            )
            self._schedule_retry(vkey, now)
            return False

        # --- Custom Farm Target Manager (alternativa al Gold Club) ---
        if farm_store is not None:
            await self._discover_targets(
                observer=observer,
                farm_store=farm_store,
                village=village,
                vkey=vkey,
                settings=settings,
                prefix=prefix,
                now=now,
            )
            sent = await self._send_approved_raids(
                executor=executor,
                observer=observer,
                farm_store=farm_store,
                settings=settings,
                village=village,
                rally_slot=rally_slot,
                troops=troops,
                unit_pref=unit_pref,
                farm_unit_qty=(vcfg.farm_unit_qty if vcfg else 0),
                prefix=prefix,
            )
            if sent:
                due = self.schedule_next(vkey, settings, now)
                wait_min = (due - now) / 60.0
                logger.info(
                    "%s Farming: raid custom avviati da '%s'. "
                    "Prossimo ciclo tra ~%.0f min.",
                    prefix, village.name, wait_min,
                )
                return True
            if farm_store.get_approved_targets():
                # Target approved presenti ma invio fallito: retry breve
                self._schedule_retry(vkey, now)
                return False
            logger.info(
                "%s FarmTargets: nessun target approved per '%s' — "
                "approva i target in dashboard (fallback farm list).",
                prefix, village.name,
            )

        # --- Fallback: farm list nativa del Punto di Raduno (tt=99) ---
        action = Action(
            type=ActionType.SEND_RAID,
            params={
                "rally_slot": rally_slot,
                "farm_list": cfg.farm_list,
                "unit": unit_pref,
            },
            reason=(
                f"Farming automatico: raid dalla farm list "
                f"'{cfg.farm_list or '(prima disponibile)'}' "
                f"(villaggio {village.name})"
            ),
        )
        result = await executor.execute(action, village_id=village.village_id)
        if result.success:
            due = self.schedule_next(vkey, settings, now)
            wait_min = (due - now) / 60.0
            logger.info(
                "%s Farming: raid avviato da '%s'. Prossimo ciclo tra ~%.0f min.",
                prefix, village.name, wait_min,
            )
            return True

        logger.warning(
            "%s Farming: raid non avviato su '%s': %s. Retry programmato.",
            prefix, village.name, result.message,
        )
        if "nessuna farm list" in (result.message or "").lower():
            # Le liste non esistono: creazione manuale in-game richiesta.
            self.next_due[vkey] = now + random.uniform(
                NO_LIST_RETRY_MIN_SECONDS, NO_LIST_RETRY_MAX_SECONDS
            )
            self._save()
        else:
            self._schedule_retry(vkey, now)
        return False
