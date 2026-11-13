"""Dashboard Web in Streamlit per configurare e gestire Travian Agent (Supporto Multi-Account)."""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import streamlit as st
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

STATE_DIR = BASE_DIR / "state"
DEBUG_DIR = BASE_DIR / "debug"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "agent.log"
PYTHON_EXE = sys.executable

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
    """Estrae l'ultimo snapshot registrato per ciascun villaggio dal JSONL dello storico.

    Filtraggio stretto per account: usa solo `safe_id`, lo stesso nome file
    che l'orchestratore scrive in `_append_history`. Nessun fallback su nomi
    raw: due account con id simili (es. 'server 11' vs 'server_11')
    non devono mai leggere lo storico l'uno dell'altro.
    """
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
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
    """Carica i dati di intelligence radar, report e valutazione globale dell'AI Scout Brain."""
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
    scout_file = STATE_DIR / f"scout_{safe_id}.json"
    if not scout_file.exists():
        return {}
    try:
        return json.loads(scout_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_hero_data(account_id: str) -> dict:
    """Carica l'ultimo snapshot dell'eroe scritto da HeroManager."""
    safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
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


# Caricamento account configurati
accounts = load_accounts()

# --- BARRA LATERALE ---
with st.sidebar:
    st.title("🛡️ Travian Agent")
    st.caption("Multi-Account & Multi-Server Dashboard")

    st.divider()

    # Stato Bot (sessione + orfani rilevati dai PID file)
    running_agents = find_running_agents()
    running = is_agent_running()
    if running:
        # Controllo stato di pausa per l'account principale/attivo
        paused_any = any(is_account_paused(acc.account_id) for acc in accounts)
        if paused_any:
            st.warning("⏸️ **Agente in PAUSA d'emergenza**")
        else:
            if running_agents:
                labels = ", ".join(f"{a} (PID {p})" for a, p in running_agents)
                st.success(f"🟢 **In esecuzione**: {labels}")
            else:
                st.success("🟢 **In esecuzione**")

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
        st.info("⚪ **Agente fermo**")

    st.divider()

    st.subheader(f"👥 Account Attivi ({len(accounts)})")
    if accounts:
        for acc in accounts:
            s_info = get_account_session_info(acc)
            icon = "🔑" if s_info["exists"] else "⚠️"
            st.write(f"{icon} **{acc.account_id}** ({acc.server_url})")
            if s_info["exists"]:
                st.caption(f"Sessione valida: {s_info['time']}")
    else:
        st.warning("Nessun account configurato.")

    st.divider()
    st.caption("⚠️ **Promemoria ToS**: Solo a scopo didattico / test.")


# --- CONTENUTO PRINCIPALE ---
tab_control, tab_villages, tab_warroom, tab_modules, tab_expansion, tab_accounts, tab_logs, tab_debug = st.tabs(
    [
        "🎮 Controllo & Avvio",
        "🏰 Villaggi & Strategia",
        "⚔️ War Room & AI Scout",
        "🌾 Farming & Espansione",
        "🌍 Espansione & Cropper",
        "👥 Gestione Account / Server",
        "📜 Log Paralleli",
        "🖼️ Debug & Screenshot",
    ]
)

# 1. TAB CONTROLLO & AVVIO
with tab_control:
    st.subheader("1. Login Manuale Assistito")
    st.write(
        "Effettua il login sul browser per l'account desiderato per salvare cookie e sessione (`session_<account_id>.json`)."
    )

    if not accounts:
        st.warning("Configura almeno un account nella scheda '👥 Gestione Account / Server'.")
    else:
        col_acc_sel, col_btn_login = st.columns([2, 2])
        with col_acc_sel:
            account_names = [f"{acc.account_id} ({acc.server_url})" for acc in accounts]
            selected_idx = st.selectbox("Seleziona Account per il Login:", range(len(accounts)), format_func=lambda i: account_names[i])
            chosen_acc = accounts[selected_idx]

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
            acc_i = st.selectbox("Scegli Account:", range(len(accounts)), format_func=lambda i: account_names[i])
            target_account = accounts[acc_i].account_id

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


# 2. TAB VILLAGGI & STRATEGIA
with tab_villages:
    st.subheader("🏰 Situazione Villaggi & Gestione Strategica (Macro / Micro)")
    st.caption("Visualizza capienza magazzini, code e attacchi in tempo reale e modifica i ruoli con applicazione immediata a caldo (senza riavviare il bot).")

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        # Selezione account
        col_acc, col_strat = st.columns([1, 1])
        with col_acc:
            acc_opts = [f"{a.account_id} ({a.server_url})" for a in accounts]
            sel_v_acc_idx = st.selectbox(
                "Seleziona Account:", range(len(accounts)),
                format_func=lambda i: acc_opts[i], key="v_acc_sel",
                on_change=_on_village_account_change,
            )
            cur_acc = accounts[sel_v_acc_idx]

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
                vrole = vcfg.role or "standard"

                with st.container(border=True):
                    head_col1, head_col2 = st.columns([3, 1])
                    with head_col1:
                        badge_color = {
                            "hammer": "🔴 Hammer (Offensivo)",
                            "defensive": "🛡️ Defensive (Scudo)",
                            "croplog": "🌾 Croplog (Capitale)",
                            "feeder": "📦 Feeder (Economico)",
                            "chiefing": "👑 Chiefing (Espansione/Nobili)",
                            "early_cropper_founder": "🚀 Founder (Pioniere 15c)",
                            "standard": "⚙️ Standard",
                        }.get(vrole, f"⚙️ {vrole}")
                        target_info = f" ➡️ Target: `{vcfg.target_village}`" if vcfg.target_village else ""
                        st.markdown(f"### 🏰 `{vname}` — **{badge_color}**{target_info}")
                    with head_col2:
                        if snap:
                            ts_str = datetime.datetime.fromtimestamp(snap.get("ts", 0)).strftime("%H:%M:%S")
                            st.caption(f"Ultimo aggiornamento: **{ts_str}**")
                        else:
                            st.caption("In attesa di scansione...")

                    if snap:
                        res = snap.get("resources", {})
                        wood = res.get("wood", 0)
                        clay = res.get("clay", 0)
                        iron = res.get("iron", 0)
                        crop = res.get("crop", 0)
                        wh_cap = res.get("warehouse_cap", 0) or 1
                        gr_cap = res.get("granary_cap", 0) or 1

                        # Metriche risorse
                        mc1, mc2, mc3, mc4, mc5, mc6 = st.columns(6)
                        mc1.metric("🪵 Legno", f"{wood:,}")
                        mc2.metric("🧱 Argilla", f"{clay:,}")
                        mc3.metric("⛓️ Ferro", f"{iron:,}")
                        mc4.metric("🌾 Grano", f"{crop:,}")
                        mc5.metric("🔨 Coda", snap.get("build_queue", 0))
                        attacks = snap.get("incoming_attacks", 0)
                        if attacks > 0:
                            mc6.metric("⚔️ Attacchi", attacks, delta="In arrivo!", delta_color="inverse")
                        else:
                            mc6.metric("⚔️ Attacchi", 0)

                        # Barre di capienza
                        wh_pct = min(1.0, max(wood, clay, iron) / wh_cap)
                        gr_pct = min(1.0, crop / gr_cap)

                        pc1, pc2 = st.columns(2)
                        with pc1:
                            st.write(f"**Magazzino**: {max(wood, clay, iron):,} / {wh_cap:,} ({wh_pct*100:.1f}%)")
                            st.progress(wh_pct)
                        with pc2:
                            st.write(f"**Granaio**: {crop:,} / {gr_cap:,} ({gr_pct*100:.1f}%)")
                            st.progress(gr_pct)

                        action_text = snap.get("action", "-")
                        reason_text = snap.get("reason", "-")
                        st.caption(f"Ultima decisione: `{action_text}` — {reason_text}")

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


# 2.2 TAB WAR ROOM & AI SCOUT BRAIN
with tab_warroom:
    st.subheader("⚔️ War Room: Intelligence Imperiale & AI Scout Radar")
    st.caption("Pannello di controllo strategico globale: bilancio della potenza militare, radar mappa, target saccheggio e raccomandazioni tattiche con Gemini AI.")

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        w_acc_opts = [f"{a.account_id} ({a.server_url})" for a in accounts]
        w_acc_idx = st.selectbox("Seleziona Account War Room:", range(len(accounts)), format_func=lambda i: w_acc_opts[i], key="warroom_acc_sel")
        w_acc = accounts[w_acc_idx]

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

            for t in filtered_tgts:
                is_bl = t.get("is_blacklisted", False)
                with st.container(border=True):
                    tc1, tc2, tc3, tc4 = st.columns([2, 1, 1, 2])
                    with tc1:
                        icon = "⛔" if is_bl else ("🏝️" if t.get("is_oasis") else "🏕️")
                        st.markdown(f"**{icon} {t.get('village_name')}** `({t.get('x')}|{t.get('y')})`")
                        if t.get("player_name"):
                            st.caption(f"Giocatore: {t.get('player_name')} | Pop: {t.get('population', 0)}")
                    with tc2:
                        st.write(f"📏 Dist: **{t.get('distance', 0):.1f}**")
                    with tc3:
                        loot = t.get("last_loot_ratio", 1.0)
                        st.write(f"💰 Bottino: **{int(loot*100)}%**")
                    with tc4:
                        if is_bl:
                            st.error(f"Blacklist: {t.get('notes', 'Perdite rilevate')}")
                        else:
                            st.success("Target Valido / Pronto")
        else:
            st.info("Nessun tile scansionato finora. L'agente esplorerà la mappa al prossimo ciclo con il sensore mappa attivo.")


# 2.5 TAB FARMING & ESPANSIONE
with tab_modules:
    st.subheader("🌾 Automazioni: Saccheggio (Farm List) e Fondazione Villaggi")
    st.caption(
        "Attiva i moduli autonomi. Le modifiche vengono salvate su accounts.json "
        "e applicate a caldo dall'agente al prossimo ciclo (senza riavvio)."
    )

    if not accounts:
        st.warning("Nessun account configurato.")
    else:
        mod_acc_opts = [f"{a.account_id} ({a.server_url})" for a in accounts]
        mod_acc_idx = st.selectbox(
            "Seleziona Account:", range(len(accounts)),
            format_func=lambda i: mod_acc_opts[i], key="mod_acc_sel",
        )
        mod_acc = accounts[mod_acc_idx]

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
                )
            with f_col2:
                cur_unit = mod_acc.farming.unit or unit_choices[0]
                if cur_unit not in unit_choices:
                    unit_choices.append(cur_unit)
                farm_unit_g = st.selectbox(
                    "Unità di saccheggio predefinita:", unit_choices,
                    index=unit_choices.index(cur_unit),
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


# 2.6 TAB ESPANSIONE & CROPPER (Human-in-the-Loop)
with tab_expansion:
    st.subheader("🌍 Cropper Finder: Candidati in Attesa di Approvazione")
    st.caption(
        "Il bot scansiona la mappa attorno ai villaggi e cataloga i tile 9c/15c liberi. "
        "La fondazione parte SOLO dopo l'approvazione umana (stato `approved`)."
    )

    if not accounts:
        st.info("Nessun account configurato.")
    else:
        e_opts = [f"{a.account_id} ({a.server_url})" for a in accounts]
        e_acc_idx = st.selectbox(
            "Seleziona Account:", range(len(accounts)),
            format_func=lambda i: e_opts[i], key="cropper_acc_sel",
        )
        e_acc = accounts[e_acc_idx]
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
            _status_label = {
                STATUS_PENDING: "🕐 In attesa",
                STATUS_APPROVED: "✅ Approvato",
                STATUS_REJECTED: "❌ Scartato",
                STATUS_SETTLING: "🚀 Coloni in viaggio",
                STATUS_SETTLED: "🏘️ Fondato",
            }
            _type_label = {
                "15c": "15 Grani", "9c": "9 Grani", "6c": "Standard",
            }
            h = st.columns([1.4, 1.1, 1.2, 2.0, 1.4, 2.0])
            for col, title in zip(h, ("Coordinate", "Tipo", "Distanza", "Oasi Bonus", "Stato", "Azioni")):
                col.markdown(f"**{title}**")
            st.divider()
            for cand in filtered:
                r = st.columns([1.4, 1.1, 1.2, 2.0, 1.4, 2.0])
                r[0].write(f"({cand.x} | {cand.y})")
                r[1].write(
                    _type_label.get(cand.type, cand.type)
                    + (" \u270b" if getattr(cand, "manual", False) else "")
                )
                r[2].write(f"{cand.distance_hours:.1f} ore")
                oases_txt = ", ".join(
                    f"+{o.get('bonus', 25)}% {o.get('type', 'crop')}"
                    for o in (cand.oases or [])
                ) or "—"
                r[3].write(oases_txt)
                r[4].write(_status_label.get(cand.status, cand.status))
                with r[5]:
                    if cand.status == STATUS_PENDING:
                        b1, b2 = st.columns(2)
                        with b1:
                            if st.button(
                                "Approva",
                                key=f"app_{e_acc.account_id}_{cand.id}",
                                type="primary",
                            ):
                                store.approve(e_acc.account_id, cand.id)
                                st.rerun()
                        with b2:
                            if st.button(
                                "Scarta",
                                key=f"rej_{e_acc.account_id}_{cand.id}",
                            ):
                                store.reject(e_acc.account_id, cand.id)
                                st.rerun()
                    elif cand.status == STATUS_APPROVED:
                        st.caption("In coda fondazione")
                    elif cand.status == STATUS_REJECTED:
                        if st.button(
                            "Ripristina",
                            key=f"rst_{e_acc.account_id}_{cand.id}",
                        ):
                            store.set_status(
                                e_acc.account_id, cand.id, STATUS_PENDING
                            )
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


# 3. TAB GESTIONE ACCOUNT
with tab_accounts:
    st.subheader("Configurazione Multi-Account / Multi-Server")
    st.write("Aggiungi più server o account. Ognuno manterrà i propri cookie di sessione separati.")

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

    # Tabella account esistenti con opzione elimina
    st.markdown("#### Elenco Account Registrati")
    if accounts:
        for idx, acc in enumerate(accounts):
            with st.container(border=True):
                c_info, c_action = st.columns([3, 1])
                s_info = get_account_session_info(acc)
                with c_info:
                    st.markdown(f"### 🛡️ `{acc.account_id}`")
                    st.write(f"**Server:** `{acc.server_url}` | **User:** `{acc.username or '(Login manuale)'}`")
                    st.write(f"**Sessione Salvata:** {s_info['time']} ({s_info['size'] if s_info['exists'] else 'Nessuna'})")
                with c_action:
                    st.write(" ")
                    if st.button("🗑️ Rimuovi Account", key=f"del_{acc.account_id}"):
                        remaining = [a for a in accounts if a.account_id != acc.account_id]
                        # Rimozione esplicita: consentito svuotare la lista
                        save_accounts(remaining, allow_empty=True)
                        if acc.state_file and acc.state_file.exists():
                            acc.state_file.unlink(missing_ok=True)
                        st.toast(f"Account {acc.account_id} rimosso.")
                        time.sleep(0.5)
                        st.rerun()
    else:
        st.info("Nessun account presente.")


# 3. TAB LOG
with tab_logs:
    st.subheader("Log di Esecuzione (un file per account)")
    # Ogni agente scrive su agent_<id>.log; agent.log resta per i log legacy.
    log_files = sorted(STATE_DIR.glob("agent*.log"))
    if not log_files:
        st.info("Nessun file di log trovato. Avvia l'agente per generare i log.")
        auto_refresh = False
    else:
        sel_log = st.selectbox("File di log:", log_files, format_func=lambda p: p.name)
        c_log1, c_log2 = st.columns([1, 4])
        with c_log1:
            auto_refresh = st.checkbox("Auto-aggiorna log", value=True)
            if st.button("🗑️ Pulisci log"):
                sel_log.write_text("", encoding="utf-8")
                st.rerun()

        log_content = sel_log.read_text(encoding="utf-8", errors="replace")
        lines = log_content.splitlines()[-120:]
        st.code("\n".join(lines) if lines else "Nessun log disponibile.", language="text")

    if auto_refresh and is_agent_running():
        time.sleep(3)
        st.rerun()


# 4. TAB DEBUG
with tab_debug:
    st.subheader("Screenshot di Debug")
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
