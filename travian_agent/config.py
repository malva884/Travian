"""Configurazione dell'agente, caricata da variabili d'ambiente / file .env."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

BASE_DIR = Path(__file__).resolve().parent.parent

# Percorso di accounts.json:
# 1. override esplicito via env ACCOUNTS_FILE (es. deploy personalizzati)
# 2. root del progetto, se il file esiste gia' (back-compat setup locale)
# 3. state/accounts.json (default: state/ e' il volume persistente su Docker/Coolify)
if os.getenv("ACCOUNTS_FILE"):
    ACCOUNTS_FILE = Path(os.environ["ACCOUNTS_FILE"])
elif (BASE_DIR / "accounts.json").exists():
    ACCOUNTS_FILE = BASE_DIR / "accounts.json"
else:
    ACCOUNTS_FILE = BASE_DIR / "state" / "accounts.json"


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\-_.]", "_", text)
    return cleaned.strip("_") or "default"


@dataclass
class VillageConfig:
    """Profilo strategico di un villaggio (Sprint 1, architettura macro/micro).

    Chiave nel dict Settings.villages: newdid (stringa) oppure nome villaggio.
    """

    name: str = ""
    role: str = "standard"          # hammer|defensive|croplog|feeder|standard
    target_village: str = ""        # chiave del villaggio che riceve il surplus (feeder)
    allowed_units: list[str] = field(default_factory=list)
    priority_units: list[str] = field(default_factory=list)
    building_focus: list[str] = field(default_factory=list)  # priorita' edifici (Sprint 3+)
    farm_enabled: bool = True       # include questo villaggio nei cicli di raid
    farm_unit: str = ""             # unita' da saccheggio (vuoto = truppe leggere auto)
    market_slot: int = 0            # slot dorf2 del Mercato (0 = auto-detect via gid=17)
    x: int | None = None            # coordinata X del villaggio
    y: int | None = None            # coordinata Y del villaggio
    # Grain Safety Net: margine netto grano minimo (unita'/ora).
    # 0 = eredita la soglia globale dell'account.
    min_net_crop_threshold: float = 0.0
    defense_enabled: bool = True    # include questo villaggio nella reazione anti-attacco
    # Smart Troop Replenishment: obiettivo numerico per unita' (chiave 'tN'
    # o nome unita'). deficit = target - (truppe a casa + truppe in coda).
    troop_targets: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.priority_units and self.allowed_units:
            self.priority_units = list(self.allowed_units)
        elif not self.allowed_units and self.priority_units:
            self.allowed_units = list(self.priority_units)


@dataclass
class ExpansionTarget:
    """Coordinate di un tile candidate alla fondazione di un villaggio."""

    x: int = 0
    y: int = 0
    note: str = ""


@dataclass
class ExpansionConfig:
    """Modulo espansione autonoma: fondazione di nuovi insediamenti."""

    auto_expand: bool = False
    founder_village: str = ""        # nome/newdid del villaggio fondatore (vuoto = auto)
    expansion_slot: int = 0          # slot dorf2 Residenza/Palazzo (26/31/32; 0 = auto)
    target_slots: list[ExpansionTarget] = field(default_factory=list)
    crop_min: int = 9                # campi grano minimi richiesti sul tile (9c/15c)
    min_culture_margin: int = 0      # margine CP richiesto oltre la soglia
    # Cropper Finder (human-in-the-loop): scansione mappa -> candidati dashboard
    cropper_scan: bool = True        # scansione automatica tile 9c/15c liberi
    scan_radius: int = 15            # raggio di scansione mappa in campi


@dataclass
class FarmingConfig:
    """Modulo farming: raid periodici dalle farm list del Punto di Raduno."""

    enabled: bool = False
    interval_min_minutes: float = 12.0  # jitter anti-bot: attesa tra un ciclo e l'altro
    interval_max_minutes: float = 18.0
    farm_list: str = ""                 # nome lista preferita (vuoto = prima disponibile)
    unit: str = ""                      # unita' da saccheggio default (vuoto = leggera)


@dataclass
class ScoutBrainConfig:
    """Modulo Intelligence e Raid Dinamico con AI (AI Scout Brain)."""

    enabled: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    scan_radius: int = 7                 # raggio di scansione mappa (in campi)
    max_travel_time_minutes: float = 20.0 # tempo massimo di marcia
    min_loot_ratio: float = 0.3          # rapporto minimo bottino / capacità per considerare il target redditizio
    blacklist_hours: float = 12.0        # ore di blacklist automatica se si registrano perdite
    max_targets_per_wave: int = 5        # numero massimo di raid contemporanei lanciati da AI


@dataclass
class HeroConfig:
    """Modulo eroe autonomo: avventure, auto-cura e punti attributo."""

    enabled: bool = False
    auto_adventures: bool = True        # spedizione automatica in avventura
    heal_threshold: int = 50            # % salute sotto cui usare gli unguenti
    min_health_to_adventure: int = 40   # % salute minima per la spedizione
    attribute_focus: str = "auto"       # auto|strength|off|def|resources


@dataclass
class ReplenishmentConfig:
    """Smart Troop Replenishment: rimpiazzo condizionato delle perdite.

    Decide dopo ogni report di combattimento se riaddestrare le unita'
    perse, fino al target per-villaggio (VillageConfig.troop_targets),
    con filtro anti-suicidio e bilanciamento risorse/grano.
    """

    enabled: bool = False
    # Blocco automatico del rimpiazzo (e degli attacchi) dopo fallimenti
    # ripetuti su target difesi: disattivare per addestrare comunque.
    auto_block: bool = True
    # Fallimenti consecutivi sullo stesso target prima del congelamento
    fail_streak_threshold: int = 2
    # Ondata minima di attacchi in arrivo per considerare il villaggio
    # 'sotto assedio' e sospendere rimpiazzo + rinforzi (Scenario 2)
    siege_waves: int = 2
    # Quota del granaio da preservare: sotto questa soglia non si addestra
    crop_reserve_pct: float = 0.25
    # True = l'addestramento cede il passo alle code di sviluppo economico
    skip_while_building: bool = True


@dataclass
class DefenseConfig:
    """Modulo difesa: reazione intelligente agli attacchi in arrivo.

    Ciclo per villaggio: classificazione fake/reale -> resource drain
    (coda costruzioni/addestramento massivo) -> troop dodging verso una
    destinazione sicura -> rientro automatico dopo l'impatto.
    """

    enabled: bool = False
    panic_threshold_seconds: int = 180   # sotto questo timer -> emergenza
    fake_troop_threshold: int = 5        # <= N truppe visibili => fake
    dodge_margin_seconds: int = 30       # esegui la fuga N sec prima dell'impatto
    recall_delay_seconds: int = 120      # rientro N sec dopo l'impatto
    min_dodge_troops: int = 10           # sotto questo totale non vale la fuga
    drain_via_market: bool = False       # surplus residuo -> mercato verso alleato
    drain_target_village: str = ""       # nome destinazione drain (vuoto = nessuna)
    dodge_target: str = ""               # nome villaggio o "x|y" (vuoto = proprio piu' vicino)


@dataclass
class Settings:
    server_url: str
    username: str = ""
    password: str = ""
    account_id: str = "default"
    headless: bool = True
    loop_interval_min: float = 30.0
    loop_interval_max: float = 90.0
    action_delay_min: float = 0.8
    action_delay_max: float = 2.5
    max_build_queue: int = 2
    max_resource_queue: int = 1
    max_interior_queue: int = 1
    # True = costruzione parallela campo+edificio (tribu' romana / Plus).
    # False = una sola costruzione alla volta per villaggio.
    allow_parallel_build: bool = True
    # True = attiva automaticamente il bonus +15% sulle risorse guardando il video alla scadenza (8h)
    auto_video_bonus: bool = True
    # Grain Safety Net: margine netto grano minimo globale (unita'/ora);
    # sotto soglia gli upgrade non-grano sono bloccati.
    min_net_crop_threshold: float = 10.0
    state_file: Path | None = None
    debug_dir: Path | None = None
    browser_locale: str = "it-IT"
    navigation_timeout_ms: int = 30_000
    # Profili per-villaggio: chiave = newdid o nome -> VillageConfig
    villages: dict[str, "VillageConfig"] = field(default_factory=dict)
    # DEPRECATO: mappa nome_villaggio -> ruolo, mantenuta per back-compat.
    # Usata solo se il villaggio non e' presente in `villages`.
    village_roles: dict = field(default_factory=dict)
    # Profilo strategico globale dell'account (es. "aggressive_hammer")
    strategy_profile: str = "balanced"
    # Moduli autonomi (dashboard sezione Farming & Espansione & AI Scout)
    farming: FarmingConfig = field(default_factory=FarmingConfig)
    expansion: ExpansionConfig = field(default_factory=ExpansionConfig)
    scout_brain: ScoutBrainConfig = field(default_factory=ScoutBrainConfig)
    hero: HeroConfig = field(default_factory=HeroConfig)
    defense: DefenseConfig = field(default_factory=DefenseConfig)
    replenishment: ReplenishmentConfig = field(default_factory=ReplenishmentConfig)

    def __post_init__(self) -> None:
        if not self.account_id:
            self.account_id = _slugify(self.server_url.replace("https://", "").replace("http://", ""))
        if self.state_file is None:
            self.state_file = BASE_DIR / "state" / f"session_{self.account_id}.json"
        if self.debug_dir is None:
            self.debug_dir = BASE_DIR / "debug" / self.account_id

    def village_config_for(
        self, name: str = "", village_id: int | str = 0
    ) -> "VillageConfig | None":
        """Config del villaggio per newdid o nome; None se non configurato."""
        key_id = str(village_id) if village_id else ""
        if key_id and key_id in self.villages:
            return self.villages[key_id]
        if name:
            if name in self.villages:
                return self.villages[name]
            for cfg in self.villages.values():
                if cfg.name == name:
                    return cfg
        return None

    def village_role_for(self, name: str = "", village_id: int | str = 0) -> str:
        """Ruolo del villaggio: `villages` > `village_roles` (legacy) > 'standard'."""
        cfg = self.village_config_for(name, village_id)
        if cfg is not None:
            return cfg.role
        if name and name in self.village_roles:
            return self.village_roles[name]
        return "standard"


def _read_accounts_json(path: Path) -> list | None:
    """Legge e valida un file accounts.json; None se assente/corrotto."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def _parse_account_item(item: dict) -> Settings:
    """Costruisce un Settings da una voce di accounts.json."""
    return Settings(
                            account_id=item.get("account_id") or _slugify(item.get("server_url", "acc")),
                            server_url=item.get("server_url", "").rstrip("/"),
                            username=item.get("username", ""),
                            password=item.get("password", ""),
                            headless=item.get("headless", True),
                            loop_interval_min=float(item.get("loop_interval_min", 30.0)),
                            loop_interval_max=float(item.get("loop_interval_max", 90.0)),
                            action_delay_min=float(item.get("action_delay_min", 0.8)),
                            action_delay_max=float(item.get("action_delay_max", 2.5)),
                            max_build_queue=int(item.get("max_build_queue", 2)),
                            max_resource_queue=int(item.get("max_resource_queue", 1)),
                            max_interior_queue=int(item.get("max_interior_queue", 1)),
                            allow_parallel_build=bool(item.get("allow_parallel_build", True)),
                            auto_video_bonus=bool(item.get("auto_video_bonus", True)),
                            min_net_crop_threshold=float(item.get("min_net_crop_threshold", 10.0)),
                            strategy_profile=str(item.get("strategy_profile", "balanced")),
                            farming=FarmingConfig(
                                **{
                                    k: v
                                    for k, v in dict(item.get("farming_settings") or {}).items()
                                    if k in FarmingConfig.__dataclass_fields__
                                }
                            ),
                            scout_brain=ScoutBrainConfig(
                                **{
                                    k: v
                                    for k, v in dict(item.get("scout_brain_settings") or {}).items()
                                    if k in ScoutBrainConfig.__dataclass_fields__
                                }
                            ),
                            hero=HeroConfig(
                                **{
                                    k: v
                                    for k, v in dict(item.get("hero_settings") or {}).items()
                                    if k in HeroConfig.__dataclass_fields__
                                }
                            ),
                            defense=DefenseConfig(
                                **{
                                    k: v
                                    for k, v in dict(item.get("defense_settings") or {}).items()
                                    if k in DefenseConfig.__dataclass_fields__
                                }
                            ),
                            replenishment=ReplenishmentConfig(
                                **{
                                    k: v
                                    for k, v in dict(item.get("replenishment_settings") or {}).items()
                                    if k in ReplenishmentConfig.__dataclass_fields__
                                }
                            ),
                            expansion=ExpansionConfig(
                                auto_expand=bool(
                                    dict(item.get("expansion_settings") or {}).get("auto_expand", False)
                                ),
                                founder_village=str(
                                    dict(item.get("expansion_settings") or {}).get("founder_village", "")
                                ),
                                crop_min=int(
                                    dict(item.get("expansion_settings") or {}).get("crop_min", 9)
                                ),
                                min_culture_margin=int(
                                    dict(item.get("expansion_settings") or {}).get("min_culture_margin", 0)
                                ),
                                cropper_scan=bool(
                                    dict(item.get("expansion_settings") or {}).get("cropper_scan", True)
                                ),
                                scan_radius=int(
                                    dict(item.get("expansion_settings") or {}).get("scan_radius", 15)
                                ),
                                target_slots=[
                                    ExpansionTarget(
                                        x=int(t.get("x", 0)),
                                        y=int(t.get("y", 0)),
                                        note=str(t.get("note", "")),
                                    )
                                    for t in (
                                        dict(item.get("expansion_settings") or {}).get("target_slots") or []
                                    )
                                    if isinstance(t, dict)
                                ],
                            ),
                            village_roles=dict(item.get("village_roles", {})),
                            villages={
                                str(key): VillageConfig(
                                    name=str(cfg.get("name", key)),
                                    role=str(cfg.get("role", "standard")),
                                    target_village=str(cfg.get("target_village", "")),
                                    allowed_units=list(cfg.get("allowed_units") or cfg.get("priority_units") or []),
                                    priority_units=list(cfg.get("priority_units") or cfg.get("allowed_units") or []),
                                    building_focus=list(cfg.get("building_focus", [])),
                                    farm_enabled=bool(cfg.get("farm_enabled", True)),
                                    farm_unit=str(cfg.get("farm_unit", "")),
                                    defense_enabled=bool(cfg.get("defense_enabled", True)),
                                    market_slot=int(cfg.get("market_slot", 0) or 0),
                                    min_net_crop_threshold=float(cfg.get("min_net_crop_threshold", 0.0) or 0.0),
                                    x=int(cfg["x"]) if cfg.get("x") is not None else None,
                                    y=int(cfg["y"]) if cfg.get("y") is not None else None,
                                    troop_targets={
                                        str(k): int(v)
                                        for k, v in dict(cfg.get("troop_targets") or {}).items()
                                        if int(v or 0) > 0
                                    },
                                )
                                for key, cfg in (item.get("villages") or {}).items()
                                if isinstance(cfg, dict)
                            },
    )


def load_accounts() -> list[Settings]:
    """Carica la lista di tutti gli account configurati.

    Se accounts.json esiste, carica quelli; altrimenti ricade su .env.
    Resiliente: file corrotto -> tenta il backup .bak; singola voce
    malformata -> la salta con un warning, senza perdere le altre.
    """
    if ACCOUNTS_FILE.exists():
        data = _read_accounts_json(ACCOUNTS_FILE)
        if data is None:
            bak = ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".bak")
            data = _read_accounts_json(bak)
            if data is not None:
                logger.warning(
                    "%s corrotto/illeggibile: lista account recuperata dal backup %s",
                    ACCOUNTS_FILE.name, bak.name,
                )
        if data:
            res: list[Settings] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    res.append(_parse_account_item(item))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Voce account ignorata in accounts.json (%s): %s",
                        item.get("account_id", "?"),
                        exc,
                    )
            if res:
                return res

    # Fallback su .env
    try:
        single = load_settings(require_credentials=False)
        return [single]
    except Exception:
        return []


def save_accounts(accounts: list[Settings], allow_empty: bool = False) -> None:
    """Salva la lista di account in accounts.json.

    Guardia anti-perdita: una lista vuota non sovrascrive mai un file
    gia' popolato salvo `allow_empty=True` (rimozione esplicita dell'ultimo
    account). Evita che un load fallito (es. fallback .env vuoto o
    parsing parziale) azzeri la configurazione.
    """
    if not accounts and not allow_empty:
        existing = _read_accounts_json(ACCOUNTS_FILE)
        if existing:
            logger.warning(
                "save_accounts: rifiutato il salvataggio di una lista vuota "
                "su %s popolato (%d account preservati)",
                ACCOUNTS_FILE.name, len(existing),
            )
            return
    data = []
    for acc in accounts:
        data.append({
            "account_id": acc.account_id,
            "server_url": acc.server_url,
            "username": acc.username,
            "password": acc.password,
            "headless": acc.headless,
            "loop_interval_min": acc.loop_interval_min,
            "loop_interval_max": acc.loop_interval_max,
            "action_delay_min": acc.action_delay_min,
            "action_delay_max": acc.action_delay_max,
            "max_build_queue": acc.max_build_queue,
            "max_resource_queue": acc.max_resource_queue,
            "max_interior_queue": acc.max_interior_queue,
            "allow_parallel_build": acc.allow_parallel_build,
            "auto_video_bonus": acc.auto_video_bonus,
            "min_net_crop_threshold": acc.min_net_crop_threshold,
            "strategy_profile": acc.strategy_profile,
            "farming_settings": {
                "enabled": acc.farming.enabled,
                "interval_min_minutes": acc.farming.interval_min_minutes,
                "interval_max_minutes": acc.farming.interval_max_minutes,
                "farm_list": acc.farming.farm_list,
                "unit": acc.farming.unit,
            },
            "scout_brain_settings": {
                "enabled": acc.scout_brain.enabled,
                "gemini_api_key": acc.scout_brain.gemini_api_key,
                "gemini_model": acc.scout_brain.gemini_model,
                "scan_radius": acc.scout_brain.scan_radius,
                "max_travel_time_minutes": acc.scout_brain.max_travel_time_minutes,
                "min_loot_ratio": acc.scout_brain.min_loot_ratio,
                "blacklist_hours": acc.scout_brain.blacklist_hours,
                "max_targets_per_wave": acc.scout_brain.max_targets_per_wave,
            },
            "hero_settings": {
                "enabled": acc.hero.enabled,
                "auto_adventures": acc.hero.auto_adventures,
                "heal_threshold": acc.hero.heal_threshold,
                "min_health_to_adventure": acc.hero.min_health_to_adventure,
                "attribute_focus": acc.hero.attribute_focus,
            },
            "defense_settings": {
                "enabled": acc.defense.enabled,
                "panic_threshold_seconds": acc.defense.panic_threshold_seconds,
                "fake_troop_threshold": acc.defense.fake_troop_threshold,
                "dodge_margin_seconds": acc.defense.dodge_margin_seconds,
                "recall_delay_seconds": acc.defense.recall_delay_seconds,
                "min_dodge_troops": acc.defense.min_dodge_troops,
                "drain_via_market": acc.defense.drain_via_market,
                "drain_target_village": acc.defense.drain_target_village,
                "dodge_target": acc.defense.dodge_target,
            },
            "replenishment_settings": {
                "enabled": acc.replenishment.enabled,
                "auto_block": acc.replenishment.auto_block,
                "fail_streak_threshold": acc.replenishment.fail_streak_threshold,
                "siege_waves": acc.replenishment.siege_waves,
                "crop_reserve_pct": acc.replenishment.crop_reserve_pct,
                "skip_while_building": acc.replenishment.skip_while_building,
            },
            "expansion_settings": {
                "auto_expand": acc.expansion.auto_expand,
                "founder_village": acc.expansion.founder_village,
                "expansion_slot": acc.expansion.expansion_slot,
                "crop_min": acc.expansion.crop_min,
                "min_culture_margin": acc.expansion.min_culture_margin,
                "cropper_scan": acc.expansion.cropper_scan,
                "scan_radius": acc.expansion.scan_radius,
                "target_slots": [
                    {"x": t.x, "y": t.y, "note": t.note}
                    for t in acc.expansion.target_slots
                ],
            },
            "village_roles": acc.village_roles,
            "villages": {
                key: {
                    "name": cfg.name,
                    "role": cfg.role,
                    "target_village": cfg.target_village,
                    "allowed_units": list(cfg.allowed_units),
                    "priority_units": list(cfg.priority_units),
                    "building_focus": list(cfg.building_focus),
                    "farm_enabled": cfg.farm_enabled,
                    "farm_unit": cfg.farm_unit,
                    "defense_enabled": cfg.defense_enabled,
                    "market_slot": int(cfg.market_slot or 0),
                    "min_net_crop_threshold": float(cfg.min_net_crop_threshold or 0.0),
                    "x": cfg.x,
                    "y": cfg.y,
                    "troop_targets": dict(cfg.troop_targets),
                }
                for key, cfg in acc.villages.items()
            },
        })
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    # Scrittura atomica: tmp + rename; backup del file precedente in .bak
    # (un kill a meta' scrittura lasciava un JSON troncato e gli account
    # sparivano al load successivo, silenziosamente rimpiazzati dal .env)
    tmp_path = ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    if ACCOUNTS_FILE.exists():
        try:
            shutil.copy2(
                ACCOUNTS_FILE,
                ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".bak"),
            )
        except Exception:  # noqa: BLE001
            pass
    os.replace(tmp_path, ACCOUNTS_FILE)


def get_raw_env(env_path: str | Path | None = None) -> dict[str, str]:
    """Legge i valori grezzi del file .env senza generare eccezioni."""
    path = Path(env_path or BASE_DIR / ".env")
    if not path.exists():
        return {}
    res = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        res[k.strip()] = v.strip().strip("'\"")
    return res


def save_env_settings(values: dict[str, str | int | float | bool], env_path: str | Path | None = None) -> None:
    """Salva le impostazioni nel file .env."""
    path = Path(env_path or BASE_DIR / ".env")
    lines = [
        "# Generato da Travian Agent Dashboard",
        f"TRAVIAN_SERVER_URL={values.get('TRAVIAN_SERVER_URL', '')}",
        f"TRAVIAN_USERNAME={values.get('TRAVIAN_USERNAME', '')}",
        f"TRAVIAN_PASSWORD={values.get('TRAVIAN_PASSWORD', '')}",
        "",
        f"HEADLESS={str(values.get('HEADLESS', True)).lower()}",
        f"LOOP_INTERVAL_MIN={values.get('LOOP_INTERVAL_MIN', 30.0)}",
        f"LOOP_INTERVAL_MAX={values.get('LOOP_INTERVAL_MAX', 90.0)}",
        f"ACTION_DELAY_MIN={values.get('ACTION_DELAY_MIN', 0.8)}",
        f"ACTION_DELAY_MAX={values.get('ACTION_DELAY_MAX', 2.5)}",
        f"MAX_BUILD_QUEUE={values.get('MAX_BUILD_QUEUE', 1)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_settings(
    env_path: str | Path | None = None,
    *,
    require_credentials: bool = True,
) -> Settings:
    """Carica la configurazione.

    `require_credentials=False` per i flussi che non usano il form di
    login (es. `--login` manuale): serve solo TRAVIAN_SERVER_URL.
    """
    load_dotenv(env_path or BASE_DIR / ".env")

    server_url = (os.getenv("TRAVIAN_SERVER_URL") or "").rstrip("/")
    username = os.getenv("TRAVIAN_USERNAME") or ""
    password = os.getenv("TRAVIAN_PASSWORD") or ""

    required = [("TRAVIAN_SERVER_URL", server_url)]
    if require_credentials:
        required += [
            ("TRAVIAN_USERNAME", username),
            ("TRAVIAN_PASSWORD", password),
        ]
    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            "Variabili d'ambiente mancanti: "
            + ", ".join(missing)
            + ". Crea un file .env partendo da .env.example."
        )

    return Settings(
        server_url=server_url,
        username=username,
        password=password,
        headless=_env_bool("HEADLESS", True),
        loop_interval_min=_env_float("LOOP_INTERVAL_MIN", 30.0),
        loop_interval_max=_env_float("LOOP_INTERVAL_MAX", 90.0),
        action_delay_min=_env_float("ACTION_DELAY_MIN", 0.8),
        action_delay_max=_env_float("ACTION_DELAY_MAX", 2.5),
        max_build_queue=int(_env_float("MAX_BUILD_QUEUE", 1)),
    )
