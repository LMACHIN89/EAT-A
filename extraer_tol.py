#!/usr/bin/env python3
"""
Extractor automático de Tennis Open League (EpicenterSport API v1) — v4
=======================================================================
Novedades v4:
  - Detecta TODAS las temporadas de tu categoría (ej. T2 2026, T1 2026, etc.) usando
    tanto el nombre como division + gender (ej. division=6, gender="male").
  - Descarga grupos y tablas de posiciones POR TEMPORADA (`standings_by_season`).
  - Descarga partidos completados y playoffs (jugados, en curso y programados)
    separados limpiamente por temporada para el filtro global del portal.
"""

import json
import os
import re
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoutingBot/4.0",
}


class TOLClient:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.token: Optional[str] = None
        self.doubles_url_mode: Optional[str] = None
        self.player_url_template: Optional[str] = None

    def _request(
        self,
        method: str,
        path_or_url: str,
        payload: Optional[Dict] = None,
        allow_error: bool = False,
    ) -> Optional[Dict[str, Any]]:
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
            if allow_error and e.code in (400, 403, 404, 405, 422, 500):
                return None
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} en {method} {url}: {body}") from e
        except Exception:
            if allow_error:
                return None
            raise

    def login(self) -> None:
        resp = self._request("POST", "login", {"email": self.email, "password": self.password})
        token = None
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict):
                token = data.get("token") or data.get("access_token")
            token = token or resp.get("token") or resp.get("access_token")
        if not token:
            raise RuntimeError("Login exitoso pero no se encontró el token en la respuesta.")
        self.token = str(token)
        print("[OK] Sesión iniciada en EpicenterSport API.")

    @staticmethod
    def unwrap_list(resp: Any) -> List[Dict[str, Any]]:
        if isinstance(resp, list):
            return resp
        if not isinstance(resp, dict):
            return []
        d = resp.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("data", "items", "results", "records", "doubles", "players", "teams", "matches", "rankings", "standings", "groups", "rounds"):
                if isinstance(d.get(k), list):
                    return d[k]
        for k in ("items", "results", "doubles", "players", "teams", "matches", "rankings", "standings", "groups", "rounds"):
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
            resp = self._request("GET", f"{path}?{qs}", allow_error=True)
            if resp is None:
                break
            batch = self.unwrap_list(resp)
            if not batch:
                break
            results.extend(batch)
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
            time.sleep(0.06)
        return results

    def fetch_categories_for_league(self, league_id: str) -> List[Dict[str, Any]]:
        for path in (
            f"leagues/{league_id}/categories?per_page=100",
            f"leagues/{league_id}/categories",
            f"categories?league_id={league_id}&per_page=100",
        ):
            resp = self._request("GET", path, allow_error=True)
            items = self.unwrap_list(resp)
            if items:
                return items
        return []

    def fetch_all_categories(self) -> List[Dict[str, Any]]:
        return self.get_paginated("categories", {}, per_page=100, max_pages=15)

    def fetch_match_doubles(self, match_id: str) -> List[Dict[str, Any]]:
        candidates = [
            ("league_match_sub_doubles", f"league-match/{match_id}/doubles"),
            ("league_matches_sub_doubles", f"league-matches/{match_id}/doubles"),
            ("matches_sub_doubles", f"matches/{match_id}/doubles"),
            ("league_match_query", f"league-match/?league_match_id={match_id}"),
            ("league_match_id_direct", f"league-match/{match_id}"),
            ("doubles_by_lm", f"doubles?league_match_id={match_id}"),
            ("doubles_by_m", f"doubles?match_id={match_id}"),
        ]
        if self.doubles_url_mode:
            candidates.sort(key=lambda x: 0 if x[0] == self.doubles_url_mode else 1)

        for mode, url in candidates:
            resp = self._request("GET", url, allow_error=True)
            if resp is None:
                continue
            items = self.unwrap_list(resp)
            if not items and isinstance(resp, dict):
                d = resp.get("data")
                if isinstance(d, dict):
                    for k in ("doubles", "match_doubles", "league_doubles", "games"):
                        if isinstance(d.get(k), list) and d[k]:
                            items = d[k]
                            break
            if items:
                if self.doubles_url_mode != mode:
                    self.doubles_url_mode = mode
                    print(f"[INFO] Endpoint de dobles detectado: {mode} ({url})")
                return items
        return []

    def fetch_team_players(self, team_id: str) -> List[Dict[str, Any]]:
        resp = self._request("GET", f"teams/{team_id}/players", allow_error=True)
        return self.unwrap_list(resp) if resp else []

    def fetch_bulk_elo(self, category_id: str, league_id: str) -> Dict[str, float]:
        bulk_candidates = [
            f"elo-ranking?category_id={category_id}&per_page=500",
            f"elo-rankings?category_id={category_id}&per_page=500",
            f"rankings?category_id={category_id}&per_page=500",
            f"ranking?category_id={category_id}&per_page=500",
            f"categories/{category_id}/elo-ranking?per_page=500",
            f"categories/{category_id}/rankings?per_page=500",
            f"elo-ranking?league_id={league_id}&category_id={category_id}&per_page=500",
        ]
        for path in bulk_candidates:
            resp = self._request("GET", path, allow_error=True)
            items = self.unwrap_list(resp)
            if not items:
                continue
            elo_map: Dict[str, float] = {}
            for r in items:
                if not isinstance(r, dict):
                    continue
                u = r.get("player") or r.get("user") or r
                pid = str(pick(u, "id", default=pick(r, "player_id", "user_id", default="")))
                elo = pick(r, "elo_points", "elo", "points", "score")
                if pid and elo is not None:
                    try:
                        elo_map[pid] = float(elo)
                    except (TypeError, ValueError):
                        pass
            if elo_map:
                print(f"[INFO] Ranking ELO en bloque desde '{path}' ({len(elo_map)} jugadores).")
                return elo_map
        return {}

    def discover_and_fetch_player_profile(self, player_id: str) -> Dict[str, Any]:
        templates = [
            "players/{id}?rankings=true&teams=true",
            "player/{id}?rankings=true&teams=true",
            "users/{id}?rankings=true&teams=true",
            "profiles/{id}?rankings=true&teams=true",
            "players/profile/{id}?rankings=true&teams=true",
            "public/players/{id}?rankings=true&teams=true",
        ]
        if self.player_url_template == "__NONE__":
            return {}
        if self.player_url_template:
            resp = self._request("GET", self.player_url_template.format(id=player_id), allow_error=True)
            return self._unwrap_profile(resp)

        for tpl in templates:
            resp = self._request("GET", tpl.format(id=player_id), allow_error=True)
            prof = self._unwrap_profile(resp)
            if prof and ("rankings" in prof or "elo_points" in prof):
                self.player_url_template = tpl
                print(f"[INFO] Endpoint de perfil/ELO detectado: {tpl}")
                return prof

        self.player_url_template = "__NONE__"
        print("[WARN] Sin endpoint de perfil individual de ELO disponible.")
        return {}

    @staticmethod
    def _unwrap_profile(resp: Any) -> Dict[str, Any]:
        if not isinstance(resp, dict) or not resp:
            return {}
        d = resp.get("data")
        if isinstance(d, dict):
            if "rankings" in d or "elo_points" in d:
                return d
            for sub in ("player", "user", "profile"):
                if isinstance(d.get(sub), dict):
                    return d[sub]
            return d
        return resp


# ==============================================================================
# UTILIDADES Y PARSERS
# ==============================================================================

def pick(obj: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return default


def extract_player_name(p: Any) -> str:
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


def extract_player_id(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    inner = p.get("player") or p.get("user") or p
    if isinstance(inner, dict):
        return str(inner.get("id") or p.get("user_id") or p.get("player_id") or "")
    return ""


def parse_sets_from_double(dbl: Dict[str, Any], perspective_is_local: bool) -> Tuple[List[List[int]], bool]:
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
            loc = pick(s, "local_score", "local_games", "local_points", "home_score", "home_games", "score_local", "local", "team1_score", "team_a_score", "games_local")
            vis = pick(s, "visitor_score", "visitor_games", "visitor_points", "away_score", "away_games", "score_visitor", "visitor", "away", "team2_score", "team_b_score", "games_visitor")
            if loc is None or vis is None:
                continue
            try:
                li, vi = int(loc), int(vis)
            except (TypeError, ValueError):
                continue
            parsed.append([li, vi] if perspective_is_local else [vi, li])

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
    winner_id = str(pick(dbl, "winner_team_id", "winner_id") or "")
    if winner_id:
        if winner_id == local_team_id:
            return "local"
        if winner_id == visitor_team_id:
            return "visitor"

    winner_side = str(pick(dbl, "winner_side", "winner", "winning_side") or "").lower()
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


def parse_standings_for_category(client: TOLClient, cid: str) -> Dict[str, List[Dict[str, Any]]]:
    """Descarga grupos/tablas de posiciones para una categoría/temporada específica."""
    groups_resp = client._request("GET", f"groups?category_id={cid}", allow_error=True)
    groups_raw = client.unwrap_list(groups_resp) if groups_resp else []
    tables: Dict[str, List[Dict[str, Any]]] = {}

    for spath in (f"standings?category_id={cid}", f"categories/{cid}/standings"):
        sresp = client._request("GET", spath, allow_error=True)
        if sresp:
            s_items = client.unwrap_list(sresp)
            if s_items and isinstance(s_items[0], dict) and any(k in s_items[0] for k in ("standings", "teams", "positions", "rows", "alignments")):
                groups_raw = s_items
                break

    for g in groups_raw:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "")
        gname = str(pick(g, "name", "group_name", default="Grupo")).strip()
        rows_raw = pick(g, "standings", "positions", "rankings", "alignments", "teams", default=[])
        if gid:
            for gpath in (f"groups/{gid}/standings", f"standings?group_id={gid}"):
                gresp = client._request("GET", gpath, allow_error=True)
                g_items = client.unwrap_list(gresp) if gresp else []
                if g_items:
                    rows_raw = g_items
                    break

        parsed_rows = []
        for idx, r in enumerate(rows_raw, start=1):
            if not isinstance(r, dict):
                continue
            team_obj = r.get("team") if isinstance(r.get("team"), dict) else r
            pivot = r.get("pivot") if isinstance(r.get("pivot"), dict) else {}
            stats = r.get("stats") if isinstance(r.get("stats"), dict) else {}

            tname = str(pick(team_obj, "name", "team_name", default="Equipo")).strip()
            pts = pick(r, "points", "pts", "total_points", "score", default=pick(pivot, "points", "pts", default=pick(stats, "points", "pts", default=0)))
            set_ave = pick(r, "set_average", "set_ave", "sets_percentage", "set_pct", "sets_avg", default=pick(pivot, "set_average", "set_ave", default=pick(stats, "set_average", default=0.0)))
            game_ave = pick(r, "game_average", "game_ave", "games_percentage", "game_pct", "games_avg", default=pick(pivot, "game_average", "game_ave", default=pick(stats, "game_average", default=0.0)))
            pos = pick(r, "position", "pos", "rank", default=pick(pivot, "position", default=idx))

            sa_f = float(set_ave or 0)
            if 0 < sa_f <= 1.0:
                sa_f *= 100.0
            ga_f = float(game_ave or 0)
            if 0 < ga_f <= 1.0:
                ga_f *= 100.0

            parsed_rows.append({
                "pos": int(pos or idx),
                "team": tname,
                "team_id": str(pick(team_obj, "id", default=pick(r, "team_id", default=""))),
                "pts": int(float(pts or 0)),
                "set_ave": round(sa_f, 1),
                "game_ave": round(ga_f, 1),
            })
        parsed_rows.sort(key=lambda x: (-x["pts"], -x["set_ave"], -x["game_ave"], x["pos"]))
        for i, row in enumerate(parsed_rows, start=1):
            row["pos"] = i
        tables[gname] = parsed_rows
    return tables


def extract_match_id_and_body(raw_item: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(raw_item, dict):
        return "", {}
    body = raw_item
    for wrap_key in ("league_match", "match", "tournament_match"):
        if isinstance(raw_item.get(wrap_key), dict):
            merged = dict(raw_item)
            merged.update(raw_item[wrap_key])
            body = merged
            break
    mid = str(
        pick(body, "league_match_id", "id", "match_id", "uuid", "tournament_match_id", default="")
        or pick(raw_item, "league_match_id", "id", "match_id", "uuid", default="")
    )
    return mid, body


def detect_playoff_stage(round_lbl: str, phase_lbl: str, j_num: Any, is_elim_source: bool) -> Tuple[bool, str]:
    blob = f"{round_lbl} {phase_lbl}".lower()
    if any(k in blob for k in ("tercer", "3er", "3ro", "third")):
        return True, "3er Lugar"
    if "semi" in blob:
        return True, "Semifinal"
    if "cuarto" in blob or "quarter" in blob:
        return True, "Cuartos"
    if "octavo" in blob or "round of 16" in blob or "dieciseis" in blob:
        return True, "Octavos"
    if "final" in blob:
        return True, "Final"
    if any(k in blob for k in ("playoff", "play off", "play-off", "eliminat", "llave", "bracket")):
        return True, round_lbl or "Playoffs"
    try:
        if j_num is not None and int(j_num) >= 100:
            return True, round_lbl or f"Playoffs (J{j_num})"
    except (TypeError, ValueError):
        pass
    if is_elim_source:
        return True, round_lbl or "Playoffs"
    return False, ""


def category_matches_target(c: Dict[str, Any], cat_target: str, current_cat_id: str) -> bool:
    cid = str(c.get("id") or "")
    if cid and cid == current_cat_id:
        return True
    cname = str(pick(c, "name", "alias", default="")).lower()
    if cat_target and cat_target in cname:
        return True
    # Detección por división y género (ej. "6ta Masculino" -> division=6, gender="male")
    div_match = re.search(r"(\d)", cat_target)
    want_div = int(div_match.group(1)) if div_match else None
    want_gender = "male" if "masc" in cat_target else ("female" if "fem" in cat_target else ("mixed" if "mixt" in cat_target else None))
    c_div = c.get("division")
    c_gen = str(c.get("gender") or "").lower()
    if want_div is not None and c_div is not None and int(c_div) == want_div:
        if want_gender is None or c_gen == want_gender:
            return True
    return False


def format_season_label(lg: Dict[str, Any], used_labels: set) -> str:
    raw = str(pick(lg, "season_name", "short_name", "name", "title", default="Temporada")).strip()
    if " - " in raw:
        label = raw.split(" - ")[-1].strip()
    elif "–" in raw:
        label = raw.split("–")[-1].strip()
    else:
        label = raw
    # Si el nombre es demasiado genérico (ej. "Tennis Open League"), añadir año de inicio
    start = str(lg.get("start_date") or lg.get("created_at") or "")[:4]
    if label.lower() == "tennis open league" and start:
        label = f"TOL {start}"
    base = label
    idx = 2
    while label in used_labels:
        label = f"{base} ({idx})"
        idx += 1
    used_labels.add(label)
    return label


# ==============================================================================
# FLUJO PRINCIPAL
# ==============================================================================

def run_extraction(config_path: Path, output_path: Path) -> None:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    email = os.environ.get("TOL_EMAIL", "").strip()
    password = os.environ.get("TOL_PASSWORD", "").strip()

    if not email or not password:
        print("[ERROR] Faltan TOL_EMAIL y/o TOL_PASSWORD en GitHub Secrets.")
        sys.exit(1)

    client = TOLClient(email, password)
    client.login()

    # 1. Ligas y todas las temporadas de la categoría
    leagues_resp = client._request("GET", "leagues/?include_finalized=true")
    leagues = client.unwrap_list(leagues_resp)
    print(f"[INFO] Ligas encontradas: {len(leagues)}")

    league_kw = cfg.get("league_keyword", "Tennis Open League").lower()
    cat_target = cfg.get("category_name", "6ta Masculino").lower()
    current_cat_id = cfg.get("current_category_id", "")
    current_league_id = cfg.get("current_league_id", "")

    all_cats_cache = client.fetch_all_categories()
    print(f"[INFO] Categorías globales cargadas: {len(all_cats_cache)}")

    target_seasons: List[Dict[str, Any]] = []
    seen_cids: set = set()
    used_labels: set = set()

    for lg in leagues:
        lg_name = str(pick(lg, "name", "title", default="")).strip()
        lg_id = str(lg.get("id") or "")
        if league_kw not in lg_name.lower() and lg_id != current_league_id:
            continue
        if not cfg.get("include_previous_seasons", True) and lg_id != current_league_id:
            continue

        cats = [c for c in all_cats_cache if str(c.get("league_id") or "") == lg_id]
        if not cats:
            cats = client.fetch_categories_for_league(lg_id)

        for c in cats:
            cid = str(c.get("id") or "")
            if not cid or cid in seen_cids:
                continue
            if category_matches_target(c, cat_target, current_cat_id):
                seen_cids.add(cid)
                s_lbl = format_season_label(lg, used_labels)
                target_seasons.append({
                    "league_id": lg_id,
                    "league_name": lg_name,
                    "season_label": s_lbl,
                    "category_id": cid,
                    "category_name": str(pick(c, "name", "alias", default=cfg.get("category_name", "6ta Masculino"))).strip(),
                    "elimination_tournament_id": c.get("elimination_tournament_id"),
                    "is_current": (cid == current_cat_id or lg_id == current_league_id),
                    "start_date": str(c.get("start_date") or lg.get("start_date") or lg.get("created_at") or ""),
                })

    if not target_seasons and current_cat_id:
        cur_lg = next((l for l in leagues if str(l.get("id")) == current_league_id), {})
        s_lbl = format_season_label(cur_lg, used_labels) if cur_lg else "T2 2026"
        target_seasons.append({
            "league_id": current_league_id,
            "league_name": cfg.get("league_keyword", "Tennis Open League"),
            "season_label": s_lbl,
            "category_id": current_cat_id,
            "category_name": cfg.get("category_name", "6ta Masculino"),
            "elimination_tournament_id": None,
            "is_current": True,
            "start_date": "",
        })

    # Ordenar: temporada actual primero, luego las demás de más reciente a más antigua
    target_seasons.sort(key=lambda x: (0 if x["is_current"] else 1, x["start_date"]), reverse=False)
    # Reordenar las no actuales de más reciente a más antigua
    cur_list = [s for s in target_seasons if s["is_current"]]
    past_list = sorted([s for s in target_seasons if not s["is_current"]], key=lambda x: x["start_date"], reverse=True)
    target_seasons = cur_list + past_list

    current_season_obj = target_seasons[0]
    seasons_order = [s["season_label"] for s in target_seasons]
    print(f"[INFO] Temporadas detectadas ({len(seasons_order)}): {seasons_order}")

    # 2. Equipos, Rosters y Grupos POR TEMPORADA
    standings_by_season: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    rosters_by_team: Dict[str, List[Dict[str, Any]]] = {}
    teams_meta: Dict[str, Dict[str, Any]] = {}
    all_player_ids: set = set()

    for s_obj in target_seasons:
        s_lbl = s_obj["season_label"]
        cid = s_obj["category_id"]
        standings_by_season[s_lbl] = parse_standings_for_category(client, cid)

        teams_resp = client._request("GET", f"teams?per_page=200&category_id={cid}", allow_error=True)
        teams_raw = client.unwrap_list(teams_resp) if teams_resp else []
        print(f"[INFO] {s_lbl}: {len(teams_raw)} equipos, {len(standings_by_season[s_lbl])} grupos.")

        for t in teams_raw:
            tid = str(t.get("id") or "")
            tname = str(pick(t, "name", "team_name", default="Equipo")).strip()
            club = str(pick(t, "venue_name", "club_name", "location", default="Sede TOL"))
            if isinstance(t.get("venue"), dict):
                club = str(pick(t["venue"], "name", "address", default=club))

            if tname not in teams_meta:
                teams_meta[tname] = {"id": tid, "name": tname, "club": club, "seasons": []}
            if s_lbl not in teams_meta[tname]["seasons"]:
                teams_meta[tname]["seasons"].append(s_lbl)

            # Cargar roster (combinando jugadores si el equipo jugó en múltiples temporadas)
            raw_players = client.fetch_team_players(tid)
            existing_by_name = {p["name"].lower(): p for p in rosters_by_team.get(tname, [])}
            for item in raw_players:
                u = item.get("user") if isinstance(item.get("user"), dict) else item
                pid = str(pick(u, "id", default=item.get("user_id") or ""))
                pname = extract_player_name(u)
                ced = str(pick(u, "ced_id", "cedula", "document", "username", default="—"))
                if pid:
                    all_player_ids.add(pid)
                if pname.lower() not in existing_by_name:
                    existing_by_name[pname.lower()] = {
                        "id": pid,
                        "name": pname,
                        "cedula": ced,
                        "elo": 0.0,
                        "seasons": [s_lbl],
                        "tactical_note": "",
                    }
                else:
                    if s_lbl not in existing_by_name[pname.lower()].setdefault("seasons", []):
                        existing_by_name[pname.lower()]["seasons"].append(s_lbl)
            rosters_by_team[tname] = list(existing_by_name.values())
            time.sleep(0.02)

    # 3. Partidos completados + playoffs próximos de TODAS las temporadas
    all_raw_matches: List[Dict[str, Any]] = []
    upcoming_playoffs: List[Dict[str, Any]] = []
    seen_match_ids: set = set()
    seen_upcoming_ids: set = set()

    for s_obj in target_seasons:
        cid = s_obj["category_id"]
        s_label = s_obj["season_label"]
        is_cur = s_obj["is_current"]

        completed_batch: List[Tuple[Dict[str, Any], bool]] = [
            (m, False)
            for m in client.get_paginated("matches", {"status": "completed", "type": "league", "category_id": cid}, per_page=50)
        ]
        elim_id = s_obj.get("elimination_tournament_id")
        if elim_id:
            for m in client.get_paginated("matches", {"status": "completed", "type": "tournament", "tournament_id": elim_id}, per_page=50):
                completed_batch.append((m, True))

        print(f"[INFO] {s_label}: {len(completed_batch)} encuentros completados descargados.")

        for raw_m, is_elim_src in completed_batch:
            mid, m = extract_match_id_and_body(raw_m)
            if not mid or mid in seen_match_ids:
                continue
            seen_match_ids.add(mid)

            loc_obj = pick(m, "local_team", "home_team", "team_local", "team1", default={})
            vis_obj = pick(m, "visitor_team", "away_team", "team_visitor", "team2", default={})
            loc_name = str(pick(loc_obj, "name", "team_name", default=pick(m, "local_team_name", "home_team_name", default="Local"))).strip()
            vis_name = str(pick(vis_obj, "name", "team_name", default=pick(m, "visitor_team_name", "away_team_name", default="Visitante"))).strip()
            loc_id = str(pick(loc_obj, "id", default=pick(m, "local_team_id", "home_team_id", default="")))
            vis_id = str(pick(vis_obj, "id", default=pick(m, "visitor_team_id", "away_team_id", default="")))

            raw_date = str(pick(m, "date", "scheduled_at", "match_date", "played_at", "start_date", "created_at", default=""))[:10]
            j_num = pick(m, "journey_number", "matchday", "round", "jornada")
            round_lbl = str(pick(m, "round_name", "phase_name", "stage", "journey_name", default=""))
            if not round_lbl:
                if isinstance(m.get("journey"), dict):
                    round_lbl = str(pick(m["journey"], "name", default=""))
                    if not round_lbl and m["journey"].get("number"):
                        j_num = m["journey"]["number"]
                        round_lbl = f"Jornada {j_num}"
                if not round_lbl:
                    round_lbl = f"Jornada {j_num}" if j_num else "Jornada"

            phase_lbl = str(pick(m, "phase", "stage_name", "match_type", default=""))
            is_playoff, bracket_stage = detect_playoff_stage(round_lbl, phase_lbl, j_num, is_elim_src)
            if not phase_lbl:
                phase_lbl = f"Playoffs · {bracket_stage}" if is_playoff else "Fase Regular"

            venue_name = m["venue"].get("name", "") if isinstance(m.get("venue"), dict) else str(pick(m, "venue_name", "location", default=""))
            loc_score = int(float(pick(m, "local_score", "home_score", "local_points", "local_wins", "score_local", default=0) or 0))
            vis_score = int(float(pick(m, "visitor_score", "away_score", "visitor_points", "visitor_wins", "score_visitor", default=0) or 0))

            doubles_raw = pick(m, "doubles", "match_doubles", default=[])
            if not doubles_raw:
                doubles_raw = client.fetch_match_doubles(mid)

            doubles_norm = []
            for idx, dbl in enumerate(doubles_raw, start=1):
                if not isinstance(dbl, dict):
                    continue
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

            if loc_score == 0 and vis_score == 0 and doubles_norm:
                loc_score = sum(1 for d in doubles_norm if d["winner_side"] == "local")
                vis_score = sum(1 for d in doubles_norm if d["winner_side"] == "visitor")

            all_raw_matches.append({
                "id": mid,
                "season": s_label,
                "phase": phase_lbl,
                "round": bracket_stage if is_playoff and bracket_stage else round_lbl,
                "is_playoff": is_playoff,
                "bracket_stage": bracket_stage,
                "status": "completed",
                "venue_name": venue_name,
                "date": raw_date or "2026-01-01",
                "local_team": loc_name,
                "visitor_team": vis_name,
                "local_score": loc_score,
                "visitor_score": vis_score,
                "doubles": doubles_norm,
            })
            time.sleep(0.02)

        if is_cur:
            upcoming_raw: List[Tuple[Dict[str, Any], bool, str]] = []
            for st_key in ("in_progress", "scheduled", "pending", "upcoming"):
                for m in client.get_paginated("matches", {"status": st_key, "type": "league", "category_id": cid}, per_page=50, max_pages=3):
                    upcoming_raw.append((m, False, st_key))
                if elim_id:
                    for m in client.get_paginated("matches", {"status": st_key, "type": "tournament", "tournament_id": elim_id}, per_page=50, max_pages=3):
                        upcoming_raw.append((m, True, st_key))

            for raw_m, is_elim_src, st_key in upcoming_raw:
                mid, m = extract_match_id_and_body(raw_m)
                if not mid or mid in seen_match_ids or mid in seen_upcoming_ids:
                    continue
                seen_upcoming_ids.add(mid)

                loc_obj = pick(m, "local_team", "home_team", "team_local", "team1", default={})
                vis_obj = pick(m, "visitor_team", "away_team", "team_visitor", "team2", default={})
                loc_name = str(pick(loc_obj, "name", "team_name", default=pick(m, "local_team_name", default="Por definir"))).strip()
                vis_name = str(pick(vis_obj, "name", "team_name", default=pick(m, "visitor_team_name", default="Por definir"))).strip()
                raw_date = str(pick(m, "date", "scheduled_at", "match_date", "start_date", default="Por confirmar"))[:10]
                j_num = pick(m, "journey_number", "matchday", "round", "jornada")
                round_lbl = str(pick(m, "round_name", "phase_name", "stage", "journey_name", default=""))
                phase_lbl = str(pick(m, "phase", "stage_name", "match_type", default=""))
                is_po, b_stage = detect_playoff_stage(round_lbl, phase_lbl, j_num, is_elim_src)
                if not is_po:
                    continue

                venue_name = m["venue"].get("name", "") if isinstance(m.get("venue"), dict) else str(pick(m, "venue_name", default=""))
                upcoming_playoffs.append({
                    "id": mid,
                    "season": s_label,
                    "phase": f"Playoffs · {b_stage}",
                    "round": b_stage or round_lbl or "Playoffs",
                    "is_playoff": True,
                    "bracket_stage": b_stage or "Playoffs",
                    "status": st_key,
                    "venue_name": venue_name,
                    "date": raw_date,
                    "local_team": loc_name,
                    "visitor_team": vis_name,
                    "local_score": int(float(pick(m, "local_score", "home_score", default=0) or 0)),
                    "visitor_score": int(float(pick(m, "visitor_score", "away_score", default=0) or 0)),
                })

    # 4. ELOs
    cur_cid = current_season_obj["category_id"]
    cur_lid = current_season_obj["league_id"]
    elo_by_player_id = client.fetch_bulk_elo(cur_cid, cur_lid)
    if not elo_by_player_id and all_player_ids:
        print(f"[INFO] Consultando ELO individual ({len(all_player_ids)} jugadores)...")
        for pid in sorted(all_player_ids):
            prof = client.discover_and_fetch_player_profile(pid)
            if client.player_url_template == "__NONE__":
                break
            if not prof:
                continue
            rankings = prof.get("rankings") or []
            best_elo = 0.0
            for rk in rankings:
                if str(rk.get("category_id") or "") == cur_cid:
                    best_elo = float(rk.get("elo_points") or 0.0)
                    break
            if best_elo == 0.0 and rankings:
                for rk in rankings:
                    if str(rk.get("division") or "") == "6":
                        best_elo = float(rk.get("elo_points") or 0.0)
                        break
                if best_elo == 0.0:
                    best_elo = float(rankings[0].get("elo_points") or 0.0)
            if best_elo == 0.0 and prof.get("elo_points"):
                best_elo = float(prof.get("elo_points") or 0.0)
            elo_by_player_id[pid] = best_elo
            time.sleep(0.02)

    elo_hits = 0
    for rlist in rosters_by_team.values():
        for p in rlist:
            if p["id"] in elo_by_player_id and elo_by_player_id[p["id"]] > 0:
                p["elo"] = elo_by_player_id[p["id"]]
                elo_hits += 1

    print(f"[INFO] Jugadores con ELO cargado: {elo_hits}")

    snapshot = {
        "extracted_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "current_season": current_season_obj["season_label"],
        "seasons": seasons_order,
        "category_name": current_season_obj["category_name"],
        "teams_meta": teams_meta,
        "rosters_by_team": rosters_by_team,
        "standings_by_season": standings_by_season,
        "standings_by_group": standings_by_season.get(current_season_obj["season_label"], {}),
        "matches": all_raw_matches,
        "upcoming_playoffs": upcoming_playoffs,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Snapshot v4 guardado: {len(seasons_order)} temporadas, {len(teams_meta)} equipos, {len(all_raw_matches)} encuentros.")


if __name__ == "__main__":
    cfg_p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.json")
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/tol_snapshot.json")
    run_extraction(cfg_p, out_p)
