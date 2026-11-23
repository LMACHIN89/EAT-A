#!/usr/bin/env python3
"""
Compilador del Portal de Scouting v3 — Filtros Globales por Temporada
=====================================================================
Novedades v3:
  - Por defecto tanto el Portal Principal (docs/index.html) como CADA reporte de equipo
    muestran EXCLUSIVAMENTE la temporada actual (sin mezclar datos de temporadas pasadas).
  - Genera vistas completas e independientes para cada temporada disponible (ej. T2 2026, T1 2026)
    y una vista opcional "Histórico" para comparar química de parejas a largo plazo.
  - Mantiene el enlace de temporada activa (?season=...) al navegar entre portada y equipos.
  - Recalcula tablas de posiciones por temporada si la API devolvió 0, arma Brackets por temporada
    y calcula Posibles Parejas + Simulador D1–D5 con el rendimiento real de la temporada elegida.
"""

import argparse
import csv
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def normalize_cedula(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def normalize_name(name: str) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_str).strip().lower()


def slugify(text: str) -> str:
    s = normalize_name(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "equipo"


def load_cta_csv(csv_path: Path) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    by_ced: Dict[str, Dict] = {}
    by_name: Dict[str, Dict] = {}
    if not csv_path.exists():
        return by_ced, by_name
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rec = {
                "name": (r.get("name") or r.get("jugador") or "").strip(),
                "cedula": (r.get("cedula") or "").strip(),
                "club": (r.get("club") or "").strip(),
                "category": (r.get("category") or r.get("categoria") or "").strip(),
                "ranking": int(r.get("ranking") or 0),
                "points": float(r.get("points") or r.get("puntos") or 0),
            }
            ced = normalize_cedula(rec["cedula"])
            if ced:
                by_ced[ced] = rec
            nm = normalize_name(rec["name"])
            if nm:
                by_name[nm] = rec
    return by_ced, by_name


def match_player_cta(player: Dict[str, Any], cta_ced: Dict[str, Dict], cta_name: Dict[str, Dict]) -> Dict[str, Any]:
    ced = normalize_cedula(player.get("cedula"))
    if ced and ced in cta_ced:
        return {"verified": True, "match_method": "Cédula exacta", **cta_ced[ced]}
    nm = normalize_name(player.get("name", ""))
    if nm in cta_name:
        return {"verified": True, "match_method": "Nombre exacto", **cta_name[nm]}
    if nm and cta_name:
        best = difflib.get_close_matches(nm, cta_name.keys(), n=1, cutoff=0.88)
        if best:
            return {"verified": True, "match_method": "Nombre aproximado", **cta_name[best[0]]}
    return {"verified": False}


def analyze_double_sets(sets: List[List[int]], walkover: bool = False) -> Dict[str, Any]:
    if walkover or not sets:
        return {"sets_w": 0, "sets_l": 0, "games_w": 0, "games_l": 0, "played_stb": False, "won_stb": False, "score_str": "Walkover" if walkover else "—"}
    sets_w = sets_l = games_w = games_l = 0
    played_stb = won_stb = False
    parts = []
    for idx, st in enumerate(sets):
        a, b = int(st[0]), int(st[1])
        parts.append(f"{a}-{b}")
        is_stb = (idx == 2 and max(a, b) >= 10)
        if a > b:
            sets_w += 1
        elif b > a:
            sets_l += 1
        if is_stb:
            played_stb = True
            won_stb = a > b
            games_w += 1 if a > b else 0
            games_l += 1 if b > a else 0
        else:
            games_w += a
            games_l += b
    return {"sets_w": sets_w, "sets_l": sets_l, "games_w": games_w, "games_l": games_l, "played_stb": played_stb, "won_stb": won_stb, "score_str": ", ".join(parts)}


def canonical_pair(p1: str, p2: str) -> str:
    a, b = sorted([p1.strip(), p2.strip()], key=normalize_name)
    return f"{a} / {b}"


def is_match_playoff(m: Dict[str, Any]) -> bool:
    if m.get("is_playoff"):
        return True
    blob = f"{m.get('phase','')} {m.get('round','')} {m.get('bracket_stage','')}".lower()
    return any(k in blob for k in ("playoff", "play off", "octavo", "cuarto", "semi", "final", "tercer", "3er", "eliminat"))


def normalize_bracket_stage(m: Dict[str, Any]) -> str:
    blob = f"{m.get('bracket_stage','')} {m.get('round','')} {m.get('phase','')}".lower()
    if any(k in blob for k in ("tercer", "3er", "3ro", "third")):
        return "3er Lugar"
    if "semi" in blob:
        return "Semifinal"
    if "cuarto" in blob or "quarter" in blob:
        return "Cuartos"
    if "octavo" in blob or "round of 16" in blob:
        return "Octavos"
    if "final" in blob:
        return "Final"
    return m.get("round") or "Playoffs"


def build_team_view_of_matches(team_name: str, raw_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tnorm = normalize_name(team_name)
    out = []
    for m in raw_matches:
        is_local = normalize_name(m["local_team"]) == tnorm
        is_visitor = normalize_name(m["visitor_team"]) == tnorm
        if not (is_local or is_visitor):
            continue

        score_for = m["local_score"] if is_local else m["visitor_score"]
        score_against = m["visitor_score"] if is_local else m["local_score"]
        opponent = m["visitor_team"] if is_local else m["local_team"]
        venue = "Local" if is_local else "Visita"
        res = "V" if score_for >= score_against else "D"

        gf = ga = 0
        doubles_view = []
        for d in m.get("doubles", []):
            sets_local = d.get("sets_local", [])
            sets_team = sets_local if is_local else [[b, a] for a, b in sets_local]
            wo = bool(d.get("walkover", False))
            st = analyze_double_sets(sets_team, wo)
            gf += st["games_w"]
            ga += st["games_l"]

            ws = d.get("winner_side")
            if ws == "local":
                d_res = "V" if is_local else "D"
            elif ws == "visitor":
                d_res = "D" if is_local else "V"
            else:
                d_res = "V" if st["sets_w"] >= st["sets_l"] else "D"

            t_players = d["local_players"] if is_local else d["visitor_players"]
            o_players = d["visitor_players"] if is_local else d["local_players"]

            doubles_view.append({
                "pos": d["pos"],
                "team_players": t_players,
                "opp_players": o_players,
                "team_pair": f"{t_players[0]} / {t_players[1]}",
                "opp_pair": f"{o_players[0]} / {o_players[1]}",
                "sets": sets_team,
                "walkover": wo,
                "result": d_res,
                "score_str": st["score_str"],
            })

        out.append({
            "season": m["season"],
            "phase": m.get("phase", ""),
            "round": m.get("round", "Jornada"),
            "is_playoff": is_match_playoff(m),
            "date": m.get("date", ""),
            "venue": venue,
            "opponent": opponent,
            "score_for": score_for,
            "score_against": score_against,
            "result": res,
            "games_for": gf,
            "games_against": ga,
            "game_diff": gf - ga,
            "doubles": doubles_view,
        })

    out.sort(key=lambda x: x["date"])
    return out


def compute_standings_for_season(
    raw_groups: Dict[str, List[Dict[str, Any]]],
    raw_matches: List[Dict[str, Any]],
    season_key: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], bool]:
    """Calcula/rellena las tablas de posiciones para UNA temporada específica."""
    reg_stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"jw": 0, "jl": 0, "pts": 0, "sw": 0, "sl": 0, "gw": 0, "gl": 0})
    team_display: Dict[str, str] = {}
    season_reg_matches = [m for m in raw_matches if m["season"] == season_key and not is_match_playoff(m)]

    for m in season_reg_matches:
        ln, vn = normalize_name(m["local_team"]), normalize_name(m["visitor_team"])
        team_display[ln] = m["local_team"]
        team_display[vn] = m["visitor_team"]
        ls, vs = int(m.get("local_score", 0)), int(m.get("visitor_score", 0))
        if ls > vs:
            reg_stats[ln]["jw"] += 1; reg_stats[vn]["jl"] += 1
        elif vs > ls:
            reg_stats[vn]["jw"] += 1; reg_stats[ln]["jl"] += 1

        doubles = m.get("doubles", [])
        if doubles:
            for d in doubles:
                wo = bool(d.get("walkover", False))
                st_l = analyze_double_sets(d.get("sets_local", []), wo)
                ws = d.get("winner_side") or ("local" if st_l["sets_w"] >= st_l["sets_l"] else "visitor")
                if wo:
                    if ws == "local":
                        reg_stats[ln]["sw"] += 2; reg_stats[vn]["sl"] += 2; reg_stats[ln]["pts"] += 3
                    else:
                        reg_stats[vn]["sw"] += 2; reg_stats[ln]["sl"] += 2; reg_stats[vn]["pts"] += 3
                else:
                    reg_stats[ln]["sw"] += st_l["sets_w"]; reg_stats[ln]["sl"] += st_l["sets_l"]
                    reg_stats[vn]["sw"] += st_l["sets_l"]; reg_stats[vn]["sl"] += st_l["sets_w"]
                    reg_stats[ln]["gw"] += st_l["games_w"]; reg_stats[ln]["gl"] += st_l["games_l"]
                    reg_stats[vn]["gw"] += st_l["games_l"]; reg_stats[vn]["gl"] += st_l["games_w"]
                    if ws == "local":
                        reg_stats[ln]["pts"] += (2 if st_l["sets_l"] > 0 else 3)
                    else:
                        reg_stats[vn]["pts"] += (2 if st_l["sets_w"] > 0 else 3)
        else:
            reg_stats[ln]["pts"] += ls * 3
            reg_stats[vn]["pts"] += vs * 3

    # Si no hubo grupos desde API para esa temporada, armar tabla general con equipos que jugaron
    if not raw_groups and reg_stats:
        raw_groups = {
            "Tabla General": [
                {"pos": idx, "team": team_display[k], "pts": 0, "set_ave": 0.0, "game_ave": 0.0}
                for idx, k in enumerate(sorted(reg_stats.keys()), start=1)
            ]
        }

    recalculated_any = False
    fixed_groups: Dict[str, List[Dict[str, Any]]] = {}

    for gname, rows in raw_groups.items():
        all_zero = all((r.get("pts", 0) == 0 and r.get("set_ave", 0) == 0 and r.get("game_ave", 0) == 0) for r in rows) if rows else False
        new_rows = []
        for r in rows:
            rc = dict(r)
            st = reg_stats.get(normalize_name(r["team"]))
            if st:
                rc["jw"] = int(st["jw"])
                rc["jl"] = int(st["jl"])
                if all_zero or (rc.get("pts", 0) == 0 and st["pts"] > 0):
                    recalculated_any = True
                    rc["pts"] = int(st["pts"])
                if all_zero or (rc.get("set_ave", 0) == 0 and (st["sw"] + st["sl"]) > 0):
                    rc["set_ave"] = round(st["sw"] / (st["sw"] + st["sl"]) * 100.0, 1)
                if all_zero or (rc.get("game_ave", 0) == 0 and (st["gw"] + st["gl"]) > 0):
                    rc["game_ave"] = round(st["gw"] / (st["gw"] + st["gl"]) * 100.0, 1)
            else:
                rc.setdefault("jw", 0)
                rc.setdefault("jl", 0)
            new_rows.append(rc)

        new_rows.sort(key=lambda x: (-x["pts"], -x["set_ave"], -x["game_ave"], x["team"]))
        for i, row in enumerate(new_rows, start=1):
            row["pos"] = i
        fixed_groups[gname] = new_rows

    return fixed_groups, recalculated_any


def build_playoff_brackets(raw_matches: List[Dict[str, Any]], upcoming_playoffs: List[Dict[str, Any]], season_filter: Optional[str]) -> List[Dict[str, Any]]:
    stage_order = {"Octavos": 1, "Cuartos": 2, "Semifinal": 3, "Final": 4, "3er Lugar": 5}
    by_stage: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen = set()

    combined = [
        m for m in raw_matches
        if is_match_playoff(m) and (season_filter is None or m["season"] == season_filter)
    ] + [
        u for u in (upcoming_playoffs or [])
        if (season_filter is None or u.get("season") == season_filter)
    ]

    for m in combined:
        mid = m.get("id") or f"{m.get('date')}_{m.get('local_team')}_{m.get('visitor_team')}"
        if mid in seen:
            continue
        seen.add(mid)
        st_name = normalize_bracket_stage(m)
        by_stage[st_name].append({
            "id": mid,
            "season": m.get("season", ""),
            "round": st_name,
            "status": m.get("status", "completed"),
            "date": m.get("date", "Por confirmar"),
            "venue_name": m.get("venue_name", ""),
            "local_team": m.get("local_team", "Por definir"),
            "visitor_team": m.get("visitor_team", "Por definir"),
            "local_score": int(m.get("local_score", 0)),
            "visitor_score": int(m.get("visitor_score", 0)),
        })

    columns = []
    for st_name, mlist in sorted(by_stage.items(), key=lambda kv: (stage_order.get(kv[0], 99), kv[0])):
        mlist.sort(key=lambda x: (x["date"], x["local_team"]))
        columns.append({"stage": st_name, "matches": mlist})
    return columns


def compute_kpis(matches: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    jw = sum(1 for m in matches if m["result"] == "V")
    jl = sum(1 for m in matches if m["result"] == "D")
    cw = sum(1 for m in matches if m["score_for"] == 3 and m["score_against"] == 2)
    cl = sum(1 for m in matches if m["score_for"] == 2 and m["score_against"] == 3)
    mw = ml = sw = sl = gw = gl = stbw = stbl = 0
    for m in matches:
        for d in m.get("doubles", []):
            mw += 1 if d["result"] == "V" else 0
            ml += 1 if d["result"] != "V" else 0
            st = analyze_double_sets(d.get("sets", []), d.get("walkover", False))
            sw += st["sets_w"]; sl += st["sets_l"]
            gw += st["games_w"]; gl += st["games_l"]
            if st["played_stb"]:
                stbw += 1 if st["won_stb"] else 0
                stbl += 0 if st["won_stb"] else 1
    tm, ts, tg, tstb = mw + ml, sw + sl, gw + gl, stbw + stbl
    return {
        "season": label, "jornadas_w": jw, "jornadas_l": jl,
        "matches_w": mw, "matches_l": ml, "indiv_eff": (mw / tm * 100) if tm else 0.0,
        "sets_w": sw, "sets_l": sl, "set_ave": (sw / ts * 100) if ts else 0.0,
        "games_w": gw, "games_l": gl, "game_ave": (gw / tg * 100) if tg else 0.0,
        "stb_w": stbw, "stb_l": stbl, "stb_eff": (stbw / tstb * 100) if tstb else 0.0,
        "close_w": cw, "close_l": cl,
    }


def build_matchup_intel_for_slice(
    sliced_matches: List[Dict[str, Any]],
    players_slice: List[Dict[str, Any]],
) -> Dict[str, Any]:
    elo_map = {normalize_name(p["name"]): float(p.get("elo") or 0.0) for p in players_slice}
    recent_dates = {m["date"] for m in sliced_matches[-3:]}

    def pair_avg_elo(pair_str: str) -> float:
        parts = [p.strip() for p in pair_str.split("/")]
        elos = [elo_map.get(normalize_name(x), 0.0) for x in parts if elo_map.get(normalize_name(x), 0.0) > 0]
        return round(sum(elos) / len(elos), 1) if elos else 0.0

    pair_global: Dict[str, Dict[str, Any]] = {}
    pos_pair_weights: Dict[str, Dict[str, float]] = {p: defaultdict(float) for p in ("D1", "D2", "D3", "D4", "D5")}
    pos_pair_rec: Dict[str, Dict[str, List[int]]] = {p: defaultdict(lambda: [0, 0]) for p in ("D1", "D2", "D3", "D4", "D5")}

    for m in sliced_matches:
        w_mult = 2.0 if (m.get("is_playoff") or m["date"] in recent_dates) else 1.0
        for d in m.get("doubles", []):
            pos = d["pos"]
            if pos not in pos_pair_weights:
                continue
            pk = canonical_pair(d["team_players"][0], d["team_players"][1])
            won = d["result"] == "V"
            pos_pair_weights[pos][pk] += w_mult * (1.25 if won else 0.85)
            pos_pair_rec[pos][pk][0 if won else 1] += 1
            if pk not in pair_global:
                pair_global[pk] = {"pair_name": pk, "w": 0, "l": 0, "avg_elo": pair_avg_elo(pk)}
            pair_global[pk]["w" if won else "l"] += 1

    tactical_alert = ""
    if sliced_matches:
        last_m = sliced_matches[-1]
        d1_usual = max(pos_pair_weights["D1"].items(), key=lambda kv: kv[1])[0] if pos_pair_weights["D1"] else ""
        if d1_usual:
            for d in last_m.get("doubles", []):
                pk = canonical_pair(d["team_players"][0], d["team_players"][1])
                if pk == d1_usual and d["pos"] != "D1":
                    tactical_alert = f"En su último encuentro ({last_m['round']} vs {last_m['opponent']}), movieron su pareja habitual de D1 ({d1_usual}) hasta {d['pos']}."
                    break

    positions_out = []
    for pos in ("D1", "D2", "D3", "D4", "D5"):
        wmap = pos_pair_weights[pos]
        if wmap:
            total_w = sum(wmap.values())
            ranked = sorted(wmap.items(), key=lambda kv: (-kv[1], -pos_pair_rec[pos][kv[0]][0]))
            primary_p, primary_w = ranked[0]
            pw, pl = pos_pair_rec[pos][primary_p]
            p_eff = (pw / (pw + pl) * 100) if (pw + pl) else 50.0
            prob_pct = min(92, max(25, int(round(primary_w / total_w * 100))))
            threat = "Alta" if (p_eff >= 75 or (pw >= 3 and pl == 0)) else ("Media" if p_eff >= 45 else "Baja")
            alts = [{"pair_name": ap, "w": pos_pair_rec[pos][ap][0], "l": pos_pair_rec[pos][ap][1]} for ap, _ in ranked[1:3]]
            positions_out.append({
                "pos": pos, "primary_pair": primary_p, "prob_pct": prob_pct,
                "primary_record": f"{pw}–{pl}", "avg_elo": pair_avg_elo(primary_p),
                "threat": threat, "alternatives": alts,
            })
        else:
            top_r = sorted(players_slice, key=lambda x: -(x.get("elo") or 0))
            idx = int(pos[1]) - 1
            fb = f"{top_r[idx*2]['name']} / {top_r[idx*2+1]['name']}" if len(top_r) > idx * 2 + 1 else "Sin datos"
            positions_out.append({
                "pos": pos, "primary_pair": fb, "prob_pct": 30,
                "primary_record": "0–0", "avg_elo": pair_avg_elo(fb) if fb != "Sin datos" else 0.0,
                "threat": "Media", "alternatives": [],
            })

    all_rival_pairs = [
        {
            "pair_name": pg["pair_name"], "pj": pg["w"] + pg["l"], "w": pg["w"], "l": pg["l"],
            "eff": (pg["w"] / (pg["w"] + pg["l"]) * 100) if (pg["w"] + pg["l"]) else 50.0,
            "avg_elo": pg["avg_elo"],
        }
        for pg in pair_global.values()
    ]
    all_rival_pairs.sort(key=lambda x: (-x["pj"], -x["avg_elo"], -x["eff"]))
    return {"positions": positions_out, "all_rival_pairs": all_rival_pairs, "tactical_alert": tactical_alert}


def build_team_season_slice(
    team_name: str,
    season_key: str,
    season_label: str,
    is_current_season: bool,
    all_seasons_list: List[str],
    roster_all: List[Dict[str, Any]],
    all_team_matches: List[Dict[str, Any]],
    standings: List[Dict[str, Any]],
    group_name: str,
    standings_recalc: bool,
    team_playoffs: List[Dict[str, Any]],
    our_team_profile: Dict[str, Any],
    our_team_name: str,
    extracted_at: str,
    cta_ced: Dict[str, Dict],
    cta_name: Dict[str, Dict],
) -> Dict[str, Any]:
    """Construye la vista COMPLETA de un equipo estrictamente filtrada a `season_key` (o 'ALL')."""
    sliced_matches = all_team_matches if season_key == "ALL" else [m for m in all_team_matches if m["season"] == season_key]
    kpis = compute_kpis(sliced_matches, season_label)

    # Posiciones D1-D5
    pos_stats = []
    for pos in ("D1", "D2", "D3", "D4", "D5"):
        w = l = 0
        pair_rec: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for m in sliced_matches:
            for d in m["doubles"]:
                if d["pos"] == pos:
                    pk = canonical_pair(d["team_players"][0], d["team_players"][1])
                    if d["result"] == "V":
                        w += 1; pair_rec[pk][0] += 1
                    else:
                        l += 1; pair_rec[pk][1] += 1
        tot = w + l
        top_p, top_r = ("Sin datos", "0–0")
        if pair_rec:
            best = max(pair_rec.items(), key=lambda kv: (kv[1][0] + kv[1][1], kv[1][0]))
            top_p, top_r = best[0], f"{best[1][0]}–{best[1][1]}"
        pos_stats.append({"pos": pos, "w": w, "l": l, "eff": (w / tot * 100) if tot else 0.0, "unique_pairs": len(pair_rec), "top_pair": top_p, "top_pair_record": top_r})

    # Parejas
    agg_pairs: Dict[str, Dict[str, Any]] = {}
    for m in sliced_matches:
        for d in m["doubles"]:
            pk = canonical_pair(d["team_players"][0], d["team_players"][1])
            if pk not in agg_pairs:
                agg_pairs[pk] = {"pair_name": pk, "w": 0, "l": 0, "game_diff": 0, "positions": Counter()}
            st = analyze_double_sets(d.get("sets", []), d.get("walkover", False))
            agg_pairs[pk]["w" if d["result"] == "V" else "l"] += 1
            agg_pairs[pk]["game_diff"] += (st["games_w"] - st["games_l"])
            agg_pairs[pk]["positions"][d["pos"]] += 1
    pairs_out = []
    for it in agg_pairs.values():
        pj = it["w"] + it["l"]
        pairs_out.append({"pair_name": it["pair_name"], "pj": pj, "w": it["w"], "l": it["l"], "eff": (it["w"] / pj * 100) if pj else 0.0, "game_diff": it["game_diff"], "positions": sorted(it["positions"].keys())})
    pairs_out.sort(key=lambda x: (-x["pj"], -x["eff"], -x["game_diff"]))

    # Detectar temporadas en que jugó cada jugador (para marcar debutantes en la temporada actual)
    seasons_played_by_p: Dict[str, set] = defaultdict(set)
    for m in all_team_matches:
        for d in m["doubles"]:
            for pname in d["team_players"]:
                seasons_played_by_p[normalize_name(pname)].add(m["season"])

    # Historial por jugador STRICTLY dentro de sliced_matches
    roster_map = {normalize_name(p["name"]): p for p in roster_all}
    hist_by_p: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    pos_by_p: Dict[str, Counter] = defaultdict(Counter)

    for m in sliced_matches:
        for d in m["doubles"]:
            p1, p2 = d["team_players"][0], d["team_players"][1]
            for idx, pname in enumerate((p1, p2)):
                partner = p2 if idx == 0 else p1
                k = normalize_name(pname)
                pos_by_p[k][d["pos"]] += 1
                hist_by_p[k].append({
                    "season": m["season"], "round": m["round"], "date": m["date"],
                    "pos": d["pos"], "result": d["result"], "score_str": d["score_str"],
                    "partner": partner, "opp_pair": d["opp_pair"], "rival_team": m["opponent"],
                })

    # Qué jugadores incluir en esta vista:
    # - En temporada actual o ALL: todos los del roster de esa temporada + los que jugaron en sliced_matches
    # - En temporada pasada: los que pertenecen al roster de esa temporada o jugaron partidos en ella
    relevant_keys = set(hist_by_p.keys())
    for k, p in roster_map.items():
        p_seasons = p.get("seasons") or []
        if season_key == "ALL" or is_current_season or (season_key in p_seasons):
            relevant_keys.add(k)

    players_out = []
    heatmap_out = []
    for k in relevant_keys:
        base = roster_map.get(k, {"name": k.title(), "cedula": "—", "elo": 0.0, "tactical_note": ""})
        hist = sorted(hist_by_p.get(k, []), key=lambda x: x["date"], reverse=True)
        pj = len(hist)
        v = sum(1 for h in hist if h["result"] == "V")
        eff = (v / pj * 100) if pj else 0.0
        p_cnt = Counter(h["partner"] for h in hist)
        fixed_note = ""
        if p_cnt and pj >= 3:
            tp, cnt = p_cnt.most_common(1)[0]
            if cnt >= 3 or (cnt / pj) >= 0.5:
                pw = sum(1 for h in hist if h["partner"] == tp and h["result"] == "V")
                fixed_note = f"Compañero frecuente: {tp} en {cnt} de {pj} partidos ({pw}–{cnt - pw}, {int(round(pw / cnt * 100))}%)"
        is_deb = is_current_season and len(all_seasons_list) > 1 and (seasons_played_by_p.get(k, set()) == {season_key})
        players_out.append({
            "id": normalize_cedula(base.get("cedula")) or re.sub(r"\W+", "_", k),
            "name": base["name"], "cedula": base.get("cedula", "—"),
            "elo": float(base.get("elo") or 0.0), "pj": pj, "v": v, "eff": eff,
            "is_debutant": is_deb, "tactical_note": base.get("tactical_note", ""),
            "fixed_partner_note": fixed_note, "cta": match_player_cta(base, cta_ced, cta_name),
            "history": hist,
        })
        pc = pos_by_p.get(k, Counter())
        if sum(pc.values()) > 0:
            heatmap_out.append({"name": base["name"], "D1": pc["D1"], "D2": pc["D2"], "D3": pc["D3"], "D4": pc["D4"], "D5": pc["D5"], "total": sum(pc.values())})

    players_out.sort(key=lambda x: (-x["eff"], -x["pj"], -x["elo"]))
    heatmap_out.sort(key=lambda x: (-x["total"], -x["D1"], -x["D2"], x["name"]))

    matchup_intel = build_matchup_intel_for_slice(sliced_matches, players_out)
    h2h = sorted([m for m in sliced_matches if normalize_name(m["opponent"]) == normalize_name(our_team_name)], key=lambda x: x["date"], reverse=True)

    # Insights de 30 segundos strictly para esta temporada
    pos_grp = next((s["pos"] for s in standings if normalize_name(s["team"]) == normalize_name(team_name)), "—")
    pts_grp = next((s["pts"] for s in standings if normalize_name(s["team"]) == normalize_name(team_name)), "—")
    best_pos = max(pos_stats, key=lambda x: (x["eff"], x["w"]))
    weak_pos = min(pos_stats, key=lambda x: (x["eff"], -x["l"]))
    unbeaten = [p["name"] for p in players_out if p["pj"] >= 3 and p["v"] == p["pj"]]
    close_m = [m for m in sliced_matches if (m["score_for"], m["score_against"]) in {(3, 2), (2, 3)}]
    top4_names = {normalize_name(s["team"]) for s in standings[:4] if normalize_name(s["team"]) != normalize_name(team_name)}
    vs_top4 = [m for m in sliced_matches if normalize_name(m["opponent"]) in top4_names]
    t4_w = sum(1 for m in vs_top4 if m["result"] == "V")

    insights_30s = [
        {"kicker": f"Resumen ({season_label})", "headline": f"{pos_grp}° de {group_name} ({kpis['jornadas_w']}–{kpis['jornadas_l']} en jornadas)" if standings else f"Balance {kpis['jornadas_w']}–{kpis['jornadas_l']} en jornadas", "body": f"{f'Suma {pts_grp} pts con ' if standings else ''}{kpis['matches_w']}–{kpis['matches_l']} en dobles ({kpis['indiv_eff']:.1f}%) y {kpis['stb_w']}–{kpis['stb_l']} en súper tie-breaks.", "tone": "positive" if kpis["jornadas_w"] >= kpis["jornadas_l"] else "warn"},
        {"kicker": "Historial directo", "headline": f"{'Ganó' if h2h[0]['result']=='V' else 'Perdió'} {h2h[0]['score_for']}–{h2h[0]['score_against']} vs {our_team_name} ({h2h[0]['date']})" if h2h else f"Sin cruces vs {our_team_name}", "body": f"Diferencial en ese cruce: {h2h[0]['games_for']}–{h2h[0]['games_against']} ({h2h[0]['game_diff']:+d}). Cruces en {season_label}: {len(h2h)}." if h2h else f"Sin encuentros registrados contra {our_team_name} en {season_label}.", "tone": "info"},
        {"kicker": "Núcleo fuerte", "headline": f"{len(unbeaten)} jugador(es) invicto(s)" if unbeaten else "Rotación repartida en plantilla", "body": (f"Invictos (≥3 PJ en {season_label}): {', '.join(unbeaten[:4])}." if unbeaten else f"{len(players_out)} jugadores activos en {season_label}."), "tone": "positive" if unbeaten else "default"},
        {"kicker": "Rendimiento por posición", "headline": f"{best_pos['pos']} es su punto más fuerte ({best_pos['w']}–{best_pos['l']})", "body": f"Promedia {best_pos['eff']:.0f}% en {best_pos['pos']} (base: {best_pos['top_pair']}). Flanco más vulnerable: {weak_pos['pos']} ({weak_pos['w']}–{weak_pos['l']}).", "tone": "default"},
        {"kicker": "Fuerza ante el Top 4", "headline": f"{t4_w}–{len(vs_top4) - t4_w} contra equipos de arriba" if vs_top4 else "Sin cruces ante Top 4", "body": "Cruces: " + (", ".join(f"{m['opponent']} ({m['score_for']}–{m['score_against']})" for m in vs_top4[:3]) or f"Sin partidos ante Top 4 en {season_label}."), "tone": "info"},
        {"kicker": "Margen real vs marcador", "headline": f"{len(close_m)} jornada(s) cerrada(s) 3–2" if close_m else f"Diferencial total: {kpis['games_w'] - kpis['games_l']:+d} games", "body": ("Games en 3–2: " + ", ".join(f"{m['opponent']} ({m['game_diff']:+d})" for m in close_m[:4]) + ".") if close_m else f"Efectividad real en games del {kpis['game_ave']:.1f}%.", "tone": "warn" if close_m else "default"},
    ]

    fixed_pairs = [p for p in pairs_out if p["pj"] >= 3]
    tactical_notes = [
        f"<strong>Parejas consolidadas en {season_label}:</strong> " + (", ".join(f"<strong>{p['pair_name']}</strong> ({p['w']}–{p['l']} en {'/'.join(p['positions'])})" for p in fixed_pairs[:3]) if fixed_pairs else "Alta rotación de parejas; sin duplas repetidas en 3 o más jornadas."),
        f"<strong>Lectura por posición:</strong> su mejor rendimiento está en <strong>{best_pos['pos']}</strong> ({best_pos['w']}–{best_pos['l']}) y su punto más atacable en <strong>{weak_pos['pos']}</strong> ({weak_pos['w']}–{weak_pos['l']}, {weak_pos['unique_pairs']} parejas distintas probadas).",
    ]

    st_note = f"Datos de {season_label} actualizados el {extracted_at}."
    if standings_recalc and standings:
        st_note += " Puntos y promedios calculados automáticamente desde las actas de fase regular (3 pts por 2–0/W.O., 2 pts por 2–1)."

    return {
        "label": season_label,
        "group_name": group_name,
        "standings_note": st_note,
        "roster_count": len(players_out),
        "kpis": kpis,
        "hero_summary": f"Balance en {season_label}: {kpis['jornadas_w']}–{kpis['jornadas_l']} en jornadas y {kpis['matches_w']}–{kpis['matches_l']} en dobles ({kpis['indiv_eff']:.1f}%).",
        "insights_30s": insights_30s,
        "tactical_notes": tactical_notes,
        "matchup_intel": matchup_intel,
        "our_team_profile": our_team_profile,
        "team_playoffs": team_playoffs,
        "h2h_matches": h2h,
        "standings": standings,
        "jornadas": sliced_matches,
        "players": players_out,
        "position_stats": pos_stats,
        "pairs": pairs_out,
        "position_heatmap": heatmap_out,
    }


def render_portal_index(portal_payload: Dict[str, Any]) -> str:
    payload_json = json.dumps(portal_payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Scouting {portal_payload['category']} — {portal_payload['our_team']}</title>
  <style>
    :root {{ --bg:#f4f6f9; --surface:#fff; --border:#e2e8f0; --border-strong:#cbd5e1; --primary:#0f2c59; --soft:#e8eef8; --text:#0f172a; --muted:#64748b; --green:#166534; --gsoft:#dcfce7; --warn:#92400e; --wsoft:#fef3c7; }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }}
    .wrap {{ max-width:1160px; margin:0 auto; padding:20px 16px 60px; }}
    .hero {{ background:linear-gradient(135deg,#0b1f3f,#153a6e); color:#fff; border-radius:16px; padding:26px; margin-bottom:16px; }}
    .hero h1 {{ font-size:clamp(22px,3vw,30px); margin-bottom:6px; }}
    .hero p {{ color:#cbd5e1; font-size:13.5px; }}
    .nav-pills {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }}
    .nav-pills a {{ text-decoration:none; background:rgba(255,255,255,0.14); color:#fff; padding:6px 13px; border-radius:999px; font-size:12.5px; font-weight:700; }}
    .nav-pills a:hover {{ background:rgba(255,255,255,0.25); }}

    .season-bar {{ position:sticky; top:8px; z-index:50; background:rgba(255,255,255,0.96); backdrop-filter:blur(10px); border:1px solid var(--border-strong); border-radius:14px; padding:10px 14px; margin-bottom:22px; box-shadow:0 6px 20px rgba(15,44,89,0.09); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; }}
    .season-bar-left {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
    .season-label {{ font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:0.6px; color:var(--primary); }}
    .season-pills {{ display:inline-flex; flex-wrap:wrap; gap:6px; }}
    .season-btn {{ border:1px solid var(--border-strong); background:#f8fafc; color:#475569; padding:6px 13px; border-radius:999px; font-size:12.5px; font-weight:700; cursor:pointer; transition:all .15s; }}
    .season-btn:hover {{ border-color:var(--primary); color:var(--primary); background:var(--soft); }}
    .season-btn.active {{ background:var(--primary); border-color:var(--primary); color:#fff; box-shadow:0 2px 8px rgba(15,44,89,0.25); }}
    .season-btn.all-btn.active {{ background:#334155; border-color:#334155; }}
    .season-ind {{ font-size:12px; color:#475569; font-weight:600; }}
    .season-ind strong {{ color:var(--primary); }}
    .warn-pill {{ background:var(--wsoft); color:var(--warn); padding:3px 9px; border-radius:999px; font-size:11.5px; font-weight:700; }}

    h2 {{ font-size:19px; color:var(--primary); margin-bottom:6px; }}
    .sub {{ font-size:13px; color:var(--muted); margin-bottom:14px; }}
    .search {{ width:100%; padding:12px 16px; border:1px solid #cbd5e1; border-radius:10px; font-size:15px; margin-bottom:16px; outline:none; }}
    .search:focus {{ border-color:var(--primary); box-shadow:0 0 0 3px rgba(15,44,89,0.12); }}

    .bracket-board {{ display:flex; gap:16px; overflow-x:auto; padding-bottom:10px; align-items:flex-start; }}
    .bk-col {{ min-width:255px; flex:1; background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px; }}
    .bk-col-title {{ font-size:12.5px; font-weight:800; text-transform:uppercase; letter-spacing:0.6px; color:var(--primary); padding-bottom:8px; margin-bottom:10px; border-bottom:2px solid var(--soft); }}
    .bk-match {{ background:#f8fafc; border:1px solid var(--border); border-radius:10px; padding:10px 12px; margin-bottom:10px; }}
    .bk-match:last-child {{ margin-bottom:0; }}
    .bk-match.bk-own {{ border:2px solid #16a34a; background:#f0fdf4; }}
    .bk-meta {{ display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--muted); margin-bottom:6px; }}
    .st-pill {{ font-size:10.5px; font-weight:800; padding:1px 7px; border-radius:999px; }}
    .st-done {{ background:var(--soft); color:var(--primary); }}
    .st-live {{ background:var(--wsoft); color:var(--warn); }}
    .st-next {{ background:var(--gsoft); color:var(--green); }}
    .bk-row {{ display:flex; justify-content:space-between; align-items:center; padding:3px 0; font-size:13.5px; }}
    .bk-row a {{ text-decoration:none; color:var(--text); font-weight:600; }}
    .bk-row a:hover {{ color:var(--primary); text-decoration:underline; }}
    .bk-row.bk-win a, .bk-row.bk-win .bk-sc {{ color:var(--green); font-weight:800; }}
    .bk-sc {{ font-weight:800; font-variant-numeric:tabular-nums; margin-left:8px; }}
    .bk-venue {{ font-size:11px; color:var(--muted); margin-top:4px; }}

    .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(265px,1fr)); gap:14px; margin-bottom:28px; }}
    .team-card {{ display:block; text-decoration:none; color:inherit; background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; transition:transform .15s,border-color .15s,box-shadow .15s; }}
    .team-card:hover {{ transform:translateY(-2px); border-color:var(--primary); box-shadow:0 6px 18px rgba(15,44,89,0.1); }}
    .team-card.own {{ border:2px solid #16a34a; background:#f0fdf4; }}
    .tc-top {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:6px; }}
    .tc-name {{ font-size:16px; font-weight:800; color:var(--primary); }}
    .badge {{ font-size:11.5px; font-weight:800; padding:3px 8px; border-radius:999px; background:var(--soft); color:var(--primary); }}
    .badge.ok {{ background:var(--gsoft); color:var(--green); }}
    .tc-meta {{ font-size:12.5px; color:var(--muted); display:flex; justify-content:space-between; margin-top:6px; }}

    .standings-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; }}
    .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
    .card h3 {{ font-size:15px; color:var(--primary); margin-bottom:10px; }}
    .tw {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:8px 8px; border-bottom:1px solid var(--border); text-align:left; }}
    th {{ font-size:11px; text-transform:uppercase; color:var(--muted); }}
    td a {{ color:var(--primary); font-weight:700; text-decoration:none; }}
    td a:hover {{ text-decoration:underline; }}
    tr.own td {{ background:#f0fdf4; }}
    .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .center {{ text-align:center; font-variant-numeric:tabular-nums; }}
    footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:32px; }}
  </style>
</head>
<body>
  <div class="wrap" id="portal-app"></div>
  <script>
    const PORTAL_DATA = {payload_json};
    (function initPortal(data) {{
      const root = document.getElementById("portal-app");
      const esc = (s) => String(s ?? "").replace(/[&<>"']/g, m => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[m]));
      const seasons = data.available_seasons || [data.current_season];
      const urlS = new URLSearchParams(window.location.search).get("season");
      let activeSeason = (urlS && data.by_season[urlS]) ? urlS : data.current_season;
      if (!data.by_season[activeSeason]) activeSeason = Object.keys(data.by_season)[0];
      let searchTerm = "";

      function render() {{
        const view = data.by_season[activeSeason];
        const sLbl = view.label;
        const isHist = activeSeason === "ALL";
        const qSuffix = `?season=${{encodeURIComponent(activeSeason)}}`;
        const ourNorm = data.our_team.toLowerCase();

        const pillsHtml = [
          ...seasons.map(s => `<button class="season-btn ${{s === activeSeason ? "active" : ""}}" data-s="${{esc(s)}}">${{esc(s)}}${{s === data.current_season ? " (Actual)" : ""}}</button>`),
          ...(seasons.length > 1 ? [`<button class="season-btn all-btn ${{activeSeason === "ALL" ? "active" : ""}}" data-s="ALL">Histórico (${{seasons.length}} temp.)</button>`] : [])
        ].join("");

        // Brackets
        const cols = view.bracket_columns || [];
        const bracketsHtml = cols.length ? `
          <div class="bracket-board">
            ${{cols.map(col => `
              <div class="bk-col">
                <div class="bk-col-title">${{esc(col.stage)}} (${{col.matches.length}})</div>
                ${{col.matches.map(bm => {{
                  const invOwn = bm.local_team.toLowerCase() === ourNorm || bm.visitor_team.toLowerCase() === ourNorm;
                  const done = bm.status === "completed", live = bm.status === "in_progress";
                  const stCls = done ? "st-done" : (live ? "st-live" : "st-next");
                  const stLbl = done ? "Finalizado" : (live ? "En curso" : "Por jugar");
                  const lWin = done && bm.local_score > bm.visitor_score;
                  const vWin = done && bm.visitor_score > bm.local_score;
                  const lLink = bm.local_team !== "Por definir" ? `<a href="equipos/${{bm.local_slug}}.html${{qSuffix}}">${{esc(bm.local_team)}}</a>` : "Por definir";
                  const vLink = bm.visitor_team !== "Por definir" ? `<a href="equipos/${{bm.visitor_slug}}.html${{qSuffix}}">${{esc(bm.visitor_team)}}</a>` : "Por definir";
                  return `
                    <div class="bk-match ${{invOwn ? "bk-own" : ""}}">
                      <div class="bk-meta"><span>${{esc(bm.date)}}${{isHist && bm.season ? " · " + esc(bm.season) : ""}}</span><span class="st-pill ${{stCls}}">${{stLbl}}</span></div>
                      <div class="bk-row ${{lWin ? "bk-win" : ""}}"><span>${{lLink}}</span><span class="bk-sc">${{done || live ? bm.local_score : "—"}}</span></div>
                      <div class="bk-row ${{vWin ? "bk-win" : ""}}"><span>${{vLink}}</span><span class="bk-sc">${{done || live ? bm.visitor_score : "—"}}</span></div>
                      ${{bm.venue_name ? `<div class="bk-venue">📍 ${{esc(bm.venue_name)}}</div>` : ""}}
                    </div>
                  `;
                }}).join("")}}
              </div>
            `).join("")}}
          </div>
        ` : `<div class="card"><p style="color:var(--muted);font-size:13.5px">No hay cruces de playoffs registrados para ${{esc(sLbl)}}.</p></div>`;

        // Standings
        const grpEntries = Object.entries(view.standings_by_group || {{}}).filter(([_, rows]) => rows && rows.length);
        const standingsHtml = grpEntries.length ? `
          <div class="standings-grid">
            ${{grpEntries.map(([gname, rows]) => `
              <div class="card">
                <h3>${{esc(gname)}}</h3>
                <div class="tw">
                  <table>
                    <thead><tr><th>Pos</th><th>Equipo</th><th class="center">G–P</th><th class="num">Pts</th><th class="num">Set%</th><th class="num">Game%</th></tr></thead>
                    <tbody>
                      ${{rows.map(r => `
                        <tr class="${{r.team.toLowerCase() === ourNorm ? "own" : ""}}">
                          <td>${{r.pos}}</td>
                          <td><a href="equipos/${{r.slug}}.html${{qSuffix}}">${{esc(r.team)}}</a></td>
                          <td class="center">${{r.jw ?? 0}}–${{r.jl ?? 0}}</td>
                          <td class="num"><strong>${{r.pts}}</strong></td>
                          <td class="num">${{r.set_ave.toFixed(1)}}%</td>
                          <td class="num">${{r.game_ave.toFixed(1)}}%</td>
                        </tr>
                      `).join("")}}
                    </tbody>
                  </table>
                </div>
              </div>
            `).join("")}}
          </div>
          ${{view.standings_recalc ? `<p class="sub" style="margin-top:8px">* Puntos y promedios calculados automáticamente desde las actas de fase regular de ${{esc(sLbl)}} (3 pts por 2–0/W.O., 2 pts por 2–1).</p>` : ""}}
        ` : `<div class="card"><p style="color:var(--muted);font-size:13.5px">Selecciona una temporada específica arriba para consultar las tablas de posiciones por grupo.</p></div>`;

        root.innerHTML = `
          <header class="hero">
            <h1>🎾 Portal de Scouting — ${{esc(data.category)}}</h1>
            <p>Equipo base: <strong>${{esc(data.our_team)}}</strong> · Actualizado: ${{esc(data.updated_at)}}</p>
            <div class="nav-pills">
              <a href="#sec-brackets">🏆 Brackets Playoffs</a>
              <a href="#sec-tablas">📊 Tablas de Posiciones</a>
              <a href="#sec-equipos">🎯 Reportes &amp; Simulador de Parejas</a>
            </div>
          </header>

          <div class="season-bar">
            <div class="season-bar-left">
              <span class="season-label">📅 Temporada:</span>
              <div class="season-pills" id="portal-season-pills">${{pillsHtml}}</div>
            </div>
            <div class="season-ind">
              ${{isHist ? `<span class="warn-pill">Vista histórica acumulada</span>` : `Mostrando solo <strong>${{esc(sLbl)}}</strong>`}}
            </div>
          </div>

          <section id="sec-brackets" style="margin-bottom:28px">
            <h2>🏆 Brackets de Enfrentamiento — ${{esc(sLbl)}}</h2>
            <p class="sub">Haz clic en cualquier equipo para abrir su scouting filtrado en ${{esc(sLbl)}} y simular nuestras parejas D1–D5.</p>
            ${{bracketsHtml}}
          </section>

          <section id="sec-tablas" style="margin-bottom:28px">
            <h2>📊 Tablas de Posiciones — ${{esc(sLbl)}}</h2>
            ${{standingsHtml}}
          </section>

          <section id="sec-equipos">
            <h2>🎯 Reportes por Equipo &amp; Simulador de Parejas (${{esc(sLbl)}})</h2>
            <p class="sub">Métricas y récords correspondientes únicamente a ${{esc(sLbl)}}.</p>
            <input class="search" id="q" type="text" value="${{esc(searchTerm)}}" placeholder="Buscar equipo rival o grupo..." />
            <div class="grid" id="team-grid"></div>
          </section>

          <footer>Actualización automática TOL + CTA · GitHub Pages</footer>
        `;

        document.querySelectorAll("#portal-season-pills .season-btn").forEach(btn => btn.addEventListener("click", () => {{
          activeSeason = btn.dataset.s;
          const u = new URL(window.location.href);
          u.searchParams.set("season", activeSeason);
          window.history.replaceState({{}}, "", u.toString());
          render();
        }}));

        const qInput = document.getElementById("q");
        const grid = document.getElementById("team-grid");
        function drawCards() {{
          const term = searchTerm.trim().toLowerCase();
          const list = (view.cards || []).filter(t => t.name.toLowerCase().includes(term) || t.group.toLowerCase().includes(term));
          grid.innerHTML = list.map(t => `
            <a class="team-card ${{t.is_own ? "own" : ""}}" href="equipos/${{t.slug}}.html${{qSuffix}}">
              <div class="tc-top">
                <div class="tc-name">${{esc(t.name)}}</div>
                <span class="badge ${{t.eff >= 60 ? "ok" : ""}}">${{t.jornadas_w}}–${{t.jornadas_l}}</span>
              </div>
              <div style="font-size:12.5px;color:#475569">${{esc(t.group)}} · ${{t.roster}} jugadores</div>
              <div class="tc-meta"><span>Efectividad: <strong>${{t.eff.toFixed(0)}}%</strong></span><span>Games: ${{t.game_ave.toFixed(1)}}%</span></div>
            </a>
          `).join("") || `<p style="color:var(--muted)">Sin equipos para mostrar en ${{esc(sLbl)}}.</p>`;
        }}
        qInput.addEventListener("input", (e) => {{ searchTerm = e.target.value; drawCards(); }});
        drawCards();
      }}
      render();
    }})(PORTAL_DATA);
  </script>
</body>
</html>"""


def build_demo_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot de prueba con DOS temporadas (T2 2026 y T1 2026) para verificar el filtro global."""
    return {
        "extracted_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "current_season": "T2 2026",
        "seasons": ["T2 2026", "T1 2026"],
        "category_name": cfg.get("category_name", "6ta Masculino"),
        "teams_meta": {
            "Pan de Azucar C": {"id": "d1", "name": "Pan de Azucar C", "club": "Club Campestre Pan de Azucar", "seasons": ["T2 2026", "T1 2026"]},
            "Andrew Tennis A": {"id": "d2", "name": "Andrew Tennis A", "club": "El Laguito", "seasons": ["T2 2026", "T1 2026"]},
            "Monteclaro": {"id": "d3", "name": "Monteclaro", "club": "Monteclaro", "seasons": ["T2 2026"]},
            "Coronado A": {"id": "d4", "name": "Coronado A", "club": "Coronado", "seasons": ["T2 2026"]},
        },
        "rosters_by_team": {
            "Pan de Azucar C": [
                {"id": "p1", "name": "Jose Goncalvez", "cedula": "17845120", "elo": 274.4, "seasons": ["T2 2026", "T1 2026"]},
                {"id": "p2", "name": "Guillermo Haddad", "cedula": "18234901", "elo": 266.1, "seasons": ["T2 2026", "T1 2026"]},
                {"id": "p3", "name": "Amaru Araujo", "cedula": "15901234", "elo": 252.3, "seasons": ["T2 2026", "T1 2026"]},
                {"id": "p4", "name": "Michael Sosa", "cedula": "16096639", "elo": 260.65, "seasons": ["T2 2026", "T1 2026"]},
                {"id": "p5", "name": "Miguel La Rosa", "cedula": "20112984", "elo": 231.5, "seasons": ["T2 2026"]},
                {"id": "p6", "name": "Carlos Azocar", "cedula": "13128330", "elo": 235.76, "seasons": ["T2 2026", "T1 2026"]},
            ],
            "Andrew Tennis A": [
                {"id": "a1", "name": "Oscar Suinaga", "cedula": "6847614", "elo": 270.0, "seasons": ["T2 2026", "T1 2026"]},
                {"id": "a2", "name": "Ruben Gonzalez", "cedula": "15000001", "elo": 264.0, "seasons": ["T2 2026"]},
                {"id": "a3", "name": "Juan Diego Garcia", "cedula": "24760405", "elo": 258.5, "seasons": ["T2 2026"]},
                {"id": "a4", "name": "Jesus Melchor", "cedula": "25268133", "elo": 241.0, "seasons": ["T2 2026", "T1 2026"]},
            ],
            "Monteclaro": [],
            "Coronado A": [],
        },
        "standings_by_season": {
            "T2 2026": {
                "Grupo B": [
                    {"pos": 1, "team": "Andrew Tennis A", "pts": 0, "set_ave": 0.0, "game_ave": 0.0},
                    {"pos": 2, "team": "Pan de Azucar C", "pts": 0, "set_ave": 0.0, "game_ave": 0.0},
                    {"pos": 3, "team": "Monteclaro", "pts": 0, "set_ave": 0.0, "game_ave": 0.0},
                ]
            },
            "T1 2026": {
                "Grupo C": [
                    {"pos": 1, "team": "Andrew Tennis A", "pts": 0, "set_ave": 0.0, "game_ave": 0.0},
                    {"pos": 2, "team": "Pan de Azucar C", "pts": 0, "set_ave": 0.0, "game_ave": 0.0},
                ]
            },
        },
        "matches": [
            {
                "id": "m_t1", "season": "T1 2026", "phase": "Playoffs · 3er Lugar", "round": "3er Lugar", "is_playoff": True, "bracket_stage": "3er Lugar",
                "status": "completed", "date": "2026-08-01", "venue_name": "El Laguito",
                "local_team": "Andrew Tennis A", "visitor_team": "Pan de Azucar C", "local_score": 3, "visitor_score": 1,
                "doubles": [
                    {"pos": "D4", "local_players": ["Jesus Melchor", "Oscar Suinaga"], "visitor_players": ["Jose Goncalvez", "Guillermo Haddad"], "sets_local": [[6, 2], [4, 6], [10, 8]], "walkover": False, "winner_side": "local"},
                ],
            },
            {
                "id": "m_t2", "season": "T2 2026", "phase": "Fase Regular", "round": "Jornada 5", "is_playoff": False,
                "status": "completed", "date": "2026-10-18", "venue_name": "Pan de Azucar",
                "local_team": "Pan de Azucar C", "visitor_team": "Andrew Tennis A", "local_score": 3, "visitor_score": 2,
                "doubles": [
                    {"pos": "D1", "local_players": ["Jose Goncalvez", "Guillermo Haddad"], "visitor_players": ["Ruben Gonzalez", "Juan Diego Garcia"], "sets_local": [[6, 3], [4, 6], [10, 8]], "walkover": False, "winner_side": "local"},
                    {"pos": "D4", "local_players": ["Amaru Araujo", "Michael Sosa"], "visitor_players": ["Jesus Melchor", "Oscar Suinaga"], "sets_local": [[6, 4], [6, 0]], "walkover": False, "winner_side": "local"},
                    {"pos": "D5", "local_players": ["Carlos Azocar", "Miguel La Rosa"], "visitor_players": ["Ruben Gonzalez", "Oscar Suinaga"], "sets_local": [[7, 5], [2, 6], [11, 9]], "walkover": False, "winner_side": "local"},
                ],
            },
        ],
        "upcoming_playoffs": [
            {
                "id": "po_t2", "season": "T2 2026", "phase": "Playoffs · Semifinal", "round": "Semifinal", "is_playoff": True, "bracket_stage": "Semifinal",
                "status": "scheduled", "date": "2026-11-29", "venue_name": "El Laguito",
                "local_team": "Andrew Tennis A", "visitor_team": "Pan de Azucar C", "local_score": 0, "visitor_score": 0,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera portal y reportes con filtro global por temporada.")
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--snapshot", default="data/tol_snapshot.json", type=Path)
    parser.add_argument("--template", default="plantilla_reporte.html", type=Path)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    snapshot = build_demo_snapshot(cfg) if args.demo else json.loads(args.snapshot.read_text(encoding="utf-8"))

    cta_ced, cta_name = load_cta_csv(Path(cfg.get("cta_csv_path", "data/cta_ranking.csv")))
    template_html = args.template.read_text(encoding="utf-8")

    out_dir = Path(cfg.get("output_dir", "docs"))
    teams_dir = out_dir / "equipos"
    teams_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    current_season = snapshot["current_season"]
    category_name = snapshot["category_name"]
    extracted_at = snapshot["extracted_at"]
    raw_matches = snapshot.get("matches", [])
    upcoming_playoffs = snapshot.get("upcoming_playoffs", [])

    # Descubrir todas las temporadas ordenadas (actual primero)
    seasons_order = list(snapshot.get("seasons") or [])
    for m in raw_matches:
        if m["season"] not in seasons_order:
            seasons_order.append(m["season"])
    if current_season in seasons_order:
        seasons_order.remove(current_season)
    seasons_order.insert(0, current_season)

    # Standings por temporada (compatibilidad hacia atrás con standings_by_group)
    raw_standings_by_season = snapshot.get("standings_by_season") or {current_season: snapshot.get("standings_by_group", {})}
    standings_by_season: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    recalc_by_season: Dict[str, bool] = {}
    team_to_group_by_season: Dict[str, Dict[str, Tuple[str, List[Dict[str, Any]]]]] = {}

    for s in seasons_order:
        st_groups, was_recalc = compute_standings_for_season(raw_standings_by_season.get(s, {}), raw_matches, s)
        # Inyectar slug a cada fila para el portal
        for gname, rows in st_groups.items():
            for r in rows:
                r["slug"] = slugify(r["team"])
        standings_by_season[s] = st_groups
        recalc_by_season[s] = was_recalc
        tmap: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
        for gname, rows in st_groups.items():
            for r in rows:
                tmap[normalize_name(r["team"])] = (gname, rows)
        team_to_group_by_season[s] = tmap

    # Brackets por temporada y acumulado
    season_keys_all = list(seasons_order) + (["ALL"] if len(seasons_order) > 1 else [])
    brackets_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for sk in season_keys_all:
        cols = build_playoff_brackets(raw_matches, upcoming_playoffs, None if sk == "ALL" else sk)
        for col in cols:
            for bm in col["matches"]:
                bm["local_slug"] = slugify(bm["local_team"])
                bm["visitor_slug"] = slugify(bm["visitor_team"])
        brackets_by_key[sk] = cols

    # Perfil de nuestro equipo (Andrew Tennis A) POR TEMPORADA para el simulador D1-D5
    our_team_name = cfg.get("our_team", "Andrew Tennis A")
    our_norm = normalize_name(our_team_name)
    our_key_real = next((t for t in snapshot.get("teams_meta", {}) if normalize_name(t) == our_norm), our_team_name)
    our_roster_raw = snapshot.get("rosters_by_team", {}).get(our_key_real, [])
    our_matches_all = build_team_view_of_matches(our_key_real, raw_matches)

    our_profile_by_key: Dict[str, Dict[str, Any]] = {}
    for sk in season_keys_all:
        om_slice = our_matches_all if sk == "ALL" else [m for m in our_matches_all if m["season"] == sk]
        pstats: Dict[str, Dict[str, Any]] = {}
        pwl: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for om in om_slice:
            for d in om.get("doubles", []):
                won = d["result"] == "V"
                pk = canonical_pair(d["team_players"][0], d["team_players"][1])
                if pk not in pstats:
                    pstats[pk] = {"pj": 0, "w": 0, "eff": 50.0}
                pstats[pk]["pj"] += 1
                if won:
                    pstats[pk]["w"] += 1
                pstats[pk]["eff"] = pstats[pk]["w"] / pstats[pk]["pj"] * 100
                for pn in d["team_players"]:
                    pwl[normalize_name(pn)][0 if won else 1] += 1
        r_compact = []
        for p in our_roster_raw:
            wl = pwl.get(normalize_name(p["name"]), [0, 0])
            tot = wl[0] + wl[1]
            r_compact.append({"name": p["name"], "elo": float(p.get("elo") or 0.0), "eff": (wl[0] / tot * 100) if tot else 50.0})
        our_profile_by_key[sk] = {"name": our_key_real, "roster": r_compact, "pair_stats": pstats}

    # Compilar cada equipo con TODAS sus vistas por temporada
    all_teams = sorted(snapshot.get("teams_meta", {}).keys())
    portal_cards_by_key: Dict[str, List[Dict[str, Any]]] = {sk: [] for sk in season_keys_all}

    for tname in all_teams:
        tmeta = snapshot["teams_meta"][tname]
        roster_all = snapshot.get("rosters_by_team", {}).get(tname, [])
        t_matches_all = build_team_view_of_matches(tname, raw_matches)
        season_comparison = [compute_kpis([m for m in t_matches_all if m["season"] == s], s) for s in seasons_order]

        by_season_payload: Dict[str, Dict[str, Any]] = {}
        for sk in season_keys_all:
            is_cur = (sk == current_season)
            s_lbl = "Histórico (Todas)" if sk == "ALL" else sk
            if sk == "ALL":
                gname, g_standings, was_recalc = ("Histórico", [], False)
            else:
                gname, g_standings = team_to_group_by_season[sk].get(normalize_name(tname), ("General", []))
                was_recalc = recalc_by_season[sk]

            all_bk_m = [bm for col in brackets_by_key[sk] for bm in col["matches"]]
            t_playoffs = [
                bm for bm in all_bk_m
                if normalize_name(bm["local_team"]) == normalize_name(tname) or normalize_name(bm["visitor_team"]) == normalize_name(tname)
            ]

            slice_data = build_team_season_slice(
                tname, sk, s_lbl, is_cur, seasons_order, roster_all, t_matches_all,
                g_standings, gname, was_recalc, t_playoffs, our_profile_by_key[sk],
                our_team_name, extracted_at, cta_ced, cta_name
            )
            by_season_payload[sk] = slice_data

            # Añadir tarjeta del equipo al portal de esta temporada si participó en ella
            t_seasons = tmeta.get("seasons") or seasons_order
            if sk == "ALL" or is_cur or (sk in t_seasons) or len(slice_data["jornadas"]) > 0:
                kc = slice_data["kpis"]
                portal_cards_by_key[sk].append({
                    "name": tname, "slug": slugify(tname), "group": gname,
                    "is_own": normalize_name(tname) == our_norm,
                    "roster": slice_data["roster_count"],
                    "jornadas_w": kc["jornadas_w"], "jornadas_l": kc["jornadas_l"],
                    "eff": kc["indiv_eff"], "game_ave": kc["game_ave"],
                })

        full_team_payload = {
            "meta": {
                "team_name": tname,
                "monogram": "".join(w[0] for w in tname.split()[:2]).upper(),
                "category": category_name,
                "club": tmeta.get("club", "Tennis Open League"),
                "our_team": our_team_name,
                "current_season": current_season,
                "available_seasons": seasons_order,
                "authors": cfg.get("authors", "Scouting TOL + CTA"),
                "generated_date": extracted_at,
                "version": "Auto-TOL v4.0",
            },
            "season_comparison": season_comparison,
            "by_season": by_season_payload,
        }

        slug = slugify(tname)
        json_blob = json.dumps(full_team_payload, ensure_ascii=False).replace("</", "<\\/")
        html = template_html.replace("__PAGE_TITLE__", f"{tname} — {category_name} | Scouting")
        html = html.replace("/*__REPORT_DATA__*/ {}", json_blob)
        (teams_dir / f"{slug}.html").write_text(html, encoding="utf-8")

    # Armar portada principal multi-temporada
    portal_by_season: Dict[str, Dict[str, Any]] = {}
    for sk in season_keys_all:
        cards = portal_cards_by_key[sk]
        cards.sort(key=lambda x: (0 if x["is_own"] else 1, -x["jornadas_w"], -x["eff"], x["name"]))
        portal_by_season[sk] = {
            "label": "Histórico (Todas)" if sk == "ALL" else sk,
            "bracket_columns": brackets_by_key[sk],
            "standings_by_group": {} if sk == "ALL" else standings_by_season[sk],
            "standings_recalc": False if sk == "ALL" else recalc_by_season[sk],
            "cards": cards,
        }

    portal_payload = {
        "category": category_name,
        "our_team": our_team_name,
        "current_season": current_season,
        "available_seasons": seasons_order,
        "updated_at": extracted_at,
        "by_season": portal_by_season,
    }
    (out_dir / "index.html").write_text(render_portal_index(portal_payload), encoding="utf-8")
    print(f"[OK] Portal v3 generado: {len(all_teams)} equipos x {len(season_keys_all)} vistas de temporada ({seasons_order}).")


if __name__ == "__main__":
    main()
