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
from pathlib import Path

import streamlit as st
from travian_agent.config import (
    BASE_DIR,
    ExpansionTarget,
    Settings,
    VillageConfig,
    load_accounts,
    save_accounts,
    save_env_settings,
)
from travian_agent.models import STATIC_TROOP_LABELS, VillageRole

st.set_page_config(
    page_title="Travian Agent Multi-Account Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

STATE_DIR = BASE_DIR / "state"
DEBUG_DIR = BASE_DIR / "debug"
LOG_FILE = BASE_DIR / "agent.log"
PYTHON_EXE = sys.executable

if "agent_process" not in st.session_state:
    st.session_state.agent_process = None


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
    proc = st.session_state.agent_process
    if proc is not None:
        if proc.poll() is None:
            return True
        st.session_state.agent_process = None
    return bool(find_running_agents())


def stop_all_agents() -> int:
    """Termina il processo della sessione e tutti gli agenti rilevati dai PID file."""
    stopped = 0
    proc = st.session_state.agent_process
    if proc is not None:
        try:
            proc.terminate()
            stopped += 1
        except Exception:  # noqa: BLE001
            pass
        st.session_state.agent_process = None
    for _acc_id, pid in find_running_agents():
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except Exception:  # noqa: BLE001
            pass
    return stopped


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
    """Estrae l'ultimo snapshot registrato per ciascun villaggio dal JSONL dello storico."""
    history_file = STATE_DIR / "history" / f"history_{account_id}.jsonl"
    if not history_file.exists():
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
            if st.button("⏹️ Ferma", use_container_width=True, type="secondary"):
                n = stop_all_agents()
                for acc in accounts:
                    set_account_pause(acc.account_id, False)
                st.toast(f"Agente arrestato ({n} processo/i).", icon="⏹️")
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
tab_control, tab_villages, tab_warroom, tab_modules, tab_accounts, tab_logs, tab_debug = st.tabs(
    [
        "🎮 Controllo & Avvio",
        "🏰 Villaggi & Strategia",
        "⚔️ War Room & AI Scout",
        "🌾 Farming & Espansione",
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
        if not is_agent_running():
            if st.button("▶️ Avvia Agente / Orchestratore", type="primary", use_container_width=True):
                if not accounts:
                    st.error("Configura prima almeno un account!")
                else:
                    cmd = [PYTHON_EXE, "main.py", "--brain", selected_brain]
                    if target_account:
                        cmd += ["--account", target_account]
                    if not headless_opt:
                        cmd.append("--headed")
                    if single_run:
                        cmd.append("--once")

                    log_f = open(LOG_FILE, "a", encoding="utf-8")
                    log_f.write(f"\n--- AVVIO ORCHESTRATORE: {datetime.datetime.now()} (Modalità: {run_mode}) ---\n")
                    log_f.flush()

                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(BASE_DIR),
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    st.session_state.agent_process = proc
                    st.success("Agente avviato con successo!")
                    time.sleep(1)
                    st.rerun()
        else:
            st.warning("L'agente/orchestratore è già attivo in background.")
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
                "Seleziona Account:", range(len(accounts)), format_func=lambda i: acc_opts[i], key="v_acc_sel"
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

        with st.form("village_roles_form"):
            all_known = list(cur_acc.villages.keys())
            for sv in snapshots.keys():
                if sv not in all_known:
                    all_known.append(sv)

            updated_villages: dict[str, VillageConfig] = {}
            target_options = ["(Nessuno)"] + [k for k in all_known]

            role_choices = ["standard", "hammer", "defensive", "croplog", "feeder"]
            role_labels = {
                "standard": "standard — Sviluppo bilanciato campi ed edifici",
                "hammer": "hammer (Offensivo) — Caserma/Stalla/Fucina + Truppe d'attacco",
                "defensive": "defensive (Scudo) — Caserma/Mura/Residenza + Truppe difesa",
                "croplog": "croplog (Capitale) — Focus 100% Grano + Granai/Mulino/Panificio",
                "feeder": "feeder (Economico) — Mercato/Magazzini + Spedizione surplus al Target",
            }

            # Chiavi edificio (salvate in building_focus) -> etichetta italiana
            building_focus_labels = {
                "main": "🏛️ Edificio Principale",
                "warehouse": "📦 Magazzino",
                "granary": "🌾 Granaio",
                "cranny": "🕳️ Nascondiglio",
                "embassy": "🤝 Ambasciata",
                "rally_point": "🚩 Punto di Raduno",
                "marketplace": "⚖️ Mercato",
                "residence": "🏠 Residenza",
                "palace": "🏰 Palazzo",
                "treasury": "💰 Tesoreria",
                "academy": "🎓 Accademia",
                "smithy": "⚒️ Fucina",
                "tournament_square": "🏟️ Piazza del Torneo",
                "barracks": "⚔️ Caserma",
                "great_barracks": "⚔️ Grande Caserma",
                "stable": "🐎 Scuderia",
                "great_stable": "🐎 Grande Scuderia",
                "workshop": "🛠️ Officina",
                "hospital": "🏥 Ospedale",
                "hero_mansion": "🦸 Casa dell'Eroe",
                "town_hall": "🎉 Municipio",
                "trade_office": "📈 Ufficio Commerciale",
                "brickyard": "🧱 Fabbrica di Mattoni",
                "sawmill": "🪚 Segheria",
                "iron_foundry": "⛏️ Fonderia",
                "grain_mill": "🌾 Mulino",
                "bakery": "🥖 Panificio",
                "stonemason_lodge": "🪨 Bottega del Marmista",
                "trapper": "🪤 Trappolaio",
                "horse_drinking_trough": "🐴 Abbeveratoio",
                "great_warehouse": "📦 Grande Magazzino",
                "great_granary": "🌾 Grande Granaio",
                "city_wall": "🧱 Mura / Staccionata / Terrapieno",
            }

            for idx, vname in enumerate(all_known):
                old_cfg = cur_acc.villages.get(vname, VillageConfig(name=vname))
                with st.expander(f"🏰 Configurazione per '{vname}'", expanded=True):
                    r_col1, r_col2 = st.columns([1, 1])
                    with r_col1:
                        cur_role_val = old_cfg.role if old_cfg.role in role_choices else "standard"
                        new_role = st.selectbox(
                            f"Ruolo Strategico ({vname}):",
                            role_choices,
                            index=role_choices.index(cur_role_val),
                            format_func=lambda r: role_labels.get(r, r),
                            key=f"role_{cur_acc.account_id}_{idx}",
                        )
                    with r_col2:
                        cur_target = old_cfg.target_village if old_cfg.target_village in target_options else "(Nessuno)"
                        new_target = st.selectbox(
                            f"Villaggio Target Spedizione (per Feeder):",
                            target_options,
                            index=target_options.index(cur_target) if cur_target in target_options else 0,
                            key=f"target_{cur_acc.account_id}_{idx}",
                        )
                        assigned_target = "" if new_target == "(Nessuno)" else new_target

                    u_col1, u_col2, u_col3 = st.columns([2, 2, 1])
                    with u_col1:
                        # Opzioni = nomi unita' delle tre tribu' (dedup, ordine
                        # della mappa) + eventuali valori legacy gia' salvati
                        unit_options = list(dict.fromkeys(STATIC_TROOP_LABELS.values()))
                        for _u in old_cfg.allowed_units:
                            if _u not in unit_options:
                                unit_options.append(_u)
                        parsed_units = st.multiselect(
                            "Unità Addestrabili Consentite:",
                            options=unit_options,
                            default=[u for u in old_cfg.allowed_units if u in unit_options],
                            key=f"units_{cur_acc.account_id}_{idx}",
                            help="Ordine = priorita' di addestramento (usato dai ruoli "
                                 "hammer/defensive). Vuoto = nessun filtro.",
                        )
                    with u_col2:
                        # Le opzioni includono eventuali chiavi legacy gia'
                        # salvate e non presenti nella mappa (retrocompat).
                        focus_options = list(building_focus_labels.keys())
                        for _f in old_cfg.building_focus:
                            if _f not in focus_options:
                                focus_options.append(_f)
                        parsed_focus = st.multiselect(
                            "Edifici Prioritari:",
                            options=focus_options,
                            default=[f for f in old_cfg.building_focus if f in focus_options],
                            format_func=lambda k: building_focus_labels.get(k, k),
                            key=f"focus_{cur_acc.account_id}_{idx}",
                            help="Edifici a cui dare priorita' di upgrade in questo villaggio",
                        )
                    with u_col3:
                        parsed_market_slot = st.number_input(
                            "Slot Mercato:",
                            min_value=0,
                            max_value=40,
                            value=int(old_cfg.market_slot or 0),
                            step=1,
                            key=f"market_slot_{cur_acc.account_id}_{idx}",
                            help="Slot dorf2 del Mercato (19-40). "
                                 "0 = rilevamento automatico via gid=17.",
                        )

                    updated_villages[vname] = VillageConfig(
                        name=vname,
                        role=new_role,
                        target_village=assigned_target,
                        allowed_units=parsed_units,
                        building_focus=parsed_focus,
                        farm_enabled=old_cfg.farm_enabled,
                        farm_unit=old_cfg.farm_unit,
                        market_slot=int(parsed_market_slot),
                    )

            save_submitted = st.form_submit_button("💾 Salva Ruoli & Strategia (Applicazione a Caldo)", type="primary")
            if save_submitted:
                cur_acc.strategy_profile = new_strat
                cur_acc.villages = updated_villages
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
                    vc1, vc2 = st.columns(2)
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

            slots_text = "\n".join(
                f"{t.x},{t.y},{t.note}" for t in mod_acc.expansion.target_slots
            )
            targets_raw = st.text_area(
                "Tile target (uno per riga: x,y,nota):",
                value=slots_text,
                height=120,
                help="Es: -15,72,Futuro Croplog 15c — coordinate verificate sulla mappa.",
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
                scout_model_opts = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"]
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

            mod_saved = st.form_submit_button(
                "💾 Salva Configurazione Farming, Espansione & AI Scout", type="primary"
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
                sel_unit = st.session_state[f"troop_type_{mod_acc.account_id}_{idx}"]
                vcfg.farm_unit = "" if sel_unit == unit_choices[0] else sel_unit
                mod_acc.villages[vname] = vcfg

            # Espansione
            mod_acc.expansion.auto_expand = exp_enabled
            mod_acc.expansion.founder_village = (
                "" if founder_sel.startswith("(auto") else founder_sel
            )
            mod_acc.expansion.crop_min = int(crop_min_sel)
            parsed_targets: list[ExpansionTarget] = []
            for line in targets_raw.splitlines():
                parts = [p.strip() for p in line.replace("|", ",").split(",")]
                if len(parts) >= 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
                    parsed_targets.append(
                        ExpansionTarget(
                            x=int(parts[0]),
                            y=int(parts[1]),
                            note=parts[2] if len(parts) > 2 else "",
                        )
                    )
            mod_acc.expansion.target_slots = parsed_targets

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

            save_accounts(accounts)
            st.success(
                "Parametri di farming, espansione, AI Scout ed eroe aggiornati e salvati su "
                "accounts.json! L'agente li applicherà al prossimo ciclo."
            )
            time.sleep(1)
            st.rerun()


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
                    new_sett = Settings(
                        account_id=new_acc_id.strip() or f"acc_{len(accounts)+1}",
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
                        save_accounts(remaining)
                        if acc.state_file and acc.state_file.exists():
                            acc.state_file.unlink(missing_ok=True)
                        st.toast(f"Account {acc.account_id} rimosso.")
                        time.sleep(0.5)
                        st.rerun()
    else:
        st.info("Nessun account presente.")


# 3. TAB LOG
with tab_logs:
    st.subheader("Log di Esecuzione (Tutti gli account)")
    c_log1, c_log2 = st.columns([1, 4])
    with c_log1:
        auto_refresh = st.checkbox("Auto-aggiorna log", value=True)
        if st.button("🗑️ Pulisci log"):
            if LOG_FILE.exists():
                LOG_FILE.write_text("", encoding="utf-8")
                st.rerun()

    if LOG_FILE.exists():
        log_content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = log_content.splitlines()[-120:]
        st.code("\n".join(lines) if lines else "Nessun log disponibile.", language="text")
    else:
        st.info("Nessun log recente.")

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
