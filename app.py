"""Dashboard Web in Streamlit per configurare e gestire Travian Agent (Supporto Multi-Account)."""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import streamlit as st

import ui_theme as ui
from travian_agent import db
from travian_agent.builder import load_build_plans
from travian_agent.config import (
    BASE_DIR,
    ExpansionTarget,
    Settings,
    VillageConfig,
    load_accounts,
    save_accounts,
    save_env_settings,
)
from travian_agent.cropper import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SETTLED,
    STATUS_SETTLING,
    CropperStore,
)
from travian_agent.farm_store import (
    STATUS_APPROVED as FARM_STATUS_APPROVED,
    STATUS_BLACKLISTED as FARM_STATUS_BLACKLISTED,
    STATUS_PENDING as FARM_STATUS_PENDING,
    STATUS_REJECTED as FARM_STATUS_REJECTED,
    FarmTargetStore,
)
from travian_agent.models import STATIC_TROOP_LABELS, VillageRole
from travian_agent.presets import (
    allowed_unit_slots_for_role,
    detect_tribe_from_labels,
    preset_for_role,
    resolve_troop_labels,
    smithy_preset_for_role,
    troop_preset_for_role,
)

st.set_page_config(
    page_title="Travian Agent Multi-Account Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

ui.inject_theme()

STATE_DIR = BASE_DIR / "state"
DEBUG_DIR = BASE_DIR / "debug"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "agent.log"
PYTHON_EXE = sys.executable

# Regex per il tab Audits: formato "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.]\d+)?\s+"
    r"(?P<level>[A-Z]+)\s+(?P<logger>[\w.]+):\s*(?P<msg>.*)$"
)
_LOG_LEVEL_TONE = {
    "DEBUG": "neutral",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "danger",
    "CRITICAL": "danger",
}

if "agent_processes" not in st.session_state:
    # account_id -> subprocess.Popen degli agenti avviati da questa sessione
    st.session_state.agent_processes = {}


def _pid_alive(pid: int) -> bool:
    """True se il processo con quel PID esiste (senza inviare segnali)."""
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_running_agents() -> list[tuple[str, int]]:
    """Agenti attivi rilevati dai PID file (anche avviati fuori dalla dashboard).

    I file orfani di processi morti vengono ripuliti automaticamente.
    """
    found: list[tuple[str, int]] = []
    if not STATE_DIR.exists():
        return found
    for f in STATE_DIR.glob("agent_*.pid"):
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            continue
        if _pid_alive(pid):
            found.append((f.stem.removeprefix("agent_"), pid))
        else:
            f.unlink(missing_ok=True)
    return found


def is_agent_running() -> bool:
    """Agente attivo se avviato da questa sessione o rilevato dai PID file."""
    procs: dict[str, subprocess.Popen] = st.session_state.agent_processes
    for acc_id in [a for a, p in procs.items() if p.poll() is not None]:
        del procs[acc_id]
    return bool(procs) or bool(find_running_agents())


def stop_all_agents() -> int:
    """Termina i processi della sessione e tutti gli agenti dai PID file.

    I PID sono deduplicati: in modalita' orchestratore unico piu' file
    `agent_*.pid` possono contenere lo stesso PID del processo padre.
    """
    stopped_pids: set[int] = set()
    for proc in st.session_state.agent_processes.values():
        try:
            proc.terminate()
            stopped_pids.add(proc.pid)
        except Exception:  # noqa: BLE001
            pass
    st.session_state.agent_processes = {}
    for _acc_id, pid in find_running_agents():
        if pid in stopped_pids:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped_pids.add(pid)
        except Exception:  # noqa: BLE001
            pass
    return len(stopped_pids)


def set_account_stop(account_id: str) -> None:
    """Flag consumato dall'orchestratore: termina il loop del solo account."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"stop_{account_id}.flag").write_text(
        str(datetime.datetime.now()), encoding="utf-8"
    )


def stop_account(account_id: str) -> None:
    """Ferma il solo account indicato, senza toccare gli altri.

    - Se l'account gira in un processo dedicato (PID non condiviso), il
      processo viene terminato subito.
    - Se il PID e' condiviso con altri account (orchestratore unico avviato
      da CLI), scrive `stop_<id>.flag`: l'orchestratore chiude solo quel
      task al prossimo ciclo e rimuove il suo PID file.
    """
    proc = st.session_state.agent_processes.pop(account_id, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    running = find_running_agents()
    pid_counts: dict[int, int] = {}
    for _a, p in running:
        pid_counts[p] = pid_counts.get(p, 0) + 1
    killed = False
    for acc_id, pid in running:
        if acc_id == account_id and pid_counts.get(pid, 0) == 1:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except Exception:  # noqa: BLE001
                pass
    if killed:
        (STATE_DIR / f"agent_{account_id}.pid").unlink(missing_ok=True)
    else:
        # PID condiviso o gia' morto: uscita morbida via flag
        set_account_stop(account_id)
    set_account_pause(account_id, False)


def _on_village_account_change() -> None:
    """Cambio account nel tab Villaggi: invalida lo stato dei widget del form.

    Evita che gli input renderizzati per l'account precedente restino nel
    DOM o vengano riassociati ai villaggi dell'account appena selezionato.
    """
    st.session_state["village_form_nonce"] = (
        st.session_state.get("village_form_nonce", 0) + 1
    )
    for key in list(st.session_state.keys()):
        if key.startswith(
            (
                "role_", "target_", "units_", "focus_", "market_slot_",
                "coord_x_", "coord_y_", "bqueue_", "bqpreset_", "bqtroop_",
                "bqsmithy_", "fqtgt_", "fqup_",
                "fqdn_", "fqrm_", "fqadd_", "fqlvl_", "fqbtn_", "fqprst_",
            )
        ):
            del st.session_state[key]


def is_account_paused(account_id: str) -> bool:
    f = STATE_DIR / f"pause_{account_id}.flag"
    return f.exists()


def set_account_pause(account_id: str, paused: bool) -> None:
    f = STATE_DIR / f"pause_{account_id}.flag"
    if paused:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(str(datetime.datetime.now()), encoding="utf-8")
    else:
        f.unlink(missing_ok=True)


def get_latest_village_snapshots(account_id: str) -> dict[str, dict]:
    """Ultimo snapshot registrato per ciascun villaggio (tabella village_snapshots).

    Chiave DB = `safe_id` (stessa sanitizzazione di `_append_history`):
    due account con id simili non condividono mai lo storico.
    Fallback sul JSONL se la tabella e' vuota (es. DB rimosso).
    """
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
    snapshots = db.history_latest(safe_id)
    if snapshots:
        return snapshots

    history_file = STATE_DIR / "history" / f"history_{safe_id}.jsonl"
    if not history_file.exists():
        return {}

    snapshots: dict[str, dict] = {}
    try:
        with history_file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # Voci prive di account_id (storico legacy) restano valide;
                    # quelle di un altro account vengono scartate anche se il
                    # file e' condiviso (safe_id identico dopo sanitizzazione).
                    rec_acc = data.get("account_id")
                    if rec_acc is not None and rec_acc != account_id:
                        continue
                    vname = data.get("village_name") or f"id_{data.get('village_id', '?')}"
                    snapshots[vname] = data
                except Exception:
                    continue
    except Exception:
        pass
    return snapshots


def get_scout_brain_data(account_id: str) -> dict:
    """Dati intelligence AI Scout (kv DB, fallback file legacy)."""
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
    raw = db.kv_read(f"scout_{safe_id}.json")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    scout_file = STATE_DIR / f"scout_{safe_id}.json"
    if not scout_file.exists():
        return {}
    try:
        return json.loads(scout_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_hero_data(account_id: str) -> dict:
    """Ultimo snapshot dell'eroe (kv DB, fallback file legacy)."""
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
    raw = db.kv_read(f"hero_{safe_id}.json")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    hero_file = STATE_DIR / f"hero_{safe_id}.json"
    if not hero_file.exists():
        return {}
    try:
        return json.loads(hero_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_account_session_info(acc: Settings) -> dict:
    f = acc.state_file or (STATE_DIR / f"session_{acc.account_id}.json")
    if f.exists():
        mtime = os.path.getmtime(f)
        dt = datetime.datetime.fromtimestamp(mtime).strftime("%d/%m %H:%M")
        size_kb = f.stat().st_size / 1024
        return {"exists": True, "time": dt, "size": f"{size_kb:.1f} KB"}
    return {"exists": False, "time": "-", "size": "-"}


def _sidebar_nav() -> str:
    """Menu laterale a gruppi (layout console). Ritorna la pagina attiva.

    Ogni voce e' un bottone ghost; quella attiva e' evidenziata in accent.
    La selezione persiste in st.session_state["nav_page"].
    """
    cur = st.session_state.get("nav_page", "Overview")
    groups = [
        ("Agent", ["Overview", "Controllo", "Villaggi", "War Room"]),
        ("Automazioni", ["Farming & Eroe", "Espansione"]),
        ("Ops", ["Account", "Audits", "Debug"]),
    ]
    for gname, items in groups:
        ui.section(gname)
        for it in items:
            if st.button(
                it,
                key=f"navbtn_{it}",
                use_container_width=True,
                type="primary" if cur == it else "secondary",
            ):
                st.session_state["nav_page"] = it
                st.rerun()
    return st.session_state.get("nav_page", "Overview")


def _selected_account() -> "Settings | None":
    """Account attivo scelto nel selettore globale della sidebar.

    Tutte le pagine condividono la stessa selezione
    (``st.session_state['sel_account_id']``); se l'id non esiste piu'
    (account rimosso) ricade sul primo configurato.
    """
    if not accounts:
        return None
    cur = st.session_state.get("sel_account_id")
    return next(
        (a for a in accounts if a.account_id == cur),
        accounts[0],
    )


# Caricamento account configurati
accounts = load_accounts()

# --- BARRA LATERALE ---
with st.sidebar:
    ui.brand_block()

    # Navigazione a gruppi (layout console)
    page = _sidebar_nav()

    st.divider()
    ui.section("Account")
    if accounts:
        _acc_ids = [a.account_id for a in accounts]
        _cur_id = st.session_state.get("sel_account_id")
        _sel_idx = st.selectbox(
            "Account attivo:",
            range(len(accounts)),
            index=_acc_ids.index(_cur_id) if _cur_id in _acc_ids else 0,
            format_func=lambda i: accounts[i].account_id,
            key="sidebar_account_sel",
            on_change=_on_village_account_change,
        )
        st.session_state["sel_account_id"] = accounts[_sel_idx].account_id
        st.caption(accounts[_sel_idx].server_url)
    else:
        st.info("Nessun account configurato.")

    st.divider()
    ui.section("Controlli")

    # Stato Bot (sessione + orfani rilevati dai PID file)
    running_agents = find_running_agents()
    running = is_agent_running()
    if running:
        # Controllo stato di pausa per l'account principale/attivo
        paused_any = any(is_account_paused(acc.account_id) for acc in accounts)
        if paused_any:
            st.markdown(
                ui.pill("PAUSA D'EMERGENZA", "warning", "⏸"),
                unsafe_allow_html=True,
            )
        else:
            if running_agents:
                labels = ", ".join(f"{a} (PID {p})" for a, p in running_agents)
                st.markdown(
                    ui.pill("RUNNING", "success")
                    + f" <span class='dim'>{ui.esc(labels)}</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    ui.pill("RUNNING", "success"),
                    unsafe_allow_html=True,
                )

        # Toggle Pausa / Riprendi
        c_pause, c_stop = st.columns(2)
        with c_pause:
            if paused_any:
                if st.button("▶️ Riprendi", use_container_width=True, type="primary"):
                    for acc in accounts:
                        set_account_pause(acc.account_id, False)
                    st.toast("Bot ripreso!", icon="▶️")
                    time.sleep(0.5)
                    st.rerun()
            else:
                if st.button("⏸️ Pausa", use_container_width=True):
                    for acc in accounts:
                        set_account_pause(acc.account_id, True)
                    st.toast("Bot in pausa d'emergenza.", icon="⏸️")
                    time.sleep(0.5)
                    st.rerun()

        with c_stop:
            if st.button("⏹️ Ferma tutti", use_container_width=True, type="secondary"):
                n = stop_all_agents()
                for acc in accounts:
                    set_account_pause(acc.account_id, False)
                st.toast(f"Agente arrestato ({n} processo/i).", icon="⏹️")
                time.sleep(0.5)
                st.rerun()

        # Fermata selettiva: un pulsante per ciascun account in esecuzione,
        # cosi' si termina solo il server scelto senza toccare gli altri.
        for acc_id, pid in running_agents:
            if st.button(
                f"⏹️ Ferma `{acc_id}` (PID {pid})",
                key=f"stop_btn_{acc_id}",
                use_container_width=True,
            ):
                stop_account(acc_id)
                st.toast(f"Stop richiesto per '{acc_id}'.", icon="⏹️")
                time.sleep(0.5)
                st.rerun()
    else:
        st.markdown(
            ui.pill("AGENTE FERMO", "neutral", "⏹"),
            unsafe_allow_html=True,
        )

    st.divider()
    st.caption("⚠️ **Promemoria ToS**: Solo a scopo didattico / test.")


# --- CONTENUTO PRINCIPALE (dispatch pagine dal menu laterale) ---
if page == "Overview":
    ui.page_header(
        "Overview",
        "Stato operativo di agenti, villaggi e minacce in tempo reale.",
    )
    _running_ids_hdr = {a for a, _ in find_running_agents()}
    _hdr_villages = 0
    _hdr_attacks = 0
    for _acc in accounts:
        _snaps_h = get_latest_village_snapshots(_acc.account_id)
        _hdr_villages += len(_snaps_h) or len(_acc.villages)
        _hdr_attacks += sum(
            s.get("incoming_attacks", 0) for s in _snaps_h.values()
        )
    _hdr_farms = 0
    _hdr_pending = 0
    for _acc in accounts:
        try:
            _fts = FarmTargetStore(_acc.account_id).list_targets()
            _hdr_farms += len(_fts)
            _hdr_pending += sum(
                1 for t in _fts if t.status == FARM_STATUS_PENDING
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            _hdr_pending += sum(
                1
                for c in CropperStore().list_candidates(_acc.account_id)
                if c.status == STATUS_PENDING
            )
        except Exception:  # noqa: BLE001
            pass

    ui.kpi_row(
        [
            ui.kpi_card(
                "Agenti",
                len(accounts),
                "🤖",
                f"<b class='ok'>{len(_running_ids_hdr)} Running</b> | "
                f"<b class='bad'>{len(accounts) - len(_running_ids_hdr)} Stopped</b>",
            ),
            ui.kpi_card("Villaggi", _hdr_villages, "🏰", "villaggi monitorati"),
            ui.kpi_card("Farm Targets", _hdr_farms, "🎯", "nel catalogo raid"),
            ui.kpi_card(
                "Approvazioni",
                _hdr_pending,
                "⏳",
                "<b class='warn'>in attesa di decisione</b>"
                if _hdr_pending
                else "<b class='ok'>nessuna in sospeso</b>",
                "danger" if _hdr_pending else "",
            ),
            ui.kpi_card(
                "Attacchi",
                _hdr_attacks,
                "⚔️",
                "<b class='bad'>in arrivo</b>"
                if _hdr_attacks
                else "<b class='ok'>nessuno in arrivo</b>",
                "danger" if _hdr_attacks else "",
            ),
        ]
    )

    ui.section("Agenti")
    if accounts:
        _agent_rows: list[list[str]] = []
        for acc in accounts:
            s_info = get_account_session_info(acc)
            _snaps_a = get_latest_village_snapshots(acc.account_id)
            _atk_a = sum(
                s.get("incoming_attacks", 0) for s in _snaps_a.values()
            )
            if acc.account_id in _running_ids_hdr:
                _status = ui.pill("Running", "success")
            elif is_account_paused(acc.account_id):
                _status = ui.pill("Paused", "warning")
            else:
                _status = ui.pill("Stopped", "neutral")
            _sess = (
                ui.pill(f"Valid · {s_info['time']}", "success")
                if s_info["exists"]
                else ui.pill("Missing", "danger")
            )
            _agent_rows.append(
                [
                    f"<b>{ui.esc(acc.account_id)}</b>",
                    f"<span class='mono dim'>{ui.esc(acc.server_url)}</span>",
                    _status,
                    _sess,
                    str(len(_snaps_a) or len(acc.villages)),
                    ui.pill(str(_atk_a), "danger", "⚔")
                    if _atk_a
                    else "<span class='dim'>0</span>",
                    f"<span class='dim'>{ui.esc(acc.username or 'login manuale')}</span>",
                ]
            )
        ui.render_table(
            ["Agent", "Server", "Status", "Sessione", "Villaggi", "Attacchi", "Utente"],
            _agent_rows,
        )
    else:
        st.info(
            "Nessun account configurato — aggiungine uno dalla pagina Account."
        )


# 1. PAGINA CONTROLLO & AVVIO
if page == "Controllo":
    ui.page_header(
        "Controllo",
        "Login assistito, avvio e arresto degli agenti.",
    )
    st.subheader("1. Login Manuale Assistito")
    st.write(
        "Effettua il login sul browser per l'account desiderato per salvare cookie e sessione (`session_<account_id>.json`)."
    )

    if not accounts:
        st.warning("Configura almeno un account nella scheda '👥 Gestione Account / Server'.")
    else:
        col_acc_sel, col_btn_login = st.columns([2, 2])
        with col_acc_sel:
            chosen_acc = _selected_account()
            st.markdown(
                f"Account selezionato (sidebar): **{ui.esc(chosen_acc.account_id)}**"
                f"<br><span class='dim'>{ui.esc(chosen_acc.server_url)}</span>",
                unsafe_allow_html=True,
            )

        with col_btn_login:
            st.write(" ")
            st.write(" ")
            if st.button(f"🌐 Apri Finestra Login per '{chosen_acc.account_id}'", type="primary", use_container_width=True):
                if not chosen_acc.server_url or "ts1.travian.it" in chosen_acc.server_url:
                    st.error("⚠️ URL server non valido!")
                else:
                    st.info(f"Apertura browser per '{chosen_acc.account_id}'... Completa l'accesso nella finestra.")
                    try:
                        subprocess.Popen(
                            [PYTHON_EXE, "main.py", "--login", "--account", chosen_acc.account_id],
                            cwd=str(BASE_DIR),
                            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                        )
                        st.success("Finestra aperta! Dopo il login, ricarica la pagina.")
                    except Exception as ex:
                        st.error(f"Errore: {ex}")

    st.divider()

    st.subheader("2. Esecuzione Agente (Singolo o Parallelo)")
    c1, c2 = st.columns([1, 1])

    with c1:
        run_mode = st.radio(
            "Modalità di Esecuzione:",
            ["Tutti gli account contemporaneamente (Parallelo)", "Solo un account specifico"],
            index=0,
        )
        target_account = None
        if "Solo un account" in run_mode and accounts:
            _sel = _selected_account()
            target_account = _sel.account_id
            st.caption(
                f"Account da eseguire: **{target_account}**"
                " (selettore nella sidebar)"
            )

        brain_choice = st.selectbox(
            "Cervello Decisionale:",
            ["rules (Regole deterministiche)", "llm (Modello LLM con fallback)"],
        )
        selected_brain = "rules" if "rules" in brain_choice else "llm"

        headless_opt = st.checkbox("Esegui in background (Headless / senza finestre)", value=True)
        single_run = st.checkbox("Esegui una sola iterazione (Test rapido)", value=False)

    with c2:
        st.write(" ")
        st.write(" ")
        if st.button("▶️ Avvia Agente / Account", type="primary", use_container_width=True):
            if not accounts:
                st.error("Configura prima almeno un account!")
            else:
                running_ids = {a for a, _ in find_running_agents()}
                target_accounts = (
                    [a for a in accounts if a.account_id == target_account]
                    if target_account
                    else list(accounts)
                )
                started = 0
                for acc in target_accounts:
                    if acc.account_id in running_ids:
                        continue
                    # Un processo dedicato per account: PID file e file di
                    # log separati, cosi' la fermata selettiva termina solo
                    # il server scelto e gli stati non si mescolano.
                    cmd = [
                        PYTHON_EXE, "main.py",
                        "--account", acc.account_id,
                        "--brain", selected_brain,
                    ]
                    if not headless_opt:
                        cmd.append("--headed")
                    if single_run:
                        cmd.append("--once")

                    log_path = STATE_DIR / f"agent_{acc.account_id}.log"
                    log_f = open(log_path, "a", encoding="utf-8")
                    log_f.write(
                        f"\n--- AVVIO AGENTE {acc.account_id}: {datetime.datetime.now()} ---\n"
                    )
                    log_f.flush()

                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(BASE_DIR),
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    st.session_state.agent_processes[acc.account_id] = proc
                    started += 1
                if started:
                    st.success(f"Avviati {started} agente/i (un processo dedicato per account).")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.info("Gli account selezionati sono già in esecuzione.")
        if is_agent_running():
            if st.button("⏹️ Arresta Tutti", type="secondary", use_container_width=True):
                n = stop_all_agents()
                st.toast(f"Arrestato ({n} processo/i).", icon="⏹️")
                time.sleep(0.5)
                st.rerun()


# 2. PAGINA VILLAGGI & STRATEGIA
if page == "Villaggi":
    ui.page_header(
        "Villaggi",
        "Risorse, code e attacchi in tempo reale — ruoli strategici con applicazione a caldo (senza riavvio).",
    )

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        # Account attivo: selettore globale in sidebar
        col_acc, col_strat = st.columns([1, 1])
        with col_acc:
            cur_acc = _selected_account()
            st.markdown(
                f"**{ui.esc(cur_acc.account_id)}**"
                f"<br><span class='dim'>{ui.esc(cur_acc.server_url)}</span>",
                unsafe_allow_html=True,
            )

        with col_strat:
            strategy_profiles = ["balanced", "aggressive_hammer", "defensive_wall", "croplog_focus", "economic_growth"]
            strategy_labels = {
                "balanced": "⚖️ Bilanciato — Sviluppo omogeneo di tutti i villaggi",
                "aggressive_hammer": "⚔️ Hammer Offensivo — Risorse convogliate al villaggio d'attacco",
                "defensive_wall": "🛡️ Muro Difensivo — Priorità a fortificazioni e truppe di scudo",
                "croplog_focus": "🌾 Capitale Grano (Croplog) — Focus 100% grano e granai enormi",
                "economic_growth": "📦 Espansione Economica — Priorità a mercati, magazzini e feeder",
            }
            current_strat = cur_acc.strategy_profile if cur_acc.strategy_profile in strategy_profiles else "balanced"
            new_strat = st.selectbox(
                "Profilo Strategico Globale (Account Brain):",
                strategy_profiles,
                index=strategy_profiles.index(current_strat),
                format_func=lambda s: strategy_labels.get(s, s),
                help="Determina le priorità complessive dell'AccountBrain per il bilanciamento inter-villaggio",
            )
            tribe_choices = ["auto", "romans", "teutons", "gauls"]
            tribe_labels = {
                "auto": "🔎 Auto — rileva la tribù dalle truppe/config",
                "romans": "🏛️ Romani (t1-t10)",
                "teutons": "⚔️ Teutoni (t11-t20)",
                "gauls": "🌿 Galli (t21-t30)",
            }
            _tribe_idx = (
                tribe_choices.index(cur_acc.tribe)
                if cur_acc.tribe in tribe_choices[1:]
                else 0
            )
            new_tribe = st.selectbox(
                "Tribù Account:",
                tribe_choices,
                index=_tribe_idx,
                format_func=lambda t: tribe_labels.get(t, t),
                key=f"tribe_{cur_acc.account_id}",
                help="Serve a risolvere i preset truppe (es. fanteria base "
                     "= Legionario/Combattente/Falange). 'Auto' rileva a runtime.",
            )

        # Snapshot in tempo reale da history
        snapshots = get_latest_village_snapshots(cur_acc.account_id)

        st.markdown("#### 📊 Stato Villaggi in Tempo Reale")
        if not snapshots and not cur_acc.villages:
            st.info("Nessun dato registrato. Avvia l'agente per effettuare la prima scansione dei villaggi.")
        else:
            # Lista di tutti i villaggi conosciuti (da config o da snapshot)
            all_vnames = list(cur_acc.villages.keys())
            for sv_name in snapshots.keys():
                if sv_name not in all_vnames:
                    all_vnames.append(sv_name)

            for vname in all_vnames:
                snap = snapshots.get(vname, {})
                vcfg = cur_acc.villages.get(vname, VillageConfig(name=vname))
                st.markdown(
                    ui.village_card(
                        name=vname,
                        role=vcfg.role or "standard",
                        target=vcfg.target_village,
                        snap=snap,
                    ),
                    unsafe_allow_html=True,
                )

        st.divider()

        # Editor di Ruoli e Strategia
        st.markdown("#### ⚙️ Configurazione Ruoli Villaggio (Micro-Level Brain)")
        st.write("Modifica i ruoli strategici e i target di spedizione. I cambiamenti vengono salvati su file e **letti all'istante dal bot al prossimo ciclo senza riavvio**.")

        # Form key per-account + nonce: al cambio account i widget del
        # form precedente vengono smontati, niente residui nel DOM.
        form_nonce = st.session_state.get("village_form_nonce", 0)
        with st.form(f"village_roles_form_{cur_acc.account_id}_{form_nonce}"):
            all_known = list(cur_acc.villages.keys())
            for sv in snapshots.keys():
                if sv not in all_known:
                    all_known.append(sv)

            updated_villages: dict[str, VillageConfig] = {}
            target_options = ["(Nessuno)"] + [k for k in all_known]

            role_choices = [
                "standard", "early_cropper_founder", "hammer", "defensive",
                "croplog", "feeder", "chiefing",
            ]
            role_labels = {
                "standard": "standard — Sviluppo bilanciato campi ed edifici",
                "early_cropper_founder": "founder (Early Cropper/Pioniere 15c) — Avvio rapido, fanteria base + Residenza per i coloni",
                "hammer": "hammer (Offensivo) — Caserma/Stalla/Fucina + Truppe d'attacco",
                "defensive": "defensive (Scudo) — Caserma/Mura/Residenza + Truppe difesa",
                "croplog": "croplog (Capitale) — Focus 100% Grano + Granai/Mulino/Panificio",
                "feeder": "feeder (Economico) — Mercato/Magazzini + Spedizione surplus al Target",
                "chiefing": "chiefing (Espansione) — Accademia/Residenza-Palazzo/Municipio per i nobili",
            }

            # Chiavi canoniche uniche degli edifici per il multiselect (senza alias duplicati)
            canonical_focus_keys: list[str] = [
                # --- CAMPI RISORSA (Economia base) ---
                "cropland",
                "woodcutter",
                "clay_pit",
                "iron_mine",
                # --- STOCCAGGIO & COMMERCIO ---
                "granary",
                "warehouse",
                "great_granary",
                "great_warehouse",
                "marketplace",
                "trade_office",
                # --- POTENZIAMENTO RISORSE (+25%) ---
                "grain_mill",
                "bakery",
                "sawmill",
                "brickyard",
                "iron_foundry",
                # --- AMMINISTRAZIONE & INFRASTRUTTURE ---
                "main_building",
                "rally_point",
                "residence",
                "palace",
                "residence_palace",
                "town_hall",
                "treasury",
                "embassy",
                "hero_mansion",
                "cranny",
                "stonemason_lodge",
                # --- MILITARI & ADDESTRAMENTO ---
                "barracks",
                "stable",
                "workshop",
                "academy",
                "smithy",
                "great_barracks",
                "great_stable",
                "tournament_square",
                "trapper",
                "hospital",
                # --- DIFESA E MURA ---
                "city_wall",
                "earth_wall",
                "palisade",
                # --- EDIFICI SPECIALI ---
                "horse_drinking_trough",
                "brewery",
                "waterworks",
            ]

            # Mappa etichette dettagliate in italiano per gli edifici
            building_focus_labels: dict[str, str] = {
                # --- CAMPI RISORSA ---
                "cropland": "🌾 Campi di Grano (Tutti)",
                "woodcutter": "🌲 Taglialegna (Tutti)",
                "clay_pit": "🧱 Pozzi d'Argilla (Tutti)",
                "iron_mine": "⛏️ Miniere di Ferro (Tutti)",

                # --- STOCCAGGIO & COMMERCIO ---
                "granary": "🌾 Granaio",
                "warehouse": "📦 Magazzino",
                "great_granary": "🌾 Grande Granaio (Capitale)",
                "great_warehouse": "📦 Grande Magazzino (Capitale)",
                "marketplace": "⚖️ Mercato",
                "market": "⚖️ Mercato",  # alias retrocompatibile
                "trade_office": "📈 Ufficio Commerciale (+Capacità mercanti)",

                # --- POTENZIAMENTO RISORSE (+25%) ---
                "grain_mill": "🌾 Mulino (+25% Grano)",
                "bakery": "🥖 Panificio (+25% Grano)",
                "sawmill": "🪚 Segheria (+25% Legno)",
                "brickyard": "🧱 Fabbrica di Mattoni (+25% Argilla)",
                "iron_foundry": "⛏️ Fonderia (+25% Ferro)",

                # --- AMMINISTRAZIONE & INFRASTRUTTURE ---
                "main_building": "🏛️ Edificio Principale (Velocità costruzioni)",
                "main": "🏛️ Edificio Principale",  # alias retrocompatibile
                "rally_point": "🚩 Punto di Raduno",
                "residence": "🏠 Residenza (Espansione / Slot fondazione)",
                "palace": "🏰 Palazzo (Capitale / Espansione)",
                "residence_palace": "🏠/🏰 Residenza o Palazzo (auto: Residenza, Palazzo in capitale)",
                "town_hall": "🎉 Municipio (Grandi Feste / Punti Cultura)",
                "treasury": "💰 Tesoreria (Custodia Artefatti)",
                "embassy": "🤝 Ambasciata",
                "hero_mansion": "🦸 Casa dell'Eroe (Annessione Oasi)",
                "cranny": "🕳️ Nascondiglio (Protezione risorse)",
                "stonemason_lodge": "🪨 Bottega del Marmista (Stabilità edifici capitale)",

                # --- MILITARI & ADDESTRAMENTO ---
                "barracks": "⚔️ Caserma (Addestramento Fanteria)",
                "stable": "🐎 Scuderia (Addestramento Cavalleria)",
                "workshop": "🛠️ Officina (Arieti & Catapulte)",
                "academy": "🎓 Accademia (Sblocco Ricerche)",
                "smithy": "⚒️ Fucina (Potenziamento Armi e Armature)",
                "armoury": "⚒️ Fucina / Armeria",  # alias retrocompatibile
                "great_barracks": "⚔️ Grande Caserma (Doppio addestramento capitale)",
                "great_stable": "🐎 Grande Scuderia (Doppio addestramento capitale)",
                "tournament_square": "🏟️ Piazza del Torneo (Velocità truppe >20 campi)",
                "trapper": "🪤 Trappolaio (Galli — Cattura truppe nemiche)",
                "hospital": "🏥 Ospedale (Cura e recupero feriti)",

                # --- DIFESA E MURA ---
                "city_wall": "🧱 Mura Cittadine (Tutte le tribù)",
                "wall": "🧱 Mura Cittadine",  # alias retrocompatibile
                "earth_wall": "🧱 Terrapieno (Teutoni)",
                "palisade": "🪵 Staccionata (Galli)",

                # --- EDIFICI SPECIALI ---
                "horse_drinking_trough": "🐴 Abbeveratoio (Romani — Riduce consumo grano cavalli)",
                "brewery": "🍺 Birreria (Teutoni — Bonus attacco)",
                "waterworks": "⛲ Idrovora (Aumento produzione oasi)",
            }

            focus_alias_map: dict[str, str] = {
                "main": "main_building",
                "market": "marketplace",
                "wall": "city_wall",
                "armoury": "smithy",
            }

            # Mappa etichette dettagliate per le unità con tribù e ruolo tattico
            unit_display_labels: dict[str, str] = {
                # --- ROMANI (t1 - t10) ---
                "Legionario": "🏛️ [Romani] Legionario — Fanteria base / Difesa",
                "Pretoriano": "🏛️ [Romani] Pretoriano — Difesa anti-fanteria",
                "Imperiano": "🏛️ [Romani] Imperiano — Fanteria d'attacco",
                "Equites Legatus": "🏛️ [Romani] Equites Legatus — Esploratore / Spia",
                "Equites Imperatoris": "🏛️ [Romani] Equites Imperatoris — Cavalleria d'attacco / Raid",
                "Equites Caesaris": "🏛️ [Romani] Equites Caesaris — Cavalleria pesante",
                "Ariete": "🪵 Ariete — Macchina d'assedio",
                "Catapulta da fuoco": "🏛️ [Romani] Catapulta da fuoco — Assedio / Distruzione",
                "Senatore": "🏛️ [Romani] Senatore — Conquista villaggi",
                "Colono": "🌾 Colono — Fondazione nuovi villaggi",

                # --- TEUTONI (t11 - t20) ---
                "Mazziere": "⚔️ [Teutoni] Mazziere — Attacco rapido / Raid economico",
                "Lanciere": "⚔️ [Teutoni] Lanciere — Difesa anti-cavalleria",
                "Guerriero con l'ascia": "⚔️ [Teutoni] Guerriero con l'ascia — Fanteria d'attacco",
                "Spia": "⚔️ [Teutoni] Spia — Esploratore / Ricognizione",
                "Paladino": "⚔️ [Teutoni] Paladino — Cavalleria difesa / Raid veloce",
                "Cavaliere Teutonico": "⚔️ [Teutoni] Cavaliere Teutonico — Cavalleria pesante d'assalto",
                "Catapulta": "⚔️ [Teutoni] Catapulta — Assedio / Distruzione",
                "Capotribù": "⚔️ [Teutoni] Capotribù — Conquista villaggi",

                # --- GALLI (t21 - t30) ---
                "Falange": "🏹 [Galli] Falange — Difesa versatile ed economica",
                "Spadaccino": "🏹 [Galli] Spadaccino — Fanteria d'attacco",
                "Tracciatore": "🏹 [Galli] Tracciatore — Esploratore a cavallo",
                "Fulmine di Teutates": "🏹 [Galli] Fulmine di Teutates — Cavalleria velocissima da raid",
                "Cavaliere Druido": "🏹 [Galli] Cavaliere Druido — Difesa anti-fanteria",
                "Eduano": "🏹 [Galli] Eduano — Cavalleria pesante",
                "Trabucco": "🏹 [Galli] Trabucco — Assedio / Distruzione",
                "Comandante": "🏹 [Galli] Comandante — Conquista villaggi",

                # --- ALIAS E PLURALI RETROCOMPATIBILI ---
                "Legionari": "🏛️ [Romani] Legionario (Legionari) — Fanteria base / Difesa",
                "Pretoriani": "🏛️ [Romani] Pretoriano (Pretoriani) — Difesa anti-fanteria",
                "Imperiani": "🏛️ [Romani] Imperiano (Imperiani) — Fanteria d'attacco",
                "Mazzieri": "⚔️ [Teutoni] Mazziere (Mazzieri) — Attacco rapido / Raid",
                "Lancieri": "⚔️ [Teutoni] Lanciere (Lancieri) — Difesa anti-cavalleria",
                "Falangi": "🏹 [Galli] Falange (Falangi) — Difesa versatile",
                "Spadaccini": "🏹 [Galli] Spadaccino (Spadaccini) — Fanteria d'attacco",
            }

            # Nomi sintetici delle unità per i widget di target numerico
            unit_short_names: dict[str, str] = {
                k: v.split("—")[0].strip() for k, v in unit_display_labels.items()
            }

            for idx, vname in enumerate(all_known):
                old_cfg = cur_acc.villages.get(vname, VillageConfig(name=vname))
                # Suffisso indice: due villaggi i cui nomi si sanitizzano
                # uguali (es. "Villaggio 1" vs "Villaggio_1") non devono
                # condividere chiavi widget e coda prioritaria.
                vkey = (
                    "".join(c if c.isalnum() or c in "._-" else "_" for c in vname)
                    + f"_{idx}"
                )

                with st.expander(f"🏰 Configurazione per '{vname}'", expanded=True):
                    # --- SEZIONE 1: RUOLO STRATEGICO & TARGET FEEDER ---
                    st.markdown("##### 👑 Ruolo Strategico & Connessioni Rete")
                    r_col1, r_col2 = st.columns([1, 1])
                    with r_col1:
                        cur_role_val = old_cfg.role if old_cfg.role in role_choices else "standard"
                        new_role = st.selectbox(
                            f"Ruolo Strategico ({vname}):",
                            role_choices,
                            index=role_choices.index(cur_role_val),
                            format_func=lambda r: role_labels.get(r, r),
                            key=f"role_{cur_acc.account_id}_{vkey}",
                            help="Determina la logica autonoma di sviluppo del villaggio (VillageBrain).",
                        )
                    with r_col2:
                        cur_target = old_cfg.target_village if old_cfg.target_village in target_options else "(Nessuno)"
                        new_target = st.selectbox(
                            f"Villaggio Target Spedizione (per Feeder):",
                            target_options,
                            index=target_options.index(cur_target) if cur_target in target_options else 0,
                            key=f"target_{cur_acc.account_id}_{vkey}",
                            help="Se il ruolo è 'feeder', il surplus di risorse viene inviato periodicamente a questo villaggio.",
                        )
                        assigned_target = "" if new_target == "(Nessuno)" else new_target

                    st.divider()

                    # --- SEZIONE 2: PRIORITÀ EDIFICI & ESERCITO (DIVISE A 2 COLONNE CHIARE E LARGHE) ---
                    p_col1, p_col2 = st.columns([1, 1])

                    with p_col1:
                        st.markdown("##### 🏗️ Coda Prioritaria Edifici")
                        st.caption(
                            "Coda ordinata: il bot completa ogni step (costruzione o "
                            "livello target) prima di passare al successivo. "
                            "Livello 0 = fino al massimo consentito."
                        )

                        # Coda per-villaggio in session_state, seedata dalla
                        # config (step dict o chiavi legacy -> target 0).
                        queue_key = f"bqueue_{cur_acc.account_id}_{vkey}"
                        preset_role_key = f"bqpreset_{cur_acc.account_id}_{vkey}"
                        bqtroop_key = f"bqtroop_{cur_acc.account_id}_{vkey}"
                        bqsmithy_key = f"bqsmithy_{cur_acc.account_id}_{vkey}"
                        units_widget_key = f"units_{cur_acc.account_id}_{vkey}"

                        def _queue_from(items) -> list[dict]:
                            """Normalizza step (dict o chiave legacy) in righe coda."""
                            seeded: list[dict] = []
                            for _it in items or []:
                                if isinstance(_it, dict):
                                    _k = str(_it.get("key") or _it.get("gid") or "")
                                    _t = int(_it.get("target_level") or 0)
                                else:
                                    _k = focus_alias_map.get(str(_it), str(_it))
                                    _t = 0
                                if _k:
                                    seeded.append({
                                        "key": _k, "target_level": _t,
                                        "_uid": uuid.uuid4().hex[:8],
                                    })
                            return seeded

                        if queue_key not in st.session_state:
                            st.session_state[queue_key] = _queue_from(
                                old_cfg.building_focus
                            )

                        # Smart Role-Based Presets: al cambio di ruolo il
                        # template completo (coda edifici + preset truppe +
                        # automazione Fucina + unita' consentite) viene
                        # caricato — UNA volta per selezione, poi le
                        # modifiche manuali non vengono sovrascritte.
                        # Il pulsante sotto forza il ricaricamento.
                        def _apply_role_preset(role: str) -> bool:
                            """Inietta il template completo del ruolo."""
                            _steps = preset_for_role(role)
                            if not _steps:
                                return False
                            st.session_state[queue_key] = _queue_from(_steps)
                            st.session_state[bqtroop_key] = (
                                troop_preset_for_role(role)
                            )
                            st.session_state[bqsmithy_key] = (
                                smithy_preset_for_role(role)
                            )
                            # Sblocca le unita' del ruolo: richiede la
                            # tribu' (da config account o rilevata dalle
                            # unita' gia' consentite).
                            _tribe = (
                                cur_acc.tribe
                                or detect_tribe_from_labels(
                                    old_cfg.allowed_units
                                )
                            )
                            _slots = allowed_unit_slots_for_role(role)
                            if _tribe and _slots:
                                st.session_state[units_widget_key] = (
                                    resolve_troop_labels(_slots, _tribe)
                                )
                            return True

                        _preset_applied = False
                        if (
                            new_role != (old_cfg.role or "standard")
                            and st.session_state.get(preset_role_key) != new_role
                        ):
                            st.session_state[preset_role_key] = new_role
                            _preset_applied = _apply_role_preset(new_role)

                        _ph1, _ph2 = st.columns([3.4, 1.1])
                        with _ph2:
                            if st.form_submit_button(
                                "🔄 Applica Preset",
                                key=f"fqprst_{cur_acc.account_id}_{vkey}",
                                help=(
                                    "Sostituisce coda, preset truppe, Fucina "
                                    "e unita' consentite col template "
                                    "completo del ruolo selezionato"
                                ),
                            ):
                                _preset_applied = _apply_role_preset(new_role)
                                if not _preset_applied:
                                    st.caption("Nessun preset per questo ruolo.")
                        queue: list[dict] = st.session_state[queue_key]
                        if _preset_applied:
                            st.caption(
                                f"📋 Preset '{new_role}' applicato "
                                f"({len(queue)} step + truppe/Fucina) — "
                                "modificabile liberamente."
                            )

                        # Righe ordinate: numero, edificio, livello target,
                        # controlli sposta su/giu' e rimuovi. Le chiavi widget
                        # usano _uid (stabile per step): riordino/rimozione non
                        # sposta i valori fra gli step.
                        for _i, _step in enumerate(queue):
                            if "_uid" not in _step:
                                _step["_uid"] = uuid.uuid4().hex[:8]
                            _uid = _step["_uid"]
                            _cols = st.columns([0.6, 4.0, 1.4, 0.7, 0.7, 0.7])
                            _cols[0].markdown(f"**{_i + 1}.**")
                            _cols[1].markdown(
                                building_focus_labels.get(_step["key"], _step["key"])
                            )
                            _step["target_level"] = _cols[2].number_input(
                                "Liv.", min_value=0, max_value=20,
                                value=int(_step.get("target_level") or 0),
                                key=f"fqtgt_{cur_acc.account_id}_{vkey}_{_uid}",
                                label_visibility="collapsed",
                                help="Livello target 1-20 (0 = fino al massimo consentito)",
                            )
                            if _cols[3].form_submit_button(
                                "↑", key=f"fqup_{cur_acc.account_id}_{vkey}_{_uid}"
                            ) and _i > 0:
                                queue[_i - 1], queue[_i] = queue[_i], queue[_i - 1]
                            if _cols[4].form_submit_button(
                                "↓", key=f"fqdn_{cur_acc.account_id}_{vkey}_{_uid}"
                            ) and _i < len(queue) - 1:
                                queue[_i + 1], queue[_i] = queue[_i], queue[_i + 1]
                            if _cols[5].form_submit_button(
                                "✕", key=f"fqrm_{cur_acc.account_id}_{vkey}_{_uid}"
                            ):
                                queue.pop(_i)

                        # Aggiungi un nuovo step alla coda.
                        _ac1, _ac2, _ac3 = st.columns([3.5, 1.2, 0.8])
                        with _ac1:
                            _add_key = st.selectbox(
                                "Aggiungi edificio:",
                                options=canonical_focus_keys,
                                format_func=lambda k: building_focus_labels.get(k, k),
                                key=f"fqadd_{cur_acc.account_id}_{vkey}",
                                label_visibility="visible",
                            )
                        with _ac2:
                            _add_tgt = st.number_input(
                                "Liv. target:", min_value=0, max_value=20, value=0,
                                key=f"fqlvl_{cur_acc.account_id}_{vkey}",
                                help="Livello target 1-20 (0 = fino al massimo consentito)",
                            )
                        with _ac3:
                            st.markdown("&nbsp;")
                            if st.form_submit_button(
                                "➕", key=f"fqbtn_{cur_acc.account_id}_{vkey}",
                                help="Aggiungi lo step alla coda",
                            ):
                                queue.append(
                                    {"key": _add_key,
                                     "target_level": int(_add_tgt),
                                     "_uid": uuid.uuid4().hex[:8]}
                                )

                        # Persistenza: lista ordinata di step strutturati
                        # {"key": ..., "target_level": int, "priority": N}
                        # in ordine di coda (building_queue_plan, v2.0).
                        parsed_focus = [
                            {"key": s["key"],
                             "target_level": int(s["target_level"]),
                             "priority": _pi + 1}
                            for _pi, s in enumerate(queue)
                        ]

                    with p_col2:
                        st.markdown("##### ⚔️ Unità Addestrabili Consentite")
                        st.caption("Filtro truppe per questo villaggio: l'ordine stabilisce la priorità di addestramento.")

                        # Opzioni truppe = catalogo unità Travian + eventuali selezioni pregresse
                        unit_options = list(dict.fromkeys(list(unit_display_labels.keys()) + list(STATIC_TROOP_LABELS.values())))
                        for _u in old_cfg.allowed_units:
                            if _u not in unit_options:
                                unit_options.append(_u)

                        # 'default' solo se la chiave non e' gia' in
                        # session_state (es. preset appena applicato):
                        # altrimenti Streamlit segnala il doppio valore.
                        _units_kwargs: dict = {}
                        if units_widget_key not in st.session_state:
                            _units_kwargs["default"] = [
                                u for u in old_cfg.allowed_units
                                if u in unit_options
                            ]

                        parsed_units = st.multiselect(
                            "Unità Consentite per questo Villaggio:",
                            options=unit_options,
                            format_func=lambda u: unit_display_labels.get(u, u),
                            key=units_widget_key,
                            help="Filtra rigorosamente quali unità questo villaggio può addestrare. Lascia vuoto per consentire tutte le unità.",
                            **_units_kwargs,
                        )

                    # --- SOTTOSEZIONE: TARGET TRUPPE (SMART TROOP REPLENISHMENT) ---
                    target_units = list(dict.fromkeys(
                        list(parsed_units) + list(old_cfg.troop_targets.keys())
                    ))
                    troop_targets: dict[str, int] = {}
                    if target_units:
                        st.markdown("###### 🎯 Obiettivi Numerici (Smart Troop Replenishment)")
                        st.caption("Target di truppe stabili (presenti + in coda): il bot colmerà automaticamente il deficit post-battaglia.")
                        t_cols = st.columns(min(4, max(1, len(target_units))))
                        for t_idx, t_unit in enumerate(target_units):
                            with t_cols[t_idx % len(t_cols)]:
                                display_name = unit_short_names.get(t_unit, t_unit)
                                troop_targets[t_unit] = st.number_input(
                                    f"Target {display_name}:",
                                    min_value=0,
                                    max_value=100000,
                                    value=int(old_cfg.troop_targets.get(t_unit, 0) or 0),
                                    step=10,
                                    key=f"ttgt_{cur_acc.account_id}_{vkey}_{t_idx}",
                                    help=f"Quantità minima da mantenere per {t_unit}. 0 = non riaddestrare.",
                                )
                    troop_targets = {
                        u: q for u, q in troop_targets.items() if int(q) > 0
                    }

                    st.divider()

                    # --- SEZIONE 3: PARAMETRI TECNICI & COORDINATE (4 COLONNE SPAZIOSE E DEDICATE) ---
                    st.markdown("##### 📍 Coordinate Mappa & Parametri Tecnici")
                    c_col1, c_col2, c_col3, c_col4 = st.columns(4)
                    with c_col1:
                        parsed_x = st.number_input(
                            "Coordinata X:",
                            min_value=-500,
                            max_value=500,
                            value=int(old_cfg.x if old_cfg.x is not None else 0),
                            step=1,
                            key=f"coord_x_{cur_acc.account_id}_{vkey}",
                            help="Coordinata X sulla mappa del villaggio (utilizzata per rotte mercato e distanze).",
                        )
                    with c_col2:
                        parsed_y = st.number_input(
                            "Coordinata Y:",
                            min_value=-500,
                            max_value=500,
                            value=int(old_cfg.y if old_cfg.y is not None else 0),
                            step=1,
                            key=f"coord_y_{cur_acc.account_id}_{vkey}",
                            help="Coordinata Y sulla mappa del villaggio (utilizzata per rotte mercato e distanze).",
                        )
                    with c_col3:
                        parsed_market_slot = st.number_input(
                            "Slot Mercato (dorf2):",
                            min_value=0,
                            max_value=40,
                            value=int(old_cfg.market_slot or 0),
                            step=1,
                            key=f"market_slot_{cur_acc.account_id}_{vkey}",
                            help="Slot dorf2 del Mercato (19-40). 0 = rilevamento automatico via gid=17.",
                        )
                    with c_col4:
                        parsed_net_crop = st.number_input(
                            "Min Net Crop/h:",
                            min_value=0.0,
                            max_value=100000.0,
                            value=float(old_cfg.min_net_crop_threshold or 0.0),
                            step=5.0,
                            key=f"netcrop_{cur_acc.account_id}_{vkey}",
                            help="Grain Safety Net: margine netto grano minimo (unità/ora). "
                                 "Sotto soglia sono bloccati upgrade non-grano e addestramenti. "
                                 "0 = eredita la soglia globale dell'account.",
                        )

                    # Se X e Y sono entrambi 0 e l'utente non li ha impostati, preserva None se era None
                    final_x = parsed_x if (parsed_x != 0 or old_cfg.x == 0) else old_cfg.x
                    final_y = parsed_y if (parsed_y != 0 or old_cfg.y == 0) else old_cfg.y

                    # Flag preset (spec: 'template + overwrite'): la coda
                    # corrente e' confrontata col template del ruolo; ogni
                    # deviazione manuale marca custom_modified=true.
                    _preset_sig = {
                        s["key"]: int(s["target_level"])
                        for s in preset_for_role(new_role)
                    }
                    _queue_sig = {
                        s["key"]: int(s["target_level"]) for s in queue
                    }
                    custom_mod = (
                        not _preset_applied
                        and _preset_sig != _queue_sig
                    )

                    updated_villages[vname] = VillageConfig(
                        name=vname,
                        role=new_role,
                        target_village=assigned_target,
                        allowed_units=parsed_units,
                        priority_units=list(parsed_units),
                        building_focus=parsed_focus,
                        building_queue_plan=parsed_focus,
                        is_bootstrap=old_cfg.is_bootstrap,
                        farm_enabled=old_cfg.farm_enabled,
                        farm_unit=old_cfg.farm_unit,
                        defense_enabled=old_cfg.defense_enabled,
                        market_slot=int(parsed_market_slot),
                        min_net_crop_threshold=float(parsed_net_crop or 0.0),
                        x=int(final_x) if final_x is not None else None,
                        y=int(final_y) if final_y is not None else None,
                        troop_targets=troop_targets,
                        preset_applied=bool(
                            old_cfg.preset_applied or _preset_applied
                        ),
                        custom_modified=custom_mod,
                        troop_replenishment_preset=dict(
                            st.session_state.get(
                                bqtroop_key,
                                old_cfg.troop_replenishment_preset,
                            ) or {}
                        ),
                        smithy_auto_upgrade=dict(
                            st.session_state.get(
                                bqsmithy_key,
                                old_cfg.smithy_auto_upgrade,
                            ) or {}
                        ),
                    )

            save_submitted = st.form_submit_button("💾 Salva Ruoli & Strategia (Applicazione a Caldo)", type="primary")
            if save_submitted:
                cur_acc.strategy_profile = new_strat
                cur_acc.tribe = (
                    "" if new_tribe == "auto" else new_tribe
                )
                cur_acc.villages = updated_villages
                # Founder: il villaggio pioniere diventa il fondatore
                # designato dell'espansione (scansione cropper attiva).
                for _fc in updated_villages.values():
                    if _fc.role == "early_cropper_founder":
                        if not cur_acc.expansion.founder_village:
                            cur_acc.expansion.founder_village = _fc.name
                        cur_acc.expansion.cropper_scan = True
                        break
                save_accounts(accounts)
                st.success("Configurazione villaggi e strategia salvata con successo! L'agente leggerà le modifiche al prossimo ciclo.")
                time.sleep(1)
                st.rerun()


# 2.2 PAGINA WAR ROOM & AI SCOUT BRAIN
if page == "War Room":
    ui.page_header(
        "War Room",
        "Intelligence radar, potenza militare, target saccheggio e raccomandazioni Gemini AI.",
    )

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        w_acc = _selected_account()
        st.caption(f"Account attivo: **{w_acc.account_id}** — {w_acc.server_url}")

        scout_data = get_scout_brain_data(w_acc.account_id)
        targets_list = scout_data.get("targets", [])
        ai_recs = scout_data.get("last_ai_recommendations", [])
        last_updated = scout_data.get("updated_at", 0)

        # 1. Metriche Global Power
        st.markdown("#### 👑 Valutazione Globale Potenza Imperiale (Multi-Villaggio)")
        latest_snaps = get_latest_village_snapshots(w_acc.account_id)

        tot_wood = sum(s.get("resources", {}).get("wood", 0) for s in latest_snaps.values())
        tot_clay = sum(s.get("resources", {}).get("clay", 0) for s in latest_snaps.values())
        tot_iron = sum(s.get("resources", {}).get("iron", 0) for s in latest_snaps.values())
        tot_crop = sum(s.get("resources", {}).get("crop", 0) for s in latest_snaps.values())
        tot_wh = sum(s.get("resources", {}).get("warehouse_cap", 0) for s in latest_snaps.values()) or 1
        tot_gr = sum(s.get("resources", {}).get("granary_cap", 0) for s in latest_snaps.values()) or 1

        tot_res = tot_wood + tot_clay + tot_iron + tot_crop
        tot_cap = (tot_wh * 3) + tot_gr
        sat_pct = round((tot_res / max(1, tot_cap)) * 100.0, 1)

        g1, g2, g3, g4 = st.columns(4)
        g1.metric("🏰 Villaggi Monitorati", len(latest_snaps) or len(w_acc.villages))
        g2.metric("📦 Scorte Totali Impero", f"{tot_res:,}")
        g3.metric("📊 Saturazione Magazzini", f"{sat_pct}%")
        g4.metric("🎯 Target Radar Noti", len(targets_list))

        st.divider()

        # 2. Raccomandazioni Strategiche Gemini AI
        st.markdown("#### 🤖 AI Scout Advisor (Raccomandazioni Strategiche)")
        if ai_recs:
            st.info(f"Ultima ondata generata dall'AI Scout ({len(ai_recs)} target calcolati):")
            rec_cols = st.columns(min(3, len(ai_recs)))
            for idx, rec in enumerate(ai_recs):
                col_i = rec_cols[idx % len(rec_cols)]
                with col_i:
                    with st.container(border=True):
                        st.markdown(f"**🎯 #{rec.get('priority', idx+1)} {rec.get('target_name', 'Target')}**")
                        st.caption(f"Coordinate: `({rec.get('x')}|{rec.get('y')})`")
                        troops_str = ", ".join(f"{k}: {v}" for k, v in (rec.get("troops") or {}).items())
                        st.write(f"🗡️ **Truppe:** `{troops_str}`")
                        st.write(f"💡 **Motivazione:** {rec.get('reasoning', '-')}")
        else:
            st.info("Nessuna raccomandazione recente registrata. Abilita l'AI Scout Brain in 'Farming & Espansione' per generare piani di attacco.")

        st.divider()

        # 3. Radar Mappa & Storico Saccheggi
        st.markdown("#### 📡 Radar di Mappa & Target Intelligence")
        if targets_list:
            filter_mode = st.radio("Filtra Target:", ["Tutti i Target", "Solo Attivi (Raidabili)", "Solo Blacklistati"], horizontal=True)
            filtered_tgts = []
            for t in targets_list:
                is_bl = t.get("is_blacklisted", False)
                if filter_mode == "Solo Attivi (Raidabili)" and is_bl:
                    continue
                if filter_mode == "Solo Blacklistati" and not is_bl:
                    continue
                filtered_tgts.append(t)

            _wr_rows = []
            for t in filtered_tgts:
                is_bl = t.get("is_blacklisted", False)
                icon = "⛔" if is_bl else ("🏝️" if t.get("is_oasis") else "🏕️")
                loot = t.get("last_loot_ratio", 1.0)
                _wr_rows.append(
                    [
                        f"{icon} <b>{ui.esc(t.get('village_name', ''))}</b>",
                        f"<span class='mono'>({t.get('x')}|{t.get('y')})</span>",
                        f"<span class='dim'>{ui.esc(t.get('player_name') or '—')}"
                        f" · pop {t.get('population', 0)}</span>",
                        f"{t.get('distance', 0):.1f}",
                        f"{int(loot * 100)}%",
                        ui.pill("blacklisted", "danger")
                        if is_bl
                        else ui.pill("valid", "success"),
                        f"<span class='dim'>{ui.esc(t.get('notes', ''))}</span>"
                        if is_bl and t.get("notes")
                        else "",
                    ]
                )
            ui.render_table(
                ["Target", "Coord", "Giocatore", "Dist", "Bottino", "Stato", "Note"],
                _wr_rows,
            )
        else:
            st.info("Nessun tile scansionato finora. L'agente esplorerà la mappa al prossimo ciclo con il sensore mappa attivo.")


# 2.5 PAGINA FARMING & EROE
if page == "Farming & Eroe":
    ui.page_header(
        "Farming & Eroe",
        "Moduli autonomi: farm list, eroe, difesa e replenishment — salvataggio a caldo su accounts.json.",
    )

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        mod_acc = _selected_account()
        st.caption(f"Account attivo: **{mod_acc.account_id}** — {mod_acc.server_url}")

        # ---------------- STATUS EROE ----------------
        hero_data = get_hero_data(mod_acc.account_id)
        st.markdown("### 🦸 Status & Controllo Eroe")
        h_col1, h_col2, h_col3, h_col4 = st.columns(4)
        with h_col1:
            st.metric("Salute Eroe", f"{hero_data.get('health', '–')}%" if hero_data else "–")
        with h_col2:
            _status_map = {
                "home": "A casa", "adventure": "In avventura",
                "moving": "In marcia", "away": "Fuori villaggio",
                "dead": "Morto/in cura",
            }
            st.metric(
                "Stato",
                _status_map.get(hero_data.get("status", ""), "–") if hero_data else "–",
            )
        with h_col3:
            st.metric("Avventure disponibili", hero_data.get("adventures", "–") if hero_data else "–")
        with h_col4:
            _lvl = hero_data.get("level", 0) if hero_data else 0
            st.metric("Livello", _lvl if _lvl else "–")
        if hero_data:
            _upd = datetime.datetime.fromtimestamp(
                hero_data.get("updated_at", 0)
            ).strftime("%d/%m %H:%M")
            st.caption(
                f"Ultima azione: `{hero_data.get('last_action') or '-'}` — aggiornato {_upd}"
            )
        else:
            st.caption("Nessun dato eroe: attiva il modulo e attendi il primo ciclo dell'agente.")
        st.divider()

        # Villaggi noti da config + snapshot storico
        mod_snapshots = get_latest_village_snapshots(mod_acc.account_id)
        mod_known = list(mod_acc.villages.keys())
        for sv in mod_snapshots.keys():
            if sv not in mod_known:
                mod_known.append(sv)

        unit_choices = [
            "(auto — truppe leggere)",
            "legionario", "imperiani", "cavalieri", "equites",
            "mazzieri", "asce", "paladini", "cavalieri teutonici",
            "falangi", "spadaccini", "fulmini", "druidi", "eduani",
        ]

        with st.form("farming_expansion_form"):
            # ---------------- FARMING ----------------
            st.markdown("### 🛡️ Gestione Automazione Farming (Raid List)")
            farm_enabled_g = st.toggle(
                "Abilita Farming Automatico",
                value=mod_acc.farming.enabled,
                key="farming_toggle",
                help="Invio periodico di raid dalle farm list del Punto di Raduno (gid 16).",
            )

            interval = st.slider(
                "Intervallo tra i raid (minuti)",
                min_value=5, max_value=60,
                value=(
                    int(mod_acc.farming.interval_min_minutes),
                    int(mod_acc.farming.interval_max_minutes),
                ),
                help="Finestra di attesa casuale (jitter anti-bot): ogni ciclo "
                     "viene estratto un valore diverso dentro l'intervallo.",
            )

            f_col1, f_col2 = st.columns(2)
            with f_col1:
                farm_list_name = st.text_input(
                    "Nome Farm List preferita (vuoto = prima disponibile):",
                    value=mod_acc.farming.farm_list,
                    help="Lista nativa Gold Club (tt=99): usata solo come "
                         "fallback quando non ci sono Custom Farm Targets approved.",
                )
            with f_col2:
                cur_unit = mod_acc.farming.unit or unit_choices[0]
                if cur_unit not in unit_choices:
                    unit_choices.append(cur_unit)
                farm_unit_g = st.selectbox(
                    "Unità di saccheggio predefinita:", unit_choices,
                    index=unit_choices.index(cur_unit),
                )

            f_col3, f_col4 = st.columns(2)
            with f_col3:
                farm_wave_sel = st.number_input(
                    "Max target per ondata (Custom Farm):",
                    min_value=1, max_value=10,
                    value=int(mod_acc.farming.targets_per_wave or 3),
                    help="Quanti bersagli 'approved' vengono razziati in ogni "
                         "ciclo di farming per villaggio (raid mirati tt=2).",
                )
            with f_col4:
                farm_autoscan_sel = st.toggle(
                    "Scoperta automatica target (scansione mappa)",
                    value=bool(getattr(mod_acc.farming, "auto_scan", True)),
                    key="farm_autoscan_toggle",
                    help="Scansiona periodicamente la mappa per catalogare oasi "
                         "e villaggi razziabili come 'pending' nella tabella sotto.",
                )

            st.markdown("#### Per-villaggio")
            for idx, vname in enumerate(mod_known):
                vcfg = mod_acc.villages.get(vname, VillageConfig(name=vname))
                with st.expander(f"Villaggio: {vname} (Ruolo: {vcfg.role})"):
                    vc1, vc2, vc3 = st.columns(3)
                    with vc1:
                        st.checkbox(
                            "Usa Farm List automatica",
                            value=vcfg.farm_enabled,
                            key=f"farm_active_{mod_acc.account_id}_{idx}",
                        )
                    with vc2:
                        v_unit = vcfg.farm_unit or unit_choices[0]
                        if v_unit not in unit_choices:
                            unit_choices.append(v_unit)
                        st.selectbox(
                            "Unità di truppa designate", unit_choices,
                            index=unit_choices.index(v_unit),
                            key=f"troop_type_{mod_acc.account_id}_{idx}",
                        )
                    with vc3:
                        st.checkbox(
                            "Reazione anti-attacco (Difesa)",
                            value=getattr(vcfg, "defense_enabled", True),
                            key=f"defense_active_{mod_acc.account_id}_{idx}",
                            help="Se attivo, il villaggio attiva drain risorse "
                                 "e fuga truppe in caso di attacco reale.",
                        )
                    st.number_input(
                        "Unità per raid custom (0 = auto — divide i raider sui target):",
                        min_value=0, max_value=5000,
                        value=int(getattr(vcfg, "farm_unit_qty", 0) or 0),
                        key=f"farm_qty_{mod_acc.account_id}_{idx}",
                        help="Truppe inviate per ogni raid mirato sui Custom "
                             "Farm Targets approvati (tt=2, non Gold Club).",
                    )

            st.divider()

            # ---------------- ESPANSIONE ----------------
            st.markdown("### 🏗️ Espansione Autonoma (Fondazione Nuovi Villaggi)")
            exp_enabled = st.toggle(
                "Abilita Espansione Automatica",
                value=mod_acc.expansion.auto_expand,
                key="expansion_toggle",
                help="Addestra 3 coloni in Residenza/Palazzo e fonda sui tile target.",
            )

            e_col1, e_col2 = st.columns(2)
            with e_col1:
                founder_opts = ["(auto: primo con Residenza/Palazzo)"] + mod_known
                cur_founder = mod_acc.expansion.founder_village or founder_opts[0]
                if cur_founder not in founder_opts:
                    founder_opts.append(cur_founder)
                founder_sel = st.selectbox(
                    "Villaggio Fondatore:", founder_opts,
                    index=founder_opts.index(cur_founder),
                    help="Villaggio con Residenza/Palazzo >= 10 che addestra i coloni.",
                )
            with e_col2:
                crop_min_sel = st.selectbox(
                    "Campi grano minimi sul tile:", [9, 15, 0],
                    index=[9, 15, 0].index(mod_acc.expansion.crop_min)
                    if mod_acc.expansion.crop_min in (9, 15, 0) else 0,
                    format_func=lambda c: {9: "9c (croplog)", 15: "15c (capitale)", 0: "qualunque"}[c],
                )

            s_col1, s_col2, s_col3 = st.columns(3)
            with s_col1:
                exp_slot_input = st.number_input(
                    "Slot Residenza/Palazzo (dorf2):",
                    min_value=0, max_value=40,
                    value=int(mod_acc.expansion.expansion_slot or 0),
                    help="Slot dell'edificio che addestra i coloni (tipico: 26, 31 o 32). 0 = auto-detect.",
                )
            _primary = (
                mod_acc.expansion.target_slots[0]
                if mod_acc.expansion.target_slots else None
            )
            with s_col2:
                target_x_input = st.number_input(
                    "Coordinata X target:", min_value=-400, max_value=400,
                    value=int(_primary.x) if _primary else 0,
                    help="Coordinata X del tile su cui fondare il nuovo villaggio.",
                )
            with s_col3:
                target_y_input = st.number_input(
                    "Coordinata Y target:", min_value=-400, max_value=400,
                    value=int(_primary.y) if _primary else 0,
                    help="Coordinata Y del tile su cui fondare il nuovo villaggio.",
                )

            _extra_slots = mod_acc.expansion.target_slots[1:] \
                if len(mod_acc.expansion.target_slots) > 1 else []
            slots_text = "\n".join(
                f"{t.x},{t.y},{t.note}" for t in _extra_slots
            )
            targets_raw = st.text_area(
                "Tile target aggiuntivi (uno per riga: x,y,nota):",
                value=slots_text,
                height=100,
                help="Es: -15,72,Futuro Croplog 15c — provati in ordine se il tile X/Y sopra e' occupato.",
            )

            c_col1, c_col2 = st.columns(2)
            with c_col1:
                cropper_scan_sel = st.toggle(
                    "Abilita Cropper Finder (scansione mappa 9c/15c)",
                    value=mod_acc.expansion.cropper_scan,
                    key="cropper_scan_toggle",
                    help="Cataloga i tile liberi ad alto rendimento nella scheda 'Espansione & Cropper' per l'approvazione umana.",
                )
            with c_col2:
                scan_radius_sel = st.number_input(
                    "Raggio scansione mappa (campi):",
                    min_value=3, max_value=60,
                    value=int(mod_acc.expansion.scan_radius or 15),
                    help="Distanza massima dal villaggio per la ricerca dei cropper.",
                )

            st.divider()

            # ---------------- AI SCOUT BRAIN & DYNAMIC RAID ----------------
            st.markdown("### 🧠 AI Scout Brain & Raid Dinamico (Google Gemini)")
            scout_enabled = st.toggle(
                "Abilita AI Scout Brain (Raid Dinamico ad Alta Efficienza)",
                value=mod_acc.scout_brain.enabled,
                key="scout_brain_toggle",
                help="Scansiona la mappa, interroga i report di combattimento e calcola le ondate ottimali di truppe tramite Gemini AI (o fallback euristico).",
            )

            s_col1, s_col2 = st.columns(2)
            with s_col1:
                gemini_key_input = st.text_input(
                    "Gemini API Key (lascia vuoto per usare variabile d'ambiente o fallback euristico):",
                    value=mod_acc.scout_brain.gemini_api_key,
                    type="password",
                )
                scout_model_opts = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]
                cur_scout_model = mod_acc.scout_brain.gemini_model if mod_acc.scout_brain.gemini_model in scout_model_opts else scout_model_opts[0]
                scout_model_sel = st.selectbox(
                    "Modello Gemini:",
                    scout_model_opts,
                    index=scout_model_opts.index(cur_scout_model),
                )
            with s_col2:
                scout_radius_sel = st.slider(
                    "Raggio Scansione Radar (campi)",
                    min_value=3, max_value=20,
                    value=int(mod_acc.scout_brain.scan_radius),
                )
                max_travel_sel = st.slider(
                    "Tempo Massimo di Marcia (minuti)",
                    min_value=5, max_value=90,
                    value=int(mod_acc.scout_brain.max_travel_time_minutes),
                )

            s_col3, s_col4 = st.columns(2)
            with s_col3:
                min_loot_sel = st.slider(
                    "Rapporto Minimo Bottino Richiesto (%)",
                    min_value=10, max_value=90,
                    value=int(mod_acc.scout_brain.min_loot_ratio * 100),
                ) / 100.0
            with s_col4:
                bl_hours_sel = st.number_input(
                    "Ore di Blacklist in caso di perdite / truppe nemiche:",
                    min_value=1.0, max_value=72.0,
                    value=float(mod_acc.scout_brain.blacklist_hours),
                    step=1.0,
                )

            st.divider()

            # ---------------- EROE ----------------
            st.markdown("### 🦸 Modulo Eroe Autonomo")
            hero_enabled = st.toggle(
                "Abilita Modulo Eroe",
                value=mod_acc.hero.enabled,
                key="hero_toggle",
                help="Monitora salute, applica unguenti e spedisce in avventura.",
            )
            hero_adv = st.toggle(
                "Abilita Spedizione Automatica in Avventura",
                value=mod_acc.hero.auto_adventures,
                key="hero_auto_adv",
                help="Invia l'eroe alla prima avventura disponibile quando e' a casa.",
            )
            hero_tactical = st.toggle(
                "Consenti uso tattico dell'Eroe in combattimento",
                value=getattr(mod_acc.hero, "tactical_hero_enabled", True),
                key="hero_tactical",
                help=(
                    "Smart Hero Combat Evaluator: aggrega l'eroe ai raid ad "
                    "alto valore (oasi con belve, wave hammer dei chiefing, "
                    "target ad alto bottino). Mai sui micro-raid ripetitivi."
                ),
            )
            hero_combat_hp_sel = st.slider(
                "Salute minima per combattimento %",
                min_value=10, max_value=100,
                value=int(getattr(mod_acc.hero, "min_combat_health", 65)),
                help="Sotto questa soglia l'eroe resta a casa a curarsi.",
            )
            hr_col1, hr_col2, hr_col3 = st.columns(3)
            with hr_col1:
                hero_heal_sel = st.slider(
                    "Usa unguenti sotto salute %",
                    min_value=10, max_value=90,
                    value=int(mod_acc.hero.heal_threshold),
                )
            with hr_col2:
                hero_min_hp_sel = st.slider(
                    "Salute minima per avventura %",
                    min_value=10, max_value=90,
                    value=int(mod_acc.hero.min_health_to_adventure),
                )
            with hr_col3:
                attr_opts = ["auto", "resources", "strength", "off", "def"]
                cur_attr = (
                    mod_acc.hero.attribute_focus
                    if mod_acc.hero.attribute_focus in attr_opts else "auto"
                )
                hero_attr_sel = st.selectbox(
                    "Focus attributi (level-up):",
                    attr_opts,
                    index=attr_opts.index(cur_attr),
                    format_func=lambda a: {
                        "auto": "Auto (dal profilo account)",
                        "resources": "Risorse",
                        "strength": "Forza",
                        "off": "Bonus attacco",
                        "def": "Bonus difesa",
                    }[a],
                )

            st.divider()

            # ---------------- DIFESA (Reazione agli attacchi) ----------------
            # Guardia per oggetti Settings datati senza il campo defense
            if not hasattr(mod_acc, "defense"):
                from travian_agent.config import DefenseConfig
                mod_acc.defense = DefenseConfig()
            st.markdown("### 🚨 Difesa Intelligente (Reazione agli Attacchi)")
            defense_enabled = st.toggle(
                "Abilita Reazione Automatica agli Attacchi",
                value=mod_acc.defense.enabled,
                key="defense_toggle",
                help="Monitora la Piazza d'Armi: sotto attacco reale svuota i "
                     "magazzini (drain) ed evacua le truppe (dodge).",
            )
            d_col1, d_col2, d_col3 = st.columns(3)
            with d_col1:
                defense_panic = st.slider(
                    "Soglia panico (sec all'impatto)",
                    min_value=30, max_value=900, step=30,
                    value=int(mod_acc.defense.panic_threshold_seconds),
                    help="Sotto questo timer un attacco reale attiva "
                         "drain + fuga. Default 180s.",
                )
                defense_fake = st.number_input(
                    "Truppe max per classificare fake",
                    min_value=1, max_value=50,
                    value=int(mod_acc.defense.fake_troop_threshold),
                    help="Movimenti con <= N truppe visibili vengono "
                         "ignorati (es. esploratore singolo).",
                )
            with d_col2:
                defense_margin = st.slider(
                    "Margine fuga (sec prima dell'impatto)",
                    min_value=5, max_value=300, step=5,
                    value=int(mod_acc.defense.dodge_margin_seconds),
                )
                defense_recall = st.slider(
                    "Rientro dopo impatto (sec)",
                    min_value=30, max_value=900, step=30,
                    value=int(mod_acc.defense.recall_delay_seconds),
                )
                defense_min_troops = st.number_input(
                    "Truppe minime per la fuga",
                    min_value=0, max_value=1000,
                    value=int(mod_acc.defense.min_dodge_troops),
                    help="Sotto questo totale la fuga non vale la pena.",
                )
            with d_col3:
                defense_dodge = st.text_input(
                    "Destinazione fuga (nome o x|y)",
                    value=mod_acc.defense.dodge_target,
                    key="defense_dodge_target",
                    help="Vuoto = villaggio proprio più vicino. "
                         "Esempi: 'Hipponion' oppure '-15|72'.",
                )
                defense_drain_mkt = st.toggle(
                    "Spedisci surplus residuo via mercato",
                    value=mod_acc.defense.drain_via_market,
                    key="defense_drain_mkt",
                    help="Dopo coda costruzioni/addestramento, invia il "
                         "residuo al villaggio indicato sotto.",
                )
                defense_drain_target = st.text_input(
                    "Villaggio sicuro per drain mercato",
                    value=mod_acc.defense.drain_target_village,
                    key="defense_drain_target",
                )

            st.divider()

            # ------------- SMART TROOP REPLENISHMENT (Rimpiazzo Perdite) -------------
            if not hasattr(mod_acc, "replenishment"):
                from travian_agent.config import ReplenishmentConfig
                mod_acc.replenishment = ReplenishmentConfig()
            st.markdown("### 🔄 Smart Troop Replenishment (Rimpiazzo Perdite)")
            repl_enabled = st.toggle(
                "Abilita Rimpiazzo Intelligente delle Perdite",
                value=mod_acc.replenishment.enabled,
                key="replenish_toggle",
                help="Dopo ogni report di combattimento riaddestra le unita' "
                     "perse fino ai Target Truppe configurati per villaggio "
                     "(deficit = target - attuali - in coda).",
            )
            repl_autoblock = st.toggle(
                "Soglia di Blocco Dinamico (anti-suicidio)",
                value=mod_acc.replenishment.auto_block,
                key="replenish_autoblock",
                help="Blocca target e congela il rimpiazzo delle unita' "
                     "offensive dopo fallimenti ripetuti contro difese "
                     "pesanti (es. muri di Pretoriani).",
            )
            rp_col1, rp_col2, rp_col3 = st.columns(3)
            with rp_col1:
                repl_streak = st.number_input(
                    "Fallimenti consecutivi prima del blocco:",
                    min_value=1, max_value=10,
                    value=int(mod_acc.replenishment.fail_streak_threshold),
                    help="Dopo N raid falliti sullo stesso target: stop "
                         "attacchi su quel villaggio e congelamento unita'.",
                )
                repl_siege = st.number_input(
                    "Ondata minima per 'villaggio sotto assedio':",
                    min_value=1, max_value=20,
                    value=int(mod_acc.replenishment.siege_waves),
                    help="Con >= N attacchi in arrivo il villaggio viene "
                         "congelato: niente rimpiazzo ne' rinforzi.",
                )
            with rp_col2:
                repl_crop_res = st.slider(
                    "Riserva grano (% granaio sempre preservata)",
                    min_value=0, max_value=80,
                    value=int(mod_acc.replenishment.crop_reserve_pct * 100),
                    help="Il rimpiazzo non usa grano oltre questa soglia: "
                         "stabilita' alimentare del villaggio produttore.",
                ) / 100.0
            with rp_col3:
                repl_skip_build = st.toggle(
                    "Cedi priorita' alle costruzioni",
                    value=mod_acc.replenishment.skip_while_building,
                    key="replenish_skip_build",
                    help="Se una coda di sviluppo e' attiva, il rimpiazzo "
                         "attende il ciclo successivo.",
                )

            mod_saved = st.form_submit_button(
                "💾 Salva Configurazione Moduli", type="primary"
            )

        if mod_saved:
            # Farming globale
            mod_acc.farming.enabled = farm_enabled_g
            mod_acc.farming.interval_min_minutes = float(min(interval))
            mod_acc.farming.interval_max_minutes = float(max(interval))
            mod_acc.farming.farm_list = farm_list_name.strip()
            mod_acc.farming.unit = (
                "" if farm_unit_g == unit_choices[0] else farm_unit_g
            )
            mod_acc.farming.targets_per_wave = int(farm_wave_sel)
            mod_acc.farming.auto_scan = bool(farm_autoscan_sel)

            # Farming per-villaggio
            for idx, vname in enumerate(mod_known):
                vcfg = mod_acc.villages.get(vname)
                if vcfg is None:
                    vcfg = VillageConfig(name=vname)
                vcfg.farm_enabled = bool(
                    st.session_state[f"farm_active_{mod_acc.account_id}_{idx}"]
                )
                vcfg.defense_enabled = bool(
                    st.session_state.get(
                        f"defense_active_{mod_acc.account_id}_{idx}",
                        getattr(vcfg, "defense_enabled", True),
                    )
                )
                sel_unit = st.session_state[f"troop_type_{mod_acc.account_id}_{idx}"]
                vcfg.farm_unit = "" if sel_unit == unit_choices[0] else sel_unit
                vcfg.farm_unit_qty = int(
                    st.session_state.get(
                        f"farm_qty_{mod_acc.account_id}_{idx}",
                        getattr(vcfg, "farm_unit_qty", 0),
                    )
                )
                mod_acc.villages[vname] = vcfg

            # Espansione
            mod_acc.expansion.auto_expand = exp_enabled
            mod_acc.expansion.founder_village = (
                "" if founder_sel.startswith("(auto") else founder_sel
            )
            mod_acc.expansion.expansion_slot = int(exp_slot_input)
            mod_acc.expansion.crop_min = int(crop_min_sel)
            mod_acc.expansion.cropper_scan = bool(cropper_scan_sel)
            mod_acc.expansion.scan_radius = int(scan_radius_sel)
            parsed_targets: list[ExpansionTarget] = []
            # Target primario dai campi X/Y dedicati
            if int(target_x_input) or int(target_y_input):
                parsed_targets.append(
                    ExpansionTarget(
                        x=int(target_x_input),
                        y=int(target_y_input),
                        note="target primario",
                    )
                )
            for line in targets_raw.splitlines():
                parts = [p.strip() for p in line.replace("|", ",").split(",")]
                if len(parts) >= 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
                    tx, ty = int(parts[0]), int(parts[1])
                    if (tx, ty) in {(t.x, t.y) for t in parsed_targets}:
                        continue
                    parsed_targets.append(
                        ExpansionTarget(
                            x=tx,
                            y=ty,
                            note=parts[2] if len(parts) > 2 else "",
                        )
                    )
            mod_acc.expansion.target_slots = parsed_targets

            # Mirror runtime per l'agente (spec: state/expansion_config.json)
            try:
                _exp_cfg_path = BASE_DIR / "state" / "expansion_config.json"
                _exp_cfg_path.parent.mkdir(parents=True, exist_ok=True)
                _exp_cfg: dict = {}
                if _exp_cfg_path.exists():
                    _exp_cfg = json.loads(
                        _exp_cfg_path.read_text(encoding="utf-8")
                    )
                    if not isinstance(_exp_cfg, dict):
                        _exp_cfg = {}
                _primary_t = parsed_targets[0] if parsed_targets else None
                _exp_cfg[mod_acc.account_id] = {
                    "auto_expansion_enabled": bool(exp_enabled),
                    "source_village": mod_acc.expansion.founder_village,
                    "expansion_slot": int(exp_slot_input),
                    "target_coordinates": {
                        "x": _primary_t.x if _primary_t else 0,
                        "y": _primary_t.y if _primary_t else 0,
                    },
                }
                _exp_cfg_path.write_text(
                    json.dumps(_exp_cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

            # AI Scout Brain
            mod_acc.scout_brain.enabled = scout_enabled
            mod_acc.scout_brain.gemini_api_key = gemini_key_input.strip()
            mod_acc.scout_brain.gemini_model = scout_model_sel
            mod_acc.scout_brain.scan_radius = scout_radius_sel
            mod_acc.scout_brain.max_travel_time_minutes = max_travel_sel
            mod_acc.scout_brain.min_loot_ratio = min_loot_sel
            mod_acc.scout_brain.blacklist_hours = bl_hours_sel

            # Eroe
            mod_acc.hero.enabled = hero_enabled
            mod_acc.hero.auto_adventures = hero_adv
            mod_acc.hero.heal_threshold = int(hero_heal_sel)
            mod_acc.hero.min_health_to_adventure = int(hero_min_hp_sel)
            mod_acc.hero.attribute_focus = hero_attr_sel
            mod_acc.hero.tactical_hero_enabled = hero_tactical
            mod_acc.hero.min_combat_health = int(hero_combat_hp_sel)

            # Difesa
            mod_acc.defense.enabled = defense_enabled
            mod_acc.defense.panic_threshold_seconds = int(defense_panic)
            mod_acc.defense.fake_troop_threshold = int(defense_fake)
            mod_acc.defense.dodge_margin_seconds = int(defense_margin)
            mod_acc.defense.recall_delay_seconds = int(defense_recall)
            mod_acc.defense.min_dodge_troops = int(defense_min_troops)
            mod_acc.defense.dodge_target = defense_dodge.strip()
            mod_acc.defense.drain_via_market = bool(defense_drain_mkt)
            mod_acc.defense.drain_target_village = defense_drain_target.strip()

            # Smart Troop Replenishment
            mod_acc.replenishment.enabled = bool(repl_enabled)
            mod_acc.replenishment.auto_block = bool(repl_autoblock)
            mod_acc.replenishment.fail_streak_threshold = int(repl_streak)
            mod_acc.replenishment.siege_waves = int(repl_siege)
            mod_acc.replenishment.crop_reserve_pct = float(repl_crop_res)
            mod_acc.replenishment.skip_while_building = bool(repl_skip_build)

            save_accounts(accounts)
            st.success(
                "Parametri di farming, espansione, AI Scout, eroe, difesa e "
                "replenishment aggiornati e salvati su "
                "accounts.json! L'agente li applicherà al prossimo ciclo."
            )
            time.sleep(1)
            st.rerun()

        # ---------------- CUSTOM FARM TARGETS (Human-in-the-Loop) ----------------
        st.divider()
        st.subheader("🎯 Custom Farm Targets (alternativa al Gold Club)")
        st.caption(
            "Bersagli di saccheggio scoperti dalla scansione mappa (oasi e "
            "villaggi) o inseriti a mano. L'agente invia raid mirati (tt=2) "
            "SOLO sui target `approved` — nessuna farm list nativa richiesta. "
            "Perdite o difensori nei report -> auto-blacklist anti-suicidio."
        )
        _fstore = FarmTargetStore(mod_acc.account_id)
        _ft_all = _fstore.list_targets()
        _fc = {
            s: sum(1 for t in _ft_all if t.status == s)
            for s in (
                FARM_STATUS_PENDING, FARM_STATUS_APPROVED,
                FARM_STATUS_REJECTED, FARM_STATUS_BLACKLISTED,
            )
        }
        fm1, fm2, fm3, fm4 = st.columns(4)
        fm1.metric("🕐 Pending", _fc[FARM_STATUS_PENDING])
        fm2.metric("✅ Approved", _fc[FARM_STATUS_APPROVED])
        fm3.metric("⛔ Blacklisted", _fc[FARM_STATUS_BLACKLISTED])
        fm4.metric("❌ Scartati", _fc[FARM_STATUS_REJECTED])

        ff1, ff2, ff3 = st.columns(3)
        with ff1:
            _ft_status = st.selectbox(
                "Filtro stato:",
                ["Tutti", FARM_STATUS_PENDING, FARM_STATUS_APPROVED,
                 FARM_STATUS_REJECTED, FARM_STATUS_BLACKLISTED],
                key="farm_f_status",
            )
        with ff2:
            _ft_type = st.selectbox(
                "Filtro tipo:", ["Tutti", "Oasi", "Villaggi"],
                key="farm_f_type",
            )
        with ff3:
            _ft_maxdist = st.number_input(
                "Distanza max (campi, 0 = tutte):",
                min_value=0.0, max_value=400.0, value=0.0, step=5.0,
                key="farm_f_dist",
            )

        _ft_filtered = [
            t for t in _ft_all
            if (_ft_status == "Tutti" or t.status == _ft_status)
            and (_ft_type == "Tutti"
                 or (_ft_type == "Oasi" and t.is_oasis)
                 or (_ft_type == "Villaggi" and not t.is_oasis))
            and (not _ft_maxdist or t.distance <= _ft_maxdist or t.distance == 0)
        ]

        if not _ft_filtered:
            st.info(
                "Nessun target in catalogo. La scansione mappa dell'agente "
                "(farming.auto_scan o AI Scout) popola questa lista; oppure "
                "inserisci le coordinate a mano qui sotto."
            )
        else:
            _farm_tone = {
                FARM_STATUS_PENDING: ("pending", "warning"),
                FARM_STATUS_APPROVED: ("approved", "success"),
                FARM_STATUS_REJECTED: ("rejected", "neutral"),
                FARM_STATUS_BLACKLISTED: ("blacklisted", "danger"),
            }
            _ft_rows = []
            _ft_label = {}
            for _t in _ft_filtered:
                _lbl, _tone = _farm_tone.get(_t.status, (_t.status, "neutral"))
                _ft_label[_t.id] = f"({_t.x}|{_t.y}) {_t.name or ''}"
                _ft_rows.append(
                    [
                        f"<span class='mono'>({_t.x}|{_t.y})</span>",
                        ui.esc(_t.name or "—") + (" ✋" if _t.manual else ""),
                        "🌿 Oasi" if _t.is_oasis else "🏘️ Vill.",
                        f"{_t.distance:.0f}" if _t.distance else "—",
                        str(_t.population or "—"),
                        str(_t.estimated_loot or "—"),
                        f"{_t.last_loot_ratio:.0%}",
                        str(_t.losses_recorded or "—"),
                        ui.pill(_lbl, _tone),
                    ]
                )
            ui.render_table(
                ["Coord", "Nome", "Tipo", "Dist", "Pop", "Loot st.",
                 "Loot%", "Perdite", "Stato"],
                _ft_rows,
            )

            # Azioni bulk sui target selezionati (stile console)
            ba1, ba2, ba3, ba4 = st.columns([3, 1, 1, 1])
            with ba1:
                _ft_sel = st.multiselect(
                    "Seleziona target:",
                    options=list(_ft_label.keys()),
                    format_func=lambda tid: _ft_label[tid],
                    key=f"farm_bulk_{mod_acc.account_id}",
                    label_visibility="collapsed",
                    placeholder="Seleziona i target per le azioni...",
                )
            with ba2:
                if st.button("✅ Approva", use_container_width=True,
                             key=f"fbapp_{mod_acc.account_id}"):
                    for tid in _ft_sel:
                        _fstore.approve(tid)
                    st.rerun()
            with ba3:
                if st.button("❌ Scarta", use_container_width=True,
                             key=f"fbrej_{mod_acc.account_id}"):
                    for tid in _ft_sel:
                        _fstore.reject(tid)
                    st.rerun()
            with ba4:
                if st.button("↩️ Ripristina", use_container_width=True,
                             key=f"fbrst_{mod_acc.account_id}"):
                    for tid in _ft_sel:
                        _fstore.set_status(tid, FARM_STATUS_PENDING)
                    st.rerun()
            # (azioni per-riga sostituite dalle azioni bulk sopra)

        # --- Inserimento manuale target ---
        st.markdown("**➕ Inserimento Manuale Target**")
        st.caption(
            "Conosci un' oasi abbandonata o un villaggio inattivo da "
            "saccheggiare? Inseriscilo qui: entra come pending e va approvato "
            "come gli altri."
        )
        mt1, mt2, mt3, mt4, mt5 = st.columns([0.9, 0.9, 1.8, 0.9, 1.3])
        with mt1:
            _mt_x = st.number_input(
                "Coord. X:", min_value=-400, max_value=400, value=0,
                step=1, key=f"fmtx_{mod_acc.account_id}",
            )
        with mt2:
            _mt_y = st.number_input(
                "Coord. Y:", min_value=-400, max_value=400, value=0,
                step=1, key=f"fmty_{mod_acc.account_id}",
            )
        with mt3:
            _mt_name = st.text_input(
                "Nome (opz.):", key=f"fmtn_{mod_acc.account_id}",
            )
        with mt4:
            _mt_oasis = st.checkbox(
                "Oasi", key=f"fmto_{mod_acc.account_id}",
            )
        with mt5:
            st.write(" ")
            if st.button(
                "Aggiungi Target",
                key=f"fmtb_{mod_acc.account_id}",
            ):
                _mx, _my = int(_mt_x), int(_mt_y)
                if _fstore.status_at(_mx, _my) is not None:
                    st.warning(
                        f"({_mx}|{_my}) gia' presente in catalogo."
                    )
                else:
                    _new_t = _fstore.add_manual_target(
                        _mx, _my,
                        name=_mt_name.strip(),
                        is_oasis=bool(_mt_oasis),
                    )
                    if _new_t is not None:
                        st.success(
                            f"Target ({_mx}|{_my}) aggiunto come pending. "
                            "Approva per attivare i raid."
                        )
                        st.rerun()


# 2.6 PAGINA ESPANSIONE & CROPPER (Human-in-the-Loop)
if page == "Espansione":
    ui.page_header(
        "Espansione",
        "Cropper finder e fondazione villaggi — i tile partono solo dopo approvazione umana.",
    )

    if not accounts:
        st.info("Nessun account configurato.")
    else:
        e_acc = _selected_account()
        st.caption(f"Account attivo: **{e_acc.account_id}** — {e_acc.server_url}")
        store = CropperStore()
        candidates = store.list_candidates(e_acc.account_id)

        f_col1, f_col2, f_col3 = st.columns(3)
        with f_col1:
            f_type = st.selectbox(
                "Filtro tipo:", ["Tutti", "15c", "9c", "6c"],
                format_func=lambda t: {"6c": "Standard"}.get(t, t),
                key="cropper_f_type",
            )
        with f_col2:
            f_status = st.selectbox(
                "Filtro stato:",
                ["Tutti", STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
                 STATUS_SETTLING, STATUS_SETTLED],
                key="cropper_f_status",
            )
        with f_col3:
            st.write(" ")
            if st.button("🔄 Aggiorna lista", key="cropper_refresh"):
                st.rerun()

        filtered = [
            c for c in candidates
            if (f_type == "Tutti" or c.type == f_type)
            and (f_status == "Tutti" or c.status == f_status)
        ]

        if not filtered:
            st.info(
                "Nessun candidato trovato. Lo scanner di mappa popola questa "
                "lista durante il ciclo dell'agente (richiede `cropper_scan` attivo)."
            )
        else:
            _crop_tone = {
                STATUS_PENDING: ("pending", "warning"),
                STATUS_APPROVED: ("approved", "success"),
                STATUS_REJECTED: ("rejected", "neutral"),
                STATUS_SETTLING: ("settling", "info"),
                STATUS_SETTLED: ("settled", "success"),
            }
            _type_label = {
                "15c": "15 Grani", "9c": "9 Grani", "6c": "Standard",
            }
            _cand_rows = []
            _cand_label = {}
            for cand in filtered:
                _lbl, _tone = _crop_tone.get(cand.status, (cand.status, "neutral"))
                _cand_label[cand.id] = f"({cand.x}|{cand.y}) {cand.type}"
                oases_txt = ", ".join(
                    f"+{o.get('bonus', 25)}% {o.get('type', 'crop')}"
                    for o in (cand.oases or [])
                ) or "—"
                _cand_rows.append(
                    [
                        f"<span class='mono'>({cand.x} | {cand.y})</span>",
                        ui.esc(_type_label.get(cand.type, cand.type))
                        + (" \u270b" if getattr(cand, "manual", False) else ""),
                        f"{cand.distance_hours:.1f} ore",
                        f"<span class='dim'>{ui.esc(oases_txt)}</span>",
                        ui.pill(_lbl, _tone),
                    ]
                )
            ui.render_table(
                ["Coordinate", "Tipo", "Distanza", "Oasi Bonus", "Stato"],
                _cand_rows,
            )

            ca1, ca2, ca3, ca4 = st.columns([3, 1, 1, 1])
            with ca1:
                _cand_sel = st.multiselect(
                    "Seleziona candidati:",
                    options=list(_cand_label.keys()),
                    format_func=lambda cid: _cand_label[cid],
                    key=f"crop_bulk_{e_acc.account_id}",
                    label_visibility="collapsed",
                    placeholder="Seleziona i candidati per le azioni...",
                )
            with ca2:
                if st.button("✅ Approva", use_container_width=True,
                             key=f"cbapp_{e_acc.account_id}"):
                    for cid in _cand_sel:
                        store.approve(e_acc.account_id, cid)
                    st.rerun()
            with ca3:
                if st.button("❌ Scarta", use_container_width=True,
                             key=f"cbrej_{e_acc.account_id}"):
                    for cid in _cand_sel:
                        store.reject(e_acc.account_id, cid)
                    st.rerun()
            with ca4:
                if st.button("↩️ Ripristina", use_container_width=True,
                             key=f"cbrst_{e_acc.account_id}"):
                    for cid in _cand_sel:
                        store.set_status(e_acc.account_id, cid, STATUS_PENDING)
                    st.rerun()

        # --- Inserimento manuale candidato ---
        # Tile conosciuti via passaparola/alleanza: entrano in watchlist
        # come 'pending' (flag manual) e seguono lo stesso flusso di
        # approvazione dei candidati rilevati dallo scanner.
        st.divider()
        st.markdown("**\u2795 Inserimento Manuale Candidato**")
        st.caption(
            "Conosci gia' un tile libero tramite passaparola o alleanza? "
            "Inseriscilo qui: compare in cima alla watchlist come pending "
            "e va approvato come gli altri."
        )
        m_c1, m_c2, m_c3, m_c4 = st.columns([1.0, 1.0, 1.3, 1.6])
        with m_c1:
            m_x = st.number_input(
                "Coord. X:", min_value=-400, max_value=400, value=0,
                step=1, key=f"mcx_{e_acc.account_id}",
            )
        with m_c2:
            m_y = st.number_input(
                "Coord. Y:", min_value=-400, max_value=400, value=0,
                step=1, key=f"mcy_{e_acc.account_id}",
            )
        with m_c3:
            m_type = st.selectbox(
                "Tipo:", ["15c", "9c", "Standard"],
                key=f"mct_{e_acc.account_id}",
            )
        with m_c4:
            st.write(" ")
            if st.button(
                "Aggiungi Manualmente",
                key=f"mcb_{e_acc.account_id}",
            ):
                _mtype = "6c" if m_type == "Standard" else m_type
                _mx, _my = int(m_x), int(m_y)
                _existing = store.status_at(e_acc.account_id, _mx, _my)
                if _existing is not None:
                    st.warning(
                        f"({_mx}|{_my}) gia' presente in watchlist "
                        f"(stato: {_existing})."
                    )
                else:
                    _new_c = store.add_manual_candidate(
                        e_acc.account_id, _mx, _my, _mtype,
                    )
                    if _new_c is not None:
                        st.success(
                            f"Candidato {_mtype} a ({_mx}|{_my}) aggiunto "
                            "in cima alla watchlist (pending)."
                        )
                        st.rerun()

    # --- Smart Building planner: prossimi upgrade stimati per villaggio ---
    st.divider()
    st.subheader("🏗️ Piano Upgrade Pianificato (Smart Building)")
    st.caption(
        "Prossimi upgrade stimati dal planner dinamico "
        "(storage > grain safety > focus > ruolo > economia). "
        "Aggiornato a ogni ciclo dell'agente."
    )
    plans = load_build_plans(e_acc.account_id)
    if not plans:
        st.info(
            "Nessun piano disponibile: il planner lo genera al prossimo "
            "ciclo dell'agente."
        )
    else:
        _kind_label = {
            "storage": "📦 Storage",
            "grain_safety": "🌾 Grain Safety",
            "focus": "🎯 Focus Manuale",
            "role": "🎖️ Ruolo",
            "economy": "📈 Economia",
        }
        for pv_name, entry in plans.items():
            items = (entry or {}).get("items") or []
            if not items:
                continue
            with st.expander(f"🏰 {pv_name} — {len(items)} upgrade previsti"):
                for idx, it in enumerate(items, start=1):
                    st.write(
                        f"{idx}. **{it.get('name', 'slot ' + str(it.get('slot_id')))}** "
                        f"→ liv. {it.get('target_level')} "
                        f"· {_kind_label.get(it.get('kind'), it.get('kind'))} "
                        f"· _{it.get('reason', '')}_"
                    )


# 3. PAGINA GESTIONE ACCOUNT
if page == "Account":
    ui.page_header(
        "Account",
        "Configurazione multi-account / multi-server — cookie di sessione separati per agente.",
    )

    # Form Aggiunta/Modifica
    with st.expander("➕ Aggiungi un Nuovo Account / Server", expanded=len(accounts) == 0):
        with st.form("new_account_form"):
            ca, cb = st.columns(2)
            with ca:
                new_acc_id = st.text_input("ID / Alias Account (es. server_italia, speed_server):", value=f"server_{len(accounts)+1}")
                new_server_url = st.text_input("URL Server (es. https://ts19.x1.europe.travian.com):", value="https://")
                new_username = st.text_input("Username (opzionale con login manuale):")
            with cb:
                new_password = st.text_input("Password (opzionale):", type="password")
                new_loop_min = st.number_input("Intervallo Minimo Ciclo (sec):", value=30.0, step=5.0)
                new_loop_max = st.number_input("Intervallo Massimo Ciclo (sec):", value=90.0, step=5.0)

            submitted_acc = st.form_submit_button("💾 Salva e Aggiungi Account", type="primary")
            if submitted_acc:
                if not new_server_url or new_server_url == "https://":
                    st.error("Inserisci un URL server valido.")
                else:
                    target_id = new_acc_id.strip() or f"acc_{len(accounts)+1}"
                    existing_sett = next(
                        (a for a in accounts if a.account_id == target_id), None
                    )
                    if existing_sett is not None:
                        # Update: preserva moduli (difesa, farming, scout,
                        # eroe, replenishment, espansione) e villaggi.
                        # Username/password vuoti = mantieni i precedenti.
                        existing_sett.server_url = (
                            new_server_url.strip().rstrip("/")
                        )
                        if new_username.strip():
                            existing_sett.username = new_username.strip()
                        if new_password.strip():
                            existing_sett.password = new_password.strip()
                        existing_sett.loop_interval_min = new_loop_min
                        existing_sett.loop_interval_max = new_loop_max
                        new_sett = existing_sett
                    else:
                        new_sett = Settings(
                            account_id=target_id,
                            server_url=new_server_url.strip().rstrip("/"),
                            username=new_username.strip(),
                            password=new_password.strip(),
                            loop_interval_min=new_loop_min,
                            loop_interval_max=new_loop_max,
                        )
                    # Sostituisci se esiste o aggiungi
                    updated = [a for a in accounts if a.account_id != new_sett.account_id]
                    updated.append(new_sett)
                    save_accounts(updated)
                    # Se è il primo, aggiorna anche .env per compatibilità
                    if len(updated) == 1:
                        save_env_settings({
                            "TRAVIAN_SERVER_URL": new_sett.server_url,
                            "TRAVIAN_USERNAME": new_sett.username,
                            "TRAVIAN_PASSWORD": new_sett.password,
                        })
                    st.success(f"Account '{new_sett.account_id}' salvato!")
                    time.sleep(1)
                    st.rerun()

    # Rimozione account (la tabella agenti e' nella pagina Overview)
    ui.section("Rimuovi account")
    if accounts:
        d1, d2 = st.columns([3, 1])
        with d1:
            del_sel = st.selectbox(
                "Seleziona account da rimuovere:",
                [a.account_id for a in accounts],
                key="del_acc_sel",
            )
        with d2:
            st.write(" ")
            if st.button(
                "🗑️ Rimuovi Account", key="del_acc_btn", use_container_width=True
            ):
                remaining = [a for a in accounts if a.account_id != del_sel]
                # Rimozione esplicita: consentito svuotare la lista
                save_accounts(remaining, allow_empty=True)
                victim = next(
                    (a for a in accounts if a.account_id == del_sel), None
                )
                if victim and victim.state_file and victim.state_file.exists():
                    victim.state_file.unlink(missing_ok=True)
                st.toast(f"Account {del_sel} rimosso.")
                time.sleep(0.5)
                st.rerun()
    else:
        st.info("Nessun account presente.")


# 3. PAGINA AUDITS (log eventi stile console)
if page == "Audits":
    ui.page_header(
        "Audits",
        "Esplora gli eventi di esecuzione generati dagli agenti (un file di log per account).",
    )
    # Ogni agente scrive su agent_<id>.log; agent.log resta per i log legacy.
    log_files = sorted(STATE_DIR.glob("agent*.log"))
    auto_refresh = False
    if not log_files:
        st.info("Nessun file di log trovato. Avvia l'agente per generare gli eventi.")
    else:
        sel_log = st.selectbox("File di log:", log_files, format_func=lambda p: p.name)
        c_log1, c_log2 = st.columns([1, 4])
        with c_log1:
            auto_refresh = st.checkbox("Auto-aggiorna", value=True)
            if st.button("🗑️ Pulisci log"):
                sel_log.write_text("", encoding="utf-8")
                st.rerun()

        # --- Toolbar filtri stile console ---
        f1, f2 = st.columns([2, 2])
        with f1:
            search_q = st.text_input(
                "Cerca evento:",
                "",
                placeholder="🔍 Filtra per modulo o messaggio...",
                label_visibility="collapsed",
            )
        with f2:
            sel_levels = st.pills(
                "Livello",
                ["INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG"],
                selection_mode="multi",
                default=["INFO", "WARNING", "ERROR", "CRITICAL"],
                label_visibility="collapsed",
            )

        log_content = sel_log.read_text(encoding="utf-8", errors="replace")
        events: list[dict] = []
        for _ln in log_content.splitlines():
            if not _ln.strip():
                continue
            m = _LOG_LINE_RE.match(_ln)
            if m:
                events.append(m.groupdict())
            elif events:
                # Continuazioni (traceback) accodate all'evento precedente
                events[-1]["msg"] += "  " + _ln.strip()
            else:
                events.append(
                    {"ts": "", "level": "", "logger": "", "msg": _ln.strip()}
                )
        events.reverse()  # eventi più recenti in alto

        _audit_rows: list[list[str]] = []
        _q = search_q.strip().lower()
        for ev in events:
            _lvl = ev["level"]
            if sel_levels and _lvl and _lvl not in sel_levels:
                continue
            if _q and _q not in f"{ev['logger']} {ev['msg']}".lower():
                continue
            _ts = ev["ts"].replace("T", " ")[5:] if ev["ts"] else "—"
            _audit_rows.append(
                [
                    f"<span class='mono dim'>{ui.esc(_ts)}</span>",
                    ui.pill(_lvl or "LOG", _LOG_LEVEL_TONE.get(_lvl, "neutral")),
                    f"<span class='dim'>{ui.esc(ev['logger'].replace('travian_agent.', ''))}</span>",
                    ui.esc(ev["msg"]),
                ]
            )
            if len(_audit_rows) >= 200:
                break

        if _audit_rows:
            ui.render_table(["Time", "Level", "Module", "Message"], _audit_rows)
            st.caption(
                f"{len(events)} eventi nel file — mostrati {len(_audit_rows)} più recenti."
            )
        else:
            st.info("Nessun evento corrisponde ai filtri.")

    if auto_refresh and is_agent_running():
        time.sleep(3)
        st.rerun()


# 4. PAGINA DEBUG
if page == "Debug":
    ui.page_header("Debug", "Screenshot e artefatti di diagnostica.")
    if DEBUG_DIR.exists():
        pngs = list(DEBUG_DIR.rglob("*.png"))
        if pngs:
            for p in pngs:
                st.write(f"**{p.parent.name}/{p.name}**")
                st.image(str(p))
        else:
            st.info("Nessuno screenshot trovato in `debug/`.")
    else:
        st.info("Cartella `debug/` vuota.")
