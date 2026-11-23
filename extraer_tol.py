#!/usr/bin/env python3
"""
Extractor automático de Tennis Open League (EpicenterSport API v1)
==================================================================
Lee credenciales desde variables de entorno (GitHub Secrets):
  - TOL_EMAIL
  - TOL_PASSWORD

Descarga automáticamente:
  1. Ligas (temporada actual y anteriores si está activo en config.json)
  2. Equipos de la categoría configurada (ej. 6ta Masculino) y sus rosters con cédula (ced_id)
  3. Grupos y tabla de posiciones
  4. Jornadas y playoffs completados con detalle de dobles (D1-D5) y sets
  5. Ranking ELO por jugador

Genera `data/tol_snapshot.json` listo para `generar_portal.py`.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_API = "https://api.epicentersport.com/api/v1"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.epicentersport.com",
    "Referer": "https://www.epicentersport.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoutingBot/1.0",
}


class TOLClient:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.token: Optional[str] = None
        self.doubles_url_mode: Optional[str] = None

    def _request(self, method: str, path_or_url: str, payload: Optional[Dict] = None, allow_404: bool = False) -> Optional[Dict[str, Any]]:
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_API}/{path_or_url.lstrip('/')}"
        headers = dict(DEFAULT_HEADERS)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if allow_404 and e.code in (404, 405, 422):
                return None
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} en {method} {url}: {body}") from e

    def login(self) -> None:
        resp = self._request("POST", "login", {"email": self.email, "password": self.password})
        token = None
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict):
                token = data.get("token") or data.get("access_token")
            token = token or resp.get("token") or resp.get("access_token")
        if not token:
            keys = list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
            raise RuntimeError(f"Login exitoso pero no se encontró el token en la respuesta (keys={keys}).")
        self.token = str(token)
        print("[OK] Sesión iniciada en EpicenterSport API.")

    @staticmethod
    def unwrap_list(resp: Any) -> List[Dict[str, Any]]:
        """Extrae listas desde respuestas estándar o paginadas de Laravel (data / data.data)."""
        if isinstance(resp, list):
            return resp
        if not isinstance(resp, dict):
            return []
        d = resp.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("data", "items", "results", "records", "doubles", "players", "teams", "matches"):
                if isinstance(d.get(k), list):
                    return d[k]
        for k in ("items", "results", "doubles", "players", "teams", "matches"):
            if isinstance(resp.get(k), list):
                return resp[k]
        return []

    def get_paginated(self, path: str, params: Dict[str, Any], per_page: int = 50, max_pages: int = 25) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            q = dict(params)
            q["page"] = page
            q["per_page"] = per_page
            qs = urllib.parse.urlencode(q)
            resp = self._request("GET", f"{path}?{qs}")
            batch = self.unwrap_list(resp)
            if not batch:
                break
            results.extend(batch)
            # Revisar si hay más páginas en paginación Laravel
            data_obj = resp.get("data") if isinstance(resp, dict) else {}
            if isinstance(data_obj, dict):
                cur_p = data_obj.get("current_page")
                last_p = data_obj.get("last_page")
                if cur_p is not None and last_p is not None and int(cur_p) >= int(last_p):
                    break
                if "next_page_url" in data_obj and not data_obj.get("next_page_url"):
                    break
            if len(batch) < per_page:
                break
            time.sleep(0.1)
        return results

    def fetch_match_doubles(self, match_id: str) -> List[Dict[str, Any]]:
        """Detecta automáticamente el endpoint exacto de dobles de un partido y lo reutiliza."""
        candidates = [
            ("league_matches_doubles", f"league-matches/{match_id}/doubles"),
            ("matches_doubles", f"matches/{match_id}/doubles"),
            ("league_match_doubles", f"league-match/{match_id}/doubles"),
            ("doubles_by_lm", f"doubles?league_match_id={match_id}"),
            ("doubles_by_m", f"doubles?match_id={match_id}"),
            ("league_match_detail", f"league-match/?league_match_id={match_id}"),
        ]
        # Si ya sabemos cuál funcionó, probarlo primero
        if self.doubles_url_mode:
            candidates.sort(key=lambda x: 0 if x[0] == self.doubles_url_mode else 1)

        for mode, url in candidates:
            resp = self._request("GET", url, allow_404=True)
            if resp is None:
                continue
            items = self.unwrap_list(resp)
            if not items and isinstance(resp, dict):
                d = resp.get("data")
                if isinstance(d, dict) and isinstance(d.get("doubles"), list):
                    items = d["doubles"]
            if items:
                if self.doubles_url_mode != mode:
                    self.doubles_url_mode = mode
                    print(f"[INFO] Endpoint de dobles detectado: {mode}")
                return items
        return []

    def fetch_team_players(self, team_id: str) -> List[Dict[str, Any]]:
        resp = self._request("GET", f"teams/{team_id}/players", allow_404=True)
        return self.unwrap_list(resp) if resp else []

    def fetch_player_profile(self, player_id: str) -> Dict[str, Any]:
        for path in (f"players/{player_id}?rankings=true&teams=true", f"users/{player_id}?rankings=true&teams=true"):
            resp = self._request("GET", path, allow_404=True)
            if isinstance(resp, dict) and resp:
                return resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return {}


# ==============================================================================
# PARSERS FLEXIBLES PARA LA ESTRUCTURA DE EPICENTERSPORT
# ==============================================================================

def pick(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(obj, dict) and k in obj and obj[k] is not None:
            return obj[k]
    return default


def extract_player_name(p: Dict[str, Any]) -> str:
    if not isinstance(p, dict):
        return str(p or "").strip()
    inner = p.get("player") or p.get("user") or p
    if isinstance(inner, dict):
        full = pick(inner, "full_name", "name")
        if full:
            return str(full).strip()
        fn = str(inner.get("first_name") or "").strip()
        ln = str(inner.get("last_name") or "").strip()
        if fn or ln:
            return f"{fn} {ln}".strip()
        return str(inner.get("username") or "").strip()
    return str(inner).strip()


def extract_player_id(p: Dict[str, Any]) -> str:
    if not isinstance(p, dict):
        return ""
    inner = p.get("player") or p.get("user") or p
    if isinstance(inner, dict):
        return str(inner.get("id") or p.get("user_id") or p.get("player_id") or "")
    return ""


def parse_sets_from_double(dbl: Dict[str, Any], perspective_is_local: bool) -> Tuple[List[List[int]], bool]:
    """Extrae los marcadores de sets [nuestros, rival] y detecta Walkover."""
    wo = bool(pick(dbl, "walkover", "is_walkover", "wo", default=False))
    status = str(dbl.get("status") or "").lower()
    if "walkover" in status or status == "wo":
        wo = True

    raw_sets = pick(dbl, "sets", "scores", "match_sets", "double_sets", default=[])
    parsed: List[List[int]] = []

    if isinstance(raw_sets, list):
        for s in raw_sets:
            if not isinstance(s, dict):
                continue
            loc = pick(s, "local_score", "local_games", "home_score", "home_games", "score_local", "local", "team1_score", "team_a_score")
            vis = pick(s, "visitor_score", "visitor_games", "away_score", "away_games", "score_visitor", "visitor", "away", "team2_score", "team_b_score")
            if loc is None or vis is None:
                continue
            try:
                li, vi = int(loc), int(vis)
            except (TypeError, ValueError):
                continue
            parsed.append([li, vi] if perspective_is_local else [vi, li])

    # Fallback: sets planos en columnas set1_local / set1_visitor
    if not parsed:
        for idx in (1, 2, 3):
            loc = pick(dbl, f"set{idx}_local", f"s{idx}_local", f"local_set_{idx}")
            vis = pick(dbl, f"set{idx}_visitor", f"s{idx}_visitor", f"visitor_set_{idx}", f"set{idx}_away")
            if loc is not None and vis is not None:
                try:
                    li, vi = int(loc), int(vis)
                    parsed.append([li, vi] if perspective_is_local else [vi, li])
                except (TypeError, ValueError):
                    pass

    return parsed, wo


def parse_double_participants(dbl: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """Devuelve (local_names, visitor_names, all_player_ids)."""
    local_list = pick(dbl, "local_participants", "home_participants", "local_players")
    visitor_list = pick(dbl, "visitor_participants", "away_participants", "visitor_players")
    all_parts = pick(dbl, "participants", "players", default=[])

    if not local_list and not visitor_list and isinstance(all_parts, list):
        local_list = [p for p in all_parts if isinstance(p, dict) and p.get("is_local") is True]
        visitor_list = [p for p in all_parts if isinstance(p, dict) and p.get("is_local") is False]

    local_list = local_list or []
    visitor_list = visitor_list or []

    local_names = [extract_player_name(p) for p in local_list if extract_player_name(p)]
    visitor_names = [extract_player_name(p) for p in visitor_list if extract_player_name(p)]
    pids = [extract_player_id(p) for p in (local_list + visitor_list) if extract_player_id(p)]

    while len(local_names) < 2:
        local_names.append("Por definir")
    while len(visitor_names) < 2:
        visitor_names.append("Por definir")
    return local_names[:2], visitor_names[:2], pids


def determine_double_winner(dbl: Dict[str, Any], sets_from_local: List[List[int]], local_team_id: str, visitor_team_id: str) -> Optional[str]:
    """Devuelve 'local' o 'visitor' según el campo ganador o contando los sets."""
    winner_id = str(pick(dbl, "winner_team_id", "winner_id") or "")
    if winner_id:
        if winner_id == local_team_id:
            return "local"
        if winner_id == visitor_team_id:
            return "visitor"

    winner_side = str(pick(dbl, "winner_side", "winner") or "").lower()
    if winner_side in ("local", "home", "team1", "a"):
        return "local"
    if winner_side in ("visitor", "away", "team2", "b"):
        return "visitor"

    if sets_from_local:
        lw = sum(1 for a, b in sets_from_local if a > b)
        vw = sum(1 for a, b in sets_from_local if b > a)
        if lw > vw:
            return "local"
        if vw > lw:
            return "visitor"
    return None


def parse_standings_from_groups(groups_raw: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Extrae las tablas de posiciones por grupo (ej. Grupo A, Grupo B)."""
    tables: Dict[str, List[Dict[str, Any]]] = {}
    for g in groups_raw:
        gname = str(pick(g, "name", "group_name", default="Grupo")).strip()
        rows_raw = pick(g, "standings", "teams", "positions", "rankings", "group_teams", default=[])
        parsed_rows = []
        for idx, r in enumerate(rows_raw, start=1):
            if not isinstance(r, dict):
                continue
            team_obj = r.get("team") if isinstance(r.get("team"), dict) else r
            tname = str(pick(team_obj, "name", "team_name", default="Equipo")).strip()
            pts = pick(r, "points", "pts", "total_points", default=0)
            set_ave = pick(r, "set_average", "set_ave", "sets_percentage", "set_pct", default=0.0)
            game_ave = pick(r, "game_average", "game_ave", "games_percentage", "game_pct", default=0.0)
            pos = pick(r, "position", "pos", "rank", default=idx)
            # Normalizar si viene como decimal 0.657 en vez de 65.7
            try:
                sa_f = float(set_ave or 0)
                if 0 < sa_f <= 1.0:
                    sa_f *= 100.0
            except (TypeError, ValueError):
                sa_f = 0.0
            try:
                ga_f = float(game_ave or 0)
                if 0 < ga_f <= 1.0:
                    ga_f *= 100.0
            except (TypeError, ValueError):
                ga_f = 0.0

            parsed_rows.append({
                "pos": int(pos or idx),
                "team": tname,
                "team_id": str(pick(team_obj, "id", default=pick(r, "team_id", default=""))),
                "pts": int(float(pts or 0)),
                "set_ave": round(sa_f, 1),
                "game_ave": round(ga_f, 1),
            })
        parsed_rows.sort(key=lambda x: (x["pos"], -x["pts"]))
        tables[gname] = parsed_rows
    return tables


# ==============================================================================
# FLUJO PRINCIPAL DE EXTRACCIÓN
# ==============================================================================

def run_extraction(config_path: Path, output_path: Path) -> None:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    email = os.environ.get("TOL_EMAIL", "").strip()
    password = os.environ.get("TOL_PASSWORD", "").strip()

    if not email or not password:
        print("[ERROR] Faltan las variables de entorno TOL_EMAIL y/o TOL_PASSWORD.")
        print("        Configúralas en GitHub -> Settings -> Secrets and variables -> Actions.")
        sys.exit(1)

    client = TOLClient(email, password)
    client.login()

    # 1. Identificar ligas y categorías (actual + temporadas previas de la misma categoría)
    leagues_resp = client._request("GET", "leagues/?include_finalized=true")
    leagues = client.unwrap_list(leagues_resp)
    print(f"[INFO] Ligas encontradas: {len(leagues)}")

    league_kw = cfg.get("league_keyword", "Tennis Open League").lower()
    cat_target = cfg.get("category_name", "6ta Masculino").lower()
    current_cat_id = cfg.get("current_category_id", "")

    target_seasons: List[Dict[str, Any]] = []
    for lg in leagues:
        lg_name = str(pick(lg, "name", "title", default="")).strip()
        lg_id = str(lg.get("id") or "")
        if league_kw not in lg_name.lower() and lg_id != cfg.get("current_league_id"):
            continue
        if not cfg.get("include_previous_seasons", True) and lg_id != cfg.get("current_league_id"):
            continue

        cats_resp = client._request("GET", f"categories?league_id={lg_id}", allow_404=True)
        cats = client.unwrap_list(cats_resp)
        for c in cats:
            cname = str(pick(c, "name", "alias", default="")).strip()
            cid = str(c.get("id") or "")
            if cid == current_cat_id or cat_target in cname.lower():
                season_label = str(pick(lg, "season_name", "short_name", "name", default=lg_name)).strip()
                # Abreviar etiquetas largas tipo "Tennis Open League - T2 2026"
                if " - " in season_label:
                    season_label = season_label.split(" - ")[-1].strip()
                target_seasons.append({
                    "league_id": lg_id,
                    "league_name": lg_name,
                    "season_label": season_label,
                    "category_id": cid,
                    "category_name": cname,
                    "elimination_tournament_id": c.get("elimination_tournament_id"),
                    "is_current": (cid == current_cat_id or lg_id == cfg.get("current_league_id")),
                    "start_date": str(c.get("start_date") or lg.get("start_date") or ""),
                })

    if not target_seasons and current_cat_id:
        target_seasons.append({
            "league_id": cfg.get("current_league_id", ""),
            "league_name": cfg.get("league_keyword", "Tennis Open League"),
            "season_label": "Temporada Actual",
            "category_id": current_cat_id,
            "category_name": cfg.get("category_name", "6ta Masculino"),
            "elimination_tournament_id": None,
            "is_current": True,
            "start_date": "",
        })

    target_seasons.sort(key=lambda x: (0 if x["is_current"] else 1, x["start_date"]))
    current_season_obj = target_seasons[0]
    print(f"[INFO] Temporadas objetivo ({len(target_seasons)}): {[s['season_label'] for s in target_seasons]}")

    # 2. Equipos y Grupos de la temporada actual
    cur_cid = current_season_obj["category_id"]
    teams_resp = client._request("GET", f"teams?per_page=200&category_id={cur_cid}")
    teams_raw = client.unwrap_list(teams_resp)
    print(f"[INFO] Equipos en {current_season_obj['category_name']}: {len(teams_raw)}")

    groups_resp = client._request("GET", f"groups?category_id={cur_cid}", allow_404=True)
    groups_raw = client.unwrap_list(groups_resp) if groups_resp else []
    standings_by_group = parse_standings_from_groups(groups_raw)
    if groups_raw:
        print(f"[DEBUG] Keys de groups[0]: {list(groups_raw[0].keys())}")

    # 3. Rosters por equipo (con cédula ced_id)
    rosters_by_team: Dict[str, List[Dict[str, Any]]] = {}
    all_player_ids: set = set()
    teams_meta: Dict[str, Dict[str, Any]] = {}

    for t in teams_raw:
        tid = str(t.get("id") or "")
        tname = str(pick(t, "name", "team_name", default="Equipo")).strip()
        club = str(pick(t, "venue_name", "club_name", "location", default="Sede TOL"))
        if isinstance(t.get("venue"), dict):
            club = str(pick(t["venue"], "name", "address", default=club))
        teams_meta[tname] = {"id": tid, "name": tname, "club": club}

        raw_players = client.fetch_team_players(tid)
        roster_clean = []
        for item in raw_players:
            u = item.get("user") if isinstance(item.get("user"), dict) else item
            pid = str(pick(u, "id", default=item.get("user_id") or ""))
            pname = extract_player_name(u)
            ced = str(pick(u, "ced_id", "cedula", "document", "username", default="—"))
            if pid:
                all_player_ids.add(pid)
            roster_clean.append({
                "id": pid,
                "name": pname,
                "cedula": ced,
                "elo": 0.0,
                "tactical_note": "",
            })
        rosters_by_team[tname] = roster_clean
        time.sleep(0.05)

    # 4. Partidos completados de todas las temporadas objetivo (fase regular + playoffs)
    all_raw_matches: List[Dict[str, Any]] = []
    seen_match_ids: set = set()
    logged_double_keys = False

    for s_obj in target_seasons:
        cid = s_obj["category_id"]
        s_label = s_obj["season_label"]
        matches_batch = client.get_paginated(
            "matches",
            {"status": "completed", "type": "league", "category_id": cid},
            per_page=50,
        )
        # También consultar eliminatorias si existen en tournament separado
        elim_id = s_obj.get("elimination_tournament_id")
        if elim_id:
            try:
                elim_batch = client.get_paginated(
                    "matches",
                    {"status": "completed", "type": "tournament", "tournament_id": elim_id},
                    per_page=50,
                )
                matches_batch.extend(elim_batch)
            except Exception:
                pass

        print(f"[INFO] {s_label}: {len(matches_batch)} encuentros descargados.")
        for m in matches_batch:
            mid = str(m.get("id") or "")
            if not mid or mid in seen_match_ids:
                continue
            seen_match_ids.add(mid)

            loc_obj = m.get("local_team") or m.get("home_team") or {}
            vis_obj = m.get("visitor_team") or m.get("away_team") or {}
            loc_name = str(pick(loc_obj, "name", default=pick(m, "local_team_name", default="Local"))).strip()
            vis_name = str(pick(vis_obj, "name", default=pick(m, "visitor_team_name", default="Visitante"))).strip()
            loc_id = str(pick(loc_obj, "id", default=pick(m, "local_team_id", default="")))
            vis_id = str(pick(vis_obj, "id", default=pick(m, "visitor_team_id", default="")))

            raw_date = str(pick(m, "date", "scheduled_at", "match_date", "played_at", "created_at", default=""))[:10]
            round_lbl = str(pick(m, "round_name", "phase_name", "stage", default=""))
            j_num = pick(m, "matchday", "round", "jornada")
            if not round_lbl:
                round_lbl = f"Jornada {j_num}" if j_num else "Jornada"
            phase_lbl = str(pick(m, "phase", "stage_name", default="Playoffs" if "final" in round_lbl.lower() or "cuarto" in round_lbl.lower() or "octavo" in round_lbl.lower() else "Fase Regular"))

            loc_score = int(float(pick(m, "local_score", "home_score", "local_points", default=0) or 0))
            vis_score = int(float(pick(m, "visitor_score", "away_score", "visitor_points", default=0) or 0))

            # Traer detalle de dobles
            doubles_raw = client.fetch_match_doubles(mid)
            if doubles_raw and not logged_double_keys:
                print(f"[DEBUG] Keys de doubles[0]: {list(doubles_raw[0].keys())}")
                logged_double_keys = True

            doubles_norm = []
            for idx, dbl in enumerate(doubles_raw, start=1):
                l_names, v_names, pids = parse_double_participants(dbl)
                all_player_ids.update(pids)
                sets_local, wo = parse_sets_from_double(dbl, perspective_is_local=True)
                winner_side = determine_double_winner(dbl, sets_local, loc_id, vis_id)
                d_num = pick(dbl, "number", "order", "position", "double_number", default=idx)
                pos_str = f"D{d_num}" if str(d_num).isdigit() else str(d_num)

                doubles_norm.append({
                    "pos": pos_str,
                    "local_players": l_names,
                    "visitor_players": v_names,
                    "sets_local": sets_local,
                    "walkover": wo,
                    "winner_side": winner_side,
                })

            # Si el marcador global vino en 0-0 pero hay dobles con ganador, calcularlo
            if loc_score == 0 and vis_score == 0 and doubles_norm:
                loc_score = sum(1 for d in doubles_norm if d["winner_side"] == "local")
                vis_score = sum(1 for d in doubles_norm if d["winner_side"] == "visitor")

            all_raw_matches.append({
                "id": mid,
                "season": s_label,
                "phase": phase_lbl,
                "round": round_lbl,
                "date": raw_date or "2026-01-01",
                "local_team": loc_name,
                "visitor_team": vis_name,
                "local_score": loc_score,
                "visitor_score": vis_score,
                "doubles": doubles_norm,
            })
            time.sleep(0.05)

    # 5. Enriquecer ELO de cada jugador del roster usando /players/{id}?rankings=true&teams=true
    print(f"[INFO] Consultando ELO de {len(all_player_ids)} jugadores...")
    elo_by_player_id: Dict[str, float] = {}
    for pid in sorted(all_player_ids):
        prof = client.fetch_player_profile(pid)
        if not prof:
            continue
        rankings = prof.get("rankings") or []
        best_elo = 0.0
        for rk in rankings:
            if str(rk.get("category_id") or "") == cur_cid:
                best_elo = float(rk.get("elo_points") or 0.0)
                break
        if best_elo == 0.0 and rankings:
            # Fallback: tomar el primer ranking de división 6 o el primero disponible
            for rk in rankings:
                if str(rk.get("division") or "") == "6":
                    best_elo = float(rk.get("elo_points") or 0.0)
                    break
            if best_elo == 0.0:
                best_elo = float(rankings[0].get("elo_points") or 0.0)
        elo_by_player_id[pid] = best_elo
        time.sleep(0.03)

    # Inyectar ELOs en los rosters
    for tname, rlist in rosters_by_team.items():
        for p in rlist:
            if p["id"] in elo_by_player_id:
                p["elo"] = elo_by_player_id[p["id"]]

    snapshot = {
        "extracted_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "current_season": current_season_obj["season_label"],
        "category_name": current_season_obj["category_name"],
        "teams_meta": teams_meta,
        "rosters_by_team": rosters_by_team,
        "standings_by_group": standings_by_group,
        "matches": all_raw_matches,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Snapshot guardado en {output_path} ({len(teams_meta)} equipos, {len(all_raw_matches)} encuentros).")


if __name__ == "__main__":
    cfg_p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.json")
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/tol_snapshot.json")
    run_extraction(cfg_p, out_p)
