#!/usr/bin/env python3
"""
Compilador del Portal de Scouting (TOL + CTA -> docs/index.html + docs/equipos/*.html)
======================================================================================
Toma `data/tol_snapshot.json` (generado por `extraer_tol.py`) + opcional `data/cta_ranking.csv`
y construye automáticamente:
  - Un reporte HTML standalone para CADA equipo de la categoría
  - Una portada móvil `docs/index.html` con tabla de posiciones y buscador rápido de rivales

Uso normal:
    python generar_portal.py

Modo prueba (sin conectarse a TOL, usa datos de ejemplo):
    python generar_portal.py --demo
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
        rec = cta_ced[ced]
        return {"verified": True, "match_method": "Cédula exacta", **rec}
    nm = normalize_name(player.get("name", ""))
    if nm in cta_name:
        rec = cta_name[nm]
        return {"verified": True, "match_method": "Nombre exacto", **rec}
    if nm and cta_name:
        best = difflib.get_close_matches(nm, cta_name.keys(), n=1, cutoff=0.88)
        if best:
            rec = cta_name[best[0]]
            return {"verified": True, "match_method": "Nombre aproximado", **rec}
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


def build_team_view_of_matches(team_name: str, raw_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convierte los encuentros globales a la perspectiva de un equipo específico."""
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


def compute_season_stats(matches: List[Dict[str, Any]], season: str) -> Dict[str, Any]:
    sm = [m for m in matches if m["season"] == season]
    jw = sum(1 for m in sm if m["result"] == "V")
    jl = sum(1 for m in sm if m["result"] == "D")
    cw = sum(1 for m in sm if m["score_for"] == 3 and m["score_against"] == 2)
    cl = sum(1 for m in sm if m["score_for"] == 2 and m["score_against"] == 3)
    mw = ml = sw = sl = gw = gl = stbw = stbl = 0
    for m in sm:
        for d in m.get("doubles", []):
            if d["result"] == "V":
                mw += 1
            else:
                ml += 1
            st = analyze_double_sets(d.get("sets", []), d.get("walkover", False))
            sw += st["sets_w"]
            sl += st["sets_l"]
            gw += st["games_w"]
            gl += st["games_l"]
            if st["played_stb"]:
                if st["won_stb"]:
                    stbw += 1
                else:
                    stbl += 1
    tm, ts, tg, tstb = mw + ml, sw + sl, gw + gl, stbw + stbl
    return {
        "season": season, "jornadas_w": jw, "jornadas_l": jl,
        "matches_w": mw, "matches_l": ml, "indiv_eff": (mw / tm * 100) if tm else 0.0,
        "sets_w": sw, "sets_l": sl, "set_ave": (sw / ts * 100) if ts else 0.0,
        "games_w": gw, "games_l": gl, "game_ave": (gw / tg * 100) if tg else 0.0,
        "stb_w": stbw, "stb_l": stbl, "stb_eff": (stbw / tstb * 100) if tstb else 0.0,
        "close_w": cw, "close_l": cl,
    }


def compile_team_payload(
    team_name: str,
    team_meta: Dict[str, Any],
    roster: List[Dict[str, Any]],
    matches: List[Dict[str, Any]],
    standings: List[Dict[str, Any]],
    group_name: str,
    cfg: Dict[str, Any],
    current_season: str,
    category_name: str,
    extracted_at: str,
    cta_ced: Dict[str, Dict],
    cta_name: Dict[str, Dict],
) -> Dict[str, Any]:
    seasons_present = sorted({m["season"] for m in matches}, reverse=True)
    if current_season in seasons_present:
        seasons_present.remove(current_season)
        seasons_present.insert(0, current_season)
    elif not seasons_present:
        seasons_present = [current_season]

    season_comparison = [compute_season_stats(matches, s) for s in seasons_present]
    kpis_curr = season_comparison[0]

    # Posiciones D1-D5
    pos_stats = []
    for pos in ("D1", "D2", "D3", "D4", "D5"):
        w = l = 0
        pair_rec: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for m in matches:
            if m["season"] != current_season:
                continue
            for d in m["doubles"]:
                if d["pos"] == pos:
                    pk = canonical_pair(d["team_players"][0], d["team_players"][1])
                    if d["result"] == "V":
                        w += 1
                        pair_rec[pk][0] += 1
                    else:
                        l += 1
                        pair_rec[pk][1] += 1
        tot = w + l
        top_p, top_r = ("Sin datos", "0–0")
        if pair_rec:
            best = max(pair_rec.items(), key=lambda kv: (kv[1][0] + kv[1][1], kv[1][0]))
            top_p, top_r = best[0], f"{best[1][0]}–{best[1][1]}"
        pos_stats.append({"pos": pos, "w": w, "l": l, "eff": (w / tot * 100) if tot else 0.0, "unique_pairs": len(pair_rec), "top_pair": top_p, "top_pair_record": top_r})

    # Parejas
    def build_pairs(s_filter: Optional[str]) -> List[Dict[str, Any]]:
        agg: Dict[str, Dict[str, Any]] = {}
        for m in matches:
            if s_filter and m["season"] != s_filter:
                continue
            for d in m["doubles"]:
                pk = canonical_pair(d["team_players"][0], d["team_players"][1])
                if pk not in agg:
                    agg[pk] = {"pair_name": pk, "w": 0, "l": 0, "game_diff": 0, "positions": Counter()}
                st = analyze_double_sets(d.get("sets", []), d.get("walkover", False))
                agg[pk]["w" if d["result"] == "V" else "l"] += 1
                agg[pk]["game_diff"] += (st["games_w"] - st["games_l"])
                agg[pk]["positions"][d["pos"]] += 1
        res = []
        for it in agg.values():
            pj = it["w"] + it["l"]
            res.append({"pair_name": it["pair_name"], "pj": pj, "w": it["w"], "l": it["l"], "eff": (it["w"] / pj * 100) if pj else 0.0, "game_diff": it["game_diff"], "positions": sorted(it["positions"].keys())})
        res.sort(key=lambda x: (-x["pj"], -x["eff"], -x["game_diff"]))
        return res

    pairs_curr = build_pairs(current_season)
    pairs_all = build_pairs(None)

    # Jugadores y heatmap
    roster_map = {normalize_name(p["name"]): p for p in roster}
    hist_by_p: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seasons_by_p: Dict[str, set] = defaultdict(set)
    pos_curr_by_p: Dict[str, Counter] = defaultdict(Counter)

    for m in matches:
        for d in m["doubles"]:
            p1, p2 = d["team_players"][0], d["team_players"][1]
            for idx, pname in enumerate((p1, p2)):
                partner = p2 if idx == 0 else p1
                k = normalize_name(pname)
                seasons_by_p[k].add(m["season"])
                if m["season"] == current_season:
                    pos_curr_by_p[k][d["pos"]] += 1
                hist_by_p[k].append({
                    "season": m["season"], "round": m["round"], "date": m["date"],
                    "pos": d["pos"], "result": d["result"], "score_str": d["score_str"],
                    "partner": partner, "opp_pair": d["opp_pair"], "rival_team": m["opponent"],
                })

    players_out = []
    heatmap_out = []
    for k in (set(roster_map.keys()) | set(hist_by_p.keys())):
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
        is_deb = (seasons_by_p.get(k, set()) == {current_season}) and len(seasons_present) > 1
        players_out.append({
            "id": normalize_cedula(base.get("cedula")) or re.sub(r"\W+", "_", k),
            "name": base["name"], "cedula": base.get("cedula", "—"),
            "elo": float(base.get("elo") or 0.0), "pj_all": pj, "v_all": v, "eff_all": eff,
            "is_debutant": is_deb, "tactical_note": base.get("tactical_note", ""),
            "fixed_partner_note": fixed_note, "cta": match_player_cta(base, cta_ced, cta_name),
            "history": hist,
        })
        pc = pos_curr_by_p.get(k, Counter())
        if sum(pc.values()) > 0:
            heatmap_out.append({"name": base["name"], "D1": pc["D1"], "D2": pc["D2"], "D3": pc["D3"], "D4": pc["D4"], "D5": pc["D5"], "total": sum(pc.values())})

    players_out.sort(key=lambda x: (-x["eff_all"], -x["pj_all"], -x["elo"]))
    heatmap_out.sort(key=lambda x: (-x["total"], -x["D1"], -x["D2"], x["name"]))

    # H2H contra nuestro equipo
    our_team = cfg.get("our_team", "Andrew Tennis A")
    h2h = sorted([m for m in matches if normalize_name(m["opponent"]) == normalize_name(our_team)], key=lambda x: x["date"], reverse=True)

    # Auto-insights (6 tarjetas)
    matches_curr = [m for m in matches if m["season"] == current_season]
    pos_grp = next((s["pos"] for s in standings if normalize_name(s["team"]) == normalize_name(team_name)), "—")
    pts_grp = next((s["pts"] for s in standings if normalize_name(s["team"]) == normalize_name(team_name)), "—")
    best_pos = max(pos_stats, key=lambda x: (x["eff"], x["w"]))
    weak_pos = min(pos_stats, key=lambda x: (x["eff"], -x["l"]))
    unbeaten = [p["name"] for p in players_out if sum(1 for h in p["history"] if h["season"] == current_season) >= 3 and all(h["result"] == "V" for h in p["history"] if h["season"] == current_season)]
    close_m = [m for m in matches_curr if (m["score_for"], m["score_against"]) in {(3, 2), (2, 3)}]
    top4_names = {normalize_name(s["team"]) for s in standings[:4] if normalize_name(s["team"]) != normalize_name(team_name)}
    vs_top4 = [m for m in matches_curr if normalize_name(m["opponent"]) in top4_names]
    t4_w = sum(1 for m in vs_top4 if m["result"] == "V")

    insights_30s = [
        {"kicker": f"Temporada en curso ({current_season})", "headline": f"{pos_grp}° de {group_name} ({kpis_curr['jornadas_w']}–{kpis_curr['jornadas_l']} en jornadas)", "body": f"Suma {pts_grp} pts con {kpis_curr['matches_w']}–{kpis_curr['matches_l']} en dobles ({kpis_curr['indiv_eff']:.1f}%) y {kpis_curr['stb_w']}–{kpis_curr['stb_l']} en súper tie-breaks.", "tone": "positive" if kpis_curr["jornadas_w"] >= kpis_curr["jornadas_l"] else "warn"},
        {"kicker": "Historial directo", "headline": f"{'Ganó' if h2h[0]['result']=='V' else 'Perdió'} {h2h[0]['score_for']}–{h2h[0]['score_against']} vs {our_team} ({h2h[0]['date']})" if h2h else f"Sin cruces previos vs {our_team}", "body": f"Diferencial en ese cruce: {h2h[0]['games_for']}–{h2h[0]['games_against']} ({h2h[0]['game_diff']:+d}). Cruces analizados: {len(h2h)}." if h2h else "Aún no se registran partidos contra nuestro equipo en el histórico cargado.", "tone": "info"},
        {"kicker": "Núcleo fuerte", "headline": f"{len(unbeaten)} jugador(es) invicto(s) en {current_season}" if unbeaten else "Rotación repartida en plantilla", "body": (f"Invictos (≥3 PJ): {', '.join(unbeaten[:4])}." if unbeaten else f"Roster activo de {len(players_out)} jugadores registrados."), "tone": "positive" if unbeaten else "default"},
        {"kicker": "Rendimiento por posición", "headline": f"{best_pos['pos']} es su punto más fuerte ({best_pos['w']}–{best_pos['l']})", "body": f"Promedia {best_pos['eff']:.0f}% en {best_pos['pos']} (base: {best_pos['top_pair']}). Flanco más vulnerable: {weak_pos['pos']} ({weak_pos['w']}–{weak_pos['l']}).", "tone": "default"},
        {"kicker": "Fuerza ante el Top 4", "headline": f"{t4_w}–{len(vs_top4) - t4_w} contra equipos de arriba", "body": "Cruces: " + (", ".join(f"{m['opponent']} ({m['score_for']}–{m['score_against']})" for m in vs_top4[:3]) or "Sin cruces ante Top 4 registrados."), "tone": "info"},
        {"kicker": "Margen real vs marcador", "headline": f"{len(close_m)} jornada(s) cerrada(s) 3–2" if close_m else f"Diferencial total: {kpis_curr['games_w'] - kpis_curr['games_l']:+d} games", "body": ("Games en 3–2: " + ", ".join(f"{m['opponent']} ({m['game_diff']:+d})" for m in close_m[:4]) + ".") if close_m else f"Efectividad real en games del {kpis_curr['game_ave']:.1f}%.", "tone": "warn" if close_m else "default"},
    ]

    # Notas tácticas automáticas
    fixed_pairs = [p for p in pairs_curr if p["pj"] >= 3]
    tactical_notes = [
        f"<strong>Parejas consolidadas en {current_season}:</strong> " + (", ".join(f"<strong>{p['pair_name']}</strong> ({p['w']}–{p['l']} en {'/'.join(p['positions'])})" for p in fixed_pairs[:3]) if fixed_pairs else "Alta rotación de parejas; no registra duplas repetidas en 3 o más jornadas."),
        f"<strong>Lectura por posición:</strong> su mejor rendimiento está en <strong>{best_pos['pos']}</strong> ({best_pos['w']}–{best_pos['l']}) y su punto más atacable en <strong>{weak_pos['pos']}</strong> ({weak_pos['w']}–{weak_pos['l']}, {weak_pos['unique_pairs']} parejas distintas probadas).",
    ]

    return {
        "meta": {
            "team_name": team_name,
            "monogram": "".join(w[0] for w in team_name.split()[:2]).upper(),
            "category": category_name,
            "club": team_meta.get("club", "Tennis Open League"),
            "our_team": our_team,
            "current_season": current_season,
            "group_name": group_name,
            "authors": cfg.get("authors", "Scouting TOL + CTA"),
            "generated_date": extracted_at,
            "version": "Auto-TOL v2.0",
            "next_match": "",
            "standings_note": f"Datos actualizados automáticamente desde EpicenterSport el {extracted_at}.",
        },
        "roster_count": len(players_out),
        "kpis_current": kpis_curr,
        "hero_summary": f"Balance actual en {current_season}: {kpis_curr['jornadas_w']}–{kpis_curr['jornadas_l']} en jornadas y {kpis_curr['matches_w']}–{kpis_curr['matches_l']} en partidos de dobles ({kpis_curr['indiv_eff']:.1f}%).",
        "insights_30s": insights_30s,
        "tactical_notes": tactical_notes,
        "h2h_matches": h2h,
        "h2h_summary": "",
        "standings": standings,
        "jornadas": matches,
        "players": players_out,
        "position_stats": pos_stats,
        "season_comparison": season_comparison,
        "pairs_current": pairs_curr,
        "pairs_all": pairs_all,
        "position_heatmap": heatmap_out,
    }


def render_portal_index(cards: List[Dict[str, Any]], standings_by_group: Dict[str, List[Dict[str, Any]]], cfg: Dict[str, Any], season: str, category: str, updated_at: str) -> str:
    our_team = cfg.get("our_team", "Andrew Tennis A")
    cards_json = json.dumps(cards, ensure_ascii=False).replace("</", "<\\/")
    standings_html = ""
    for gname, rows in standings_by_group.items():
        if not rows:
            continue
        trs = "".join(
            f'<tr class="{"own" if normalize_name(r["team"])==normalize_name(our_team) else ""}"><td>{r["pos"]}</td><td><a href="equipos/{slugify(r["team"])}.html">{r["team"]}</a></td><td class="num"><strong>{r["pts"]}</strong></td><td class="num">{r["set_ave"]:.1f}%</td><td class="num">{r["game_ave"]:.1f}%</td></tr>'
            for r in rows
        )
        standings_html += f'<div class="card"><h3>{gname}</h3><div class="tw"><table><thead><tr><th>Pos</th><th>Equipo</th><th class="num">Pts</th><th class="num">Set%</th><th class="num">Game%</th></tr></thead><tbody>{trs}</tbody></table></div></div>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Scouting {category} — {our_team}</title>
  <style>
    :root {{ --bg:#f4f6f9; --surface:#fff; --border:#e2e8f0; --primary:#0f2c59; --soft:#e8eef8; --text:#0f172a; --muted:#64748b; --green:#166534; --gsoft:#dcfce7; }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:20px 16px 60px; }}
    .hero {{ background:linear-gradient(135deg,#0b1f3f,#153a6e); color:#fff; border-radius:16px; padding:26px; margin-bottom:22px; }}
    .hero h1 {{ font-size:clamp(22px,3vw,30px); margin-bottom:6px; }}
    .hero p {{ color:#cbd5e1; font-size:13.5px; }}
    .search {{ width:100%; padding:12px 16px; border:1px solid #cbd5e1; border-radius:10px; font-size:15px; margin-bottom:18px; outline:none; }}
    .search:focus {{ border-color:var(--primary); box-shadow:0 0 0 3px rgba(15,44,89,0.12); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(265px,1fr)); gap:14px; margin-bottom:28px; }}
    .team-card {{ display:block; text-decoration:none; color:inherit; background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; transition:transform .15s,border-color .15s,box-shadow .15s; }}
    .team-card:hover {{ transform:translateY(-2px); border-color:var(--primary); box-shadow:0 6px 18px rgba(15,44,89,0.1); }}
    .team-card.own {{ border:2px solid #16a34a; background:#f0fdf4; }}
    .tc-top {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; }}
    .tc-name {{ font-size:16px; font-weight:800; color:var(--primary); }}
    .badge {{ font-size:11.5px; font-weight:800; padding:3px 8px; border-radius:999px; background:var(--soft); color:var(--primary); }}
    .badge.ok {{ background:var(--gsoft); color:var(--green); }}
    .tc-meta {{ font-size:12.5px; color:var(--muted); display:flex; justify-content:space-between; margin-top:6px; }}
    h2 {{ font-size:18px; color:var(--primary); margin-bottom:12px; }}
    .standings-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }}
    .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
    .card h3 {{ font-size:15px; color:var(--primary); margin-bottom:10px; }}
    .tw {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:8px 10px; border-bottom:1px solid var(--border); text-align:left; }}
    th {{ font-size:11px; text-transform:uppercase; color:var(--muted); }}
    td a {{ color:var(--primary); font-weight:700; text-decoration:none; }}
    td a:hover {{ text-decoration:underline; }}
    tr.own td {{ background:#f0fdf4; }}
    .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:32px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <h1>🎾 Portal de Scouting — {category}</h1>
      <p>Temporada: <strong>{season}</strong> · Equipo base: <strong>{our_team}</strong> · Actualizado: {updated_at}</p>
    </header>

    <h2>Reportes por Equipo (clic para abrir el análisis completo)</h2>
    <input class="search" id="q" type="text" placeholder="Buscar equipo rival..." />
    <div class="grid" id="team-grid"></div>

    {f'<h2>Tablas de Posiciones</h2><div class="standings-grid">{standings_html}</div>' if standings_html else ''}

    <footer>Actualización automática TOL + CTA · GitHub Pages</footer>
  </div>
  <script>
    const TEAMS = {cards_json};
    const grid = document.getElementById("team-grid");
    const q = document.getElementById("q");
    function draw() {{
      const term = q.value.trim().toLowerCase();
      const filtered = TEAMS.filter(t => t.name.toLowerCase().includes(term) || t.group.toLowerCase().includes(term));
      grid.innerHTML = filtered.map(t => `
        <a class="team-card ${{t.is_own ? "own" : ""}}" href="equipos/${{t.slug}}.html">
          <div class="tc-top">
            <div class="tc-name">${{t.name}}</div>
            <span class="badge ${{t.eff >= 60 ? "ok" : ""}}">${{t.jornadas_w}}–${{t.jornadas_l}}</span>
          </div>
          <div style="font-size:12.5px;color:#475569">${{t.group}} · ${{t.roster}} jugadores</div>
          <div class="tc-meta"><span>Efectividad: <strong>${{t.eff.toFixed(0)}}%</strong></span><span>Games: ${{t.game_ave.toFixed(1)}}%</span></div>
        </a>
      `).join("");
    }}
    q.addEventListener("input", draw);
    draw();
  </script>
</body>
</html>"""


def build_demo_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot mínimo de prueba para verificar todo el portal sin conexión a TOL."""
    return {
        "extracted_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "current_season": "T2 2026",
        "category_name": cfg.get("category_name", "6ta Masculino"),
        "teams_meta": {
            "Pan de Azúcar C": {"id": "demo-1", "name": "Pan de Azúcar C", "club": "Club Campestre Pan de Azúcar"},
            "Andrew Tennis A": {"id": "demo-2", "name": "Andrew Tennis A", "club": "El Laguito"},
        },
        "rosters_by_team": {
            "Pan de Azúcar C": [
                {"id": "p1", "name": "Jose Goncalvez", "cedula": "17845120", "elo": 274.4},
                {"id": "p2", "name": "Guillermo Haddad", "cedula": "18234901", "elo": 266.1},
                {"id": "p3", "name": "Amaru Araujo", "cedula": "15901234", "elo": 252.3},
                {"id": "p4", "name": "Michael Sosa", "cedula": "16096639", "elo": 260.65},
            ],
            "Andrew Tennis A": [
                {"id": "a1", "name": "Ruben Gonzalez", "cedula": "15000001", "elo": 262.0},
                {"id": "a2", "name": "Juan Diego Garcia", "cedula": "16000002", "elo": 258.5},
                {"id": "a3", "name": "Angel Tarazona", "cedula": "17000003", "elo": 255.0},
                {"id": "a4", "name": "Oscar Suinaga", "cedula": "18000004", "elo": 264.2},
            ],
        },
        "standings_by_group": {
            "Grupo B": [
                {"pos": 1, "team": "Pan de Azúcar C", "pts": 57, "set_ave": 65.7, "game_ave": 56.6},
                {"pos": 2, "team": "Andrew Tennis A", "pts": 49, "set_ave": 63.2, "game_ave": 55.3},
            ]
        },
        "matches": [
            {
                "id": "m1", "season": "T2 2026", "phase": "Fase Regular", "round": "Jornada 5",
                "date": "2026-10-18", "local_team": "Pan de Azúcar C", "visitor_team": "Andrew Tennis A",
                "local_score": 3, "visitor_score": 2,
                "doubles": [
                    {"pos": "D1", "local_players": ["Jose Goncalvez", "Guillermo Haddad"], "visitor_players": ["Ruben Gonzalez", "Juan Diego Garcia"], "sets_local": [[6, 3], [4, 6], [10, 8]], "walkover": False, "winner_side": "local"},
                    {"pos": "D4", "local_players": ["Amaru Araujo", "Michael Sosa"], "visitor_players": ["Angel Tarazona", "Oscar Suinaga"], "sets_local": [[6, 4], [6, 0]], "walkover": False, "winner_side": "local"},
                ],
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera el portal completo de scouting (docs/index.html + reportes por equipo).")
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--snapshot", default="data/tol_snapshot.json", type=Path)
    parser.add_argument("--template", default="plantilla_reporte.html", type=Path)
    parser.add_argument("--demo", action="store_true", help="Genera el portal con datos de prueba.")
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    snapshot = build_demo_snapshot(cfg) if args.demo else json.loads(args.snapshot.read_text(encoding="utf-8"))

    cta_ced, cta_name = load_cta_csv(Path(cfg.get("cta_csv_path", "data/cta_ranking.csv")))
    template_html = args.template.read_text(encoding="utf-8")

    out_dir = Path(cfg.get("output_dir", "docs"))
    teams_dir = out_dir / "equipos"
    teams_dir.mkdir(parents=True, exist_ok=True)
    # Desactivar Jekyll en GitHub Pages
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    standings_by_group = snapshot.get("standings_by_group", {})
    team_to_group: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
    for gname, rows in standings_by_group.items():
        for r in rows:
            team_to_group[normalize_name(r["team"])] = (gname, rows)

    all_teams = sorted(snapshot.get("teams_meta", {}).keys())
    portal_cards = []
    current_season = snapshot["current_season"]
    category_name = snapshot["category_name"]
    extracted_at = snapshot["extracted_at"]
    our_norm = normalize_name(cfg.get("our_team", "Andrew Tennis A"))

    for tname in all_teams:
        tmeta = snapshot["teams_meta"][tname]
        roster = snapshot.get("rosters_by_team", {}).get(tname, [])
        t_matches = build_team_view_of_matches(tname, snapshot.get("matches", []))
        gname, g_standings = team_to_group.get(normalize_name(tname), ("General", []))

        payload = compile_team_payload(
            tname, tmeta, roster, t_matches, g_standings, gname,
            cfg, current_season, category_name, extracted_at, cta_ced, cta_name
        )

        slug = slugify(tname)
        json_blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        html = template_html.replace("__PAGE_TITLE__", f"{tname} — {category_name} | Scouting")
        html = html.replace("/*__REPORT_DATA__*/ {}", json_blob)
        (teams_dir / f"{slug}.html").write_text(html, encoding="utf-8")

        kc = payload["kpis_current"]
        portal_cards.append({
            "name": tname, "slug": slug, "group": gname,
            "is_own": normalize_name(tname) == our_norm,
            "roster": payload["roster_count"],
            "jornadas_w": kc["jornadas_w"], "jornadas_l": kc["jornadas_l"],
            "eff": kc["indiv_eff"], "game_ave": kc["game_ave"],
        })

    portal_cards.sort(key=lambda x: (0 if x["is_own"] else 1, -x["jornadas_w"], -x["eff"], x["name"]))
    index_html = render_portal_index(portal_cards, standings_by_group, cfg, current_season, category_name, extracted_at)
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"[OK] Portal generado en {out_dir}/index.html con {len(portal_cards)} reportes de equipos.")


if __name__ == "__main__":
    main()
