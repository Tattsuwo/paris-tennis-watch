#!/usr/bin/env python3
"""
Un passage de surveillance de tennispadel.paris.fr (pensé pour GitHub Actions).

- Détecte l'apparition d'un nouveau jour réservable -> heure d'ouverture, par centre.
- Suit le nombre de créneaux libres par centre -> repère les annulations et leur arrondissement.
- Lecture de la page publique uniquement : pas de connexion, pas de réservation.

Fichiers écrits dans docs/data/ (heure de Paris) :
  requetes.csv  une ligne par passage : timestamp, statut, nb_jours, dernier_jour, detail
  events.csv    timestamp, type, date_jeu, centre, arrondissement, avant, apres
  log.csv       timestamp, date_jeu, creneaux_dispo, centres_avec_dispo   (totaux)
  state.json    état du passage précédent
"""
import csv, html, json, os, re, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BASE = "https://tennispadel.paris.fr/tennispadel/jsp/site/Portal.jsp"
SPORT = os.environ.get("SPORT", "1")          # 1 = tennis, 3 = padel
WATCH_LAST_N_DAYS = int(os.environ.get("WATCH_LAST_N_DAYS", "4"))
# Centre "sentinelle" : sa page liste tous les jours réservables (J à J+14),
# alors que la liste de dates de la recherche s'arrête à J+13.
SENTINEL_ID = os.environ.get("SENTINEL_ID", "257")   # 257 = Rigoulot - La Plaine

# Centres suivis (id sur le site -> nom affiché). Vider le dictionnaire pour suivre tous les centres.
CENTRES_SUIVIS = {
    "109": "Dunois",                   # 13e
    "218": "Moureu - Baudricourt",     # 13e
    "76":  "Cordelières",              # 13e
    "60":  "Georges Carpentier",       # 13e
    "272": "Poterne des Peupliers",    # 13e
    "257": "Rigoulot - La Plaine",     # 15e
    "120": "Elisabeth",                # 14e
}
_SUIVIS = {n.lower() for n in CENTRES_SUIVIS.values()}
# Notifications d'annulation : uniquement ces créneaux (centre, jour de la semaine, heure).
# Définies dans le secret GitHub ALERTES (JSON) pour ne pas être visibles dans le dépôt public, ex. :
# [{"centre": "Nom du centre", "jour": "Lundi", "heure": "18:00"}]
try:
    ALERTES = json.loads(os.environ.get("ALERTES", "") or "[]")
except ValueError:
    print("Secret ALERTES illisible (JSON attendu) : alertes désactivées", file=sys.stderr)
    ALERTES = []
# True = recevoir aussi les notifications pour toutes les autres annulations.
NOTIF_GENERALES = False

ARR_CONNUS = {"Dunois": "13e", "Moureu - Baudricourt": "13e", "Cordelières": "13e",
              "Georges Carpentier": "13e", "Poterne des Peupliers": "13e",
              "Rigoulot - La Plaine": "15e", "Elisabeth": "14e"}


def suivi(name):
    return not _SUIVIS or name.lower() in _SUIVIS
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")
STATE = os.path.join(DATA_DIR, "state.json")
LOG = os.path.join(DATA_DIR, "log.csv")
EVENTS = os.path.join(DATA_DIR, "events.csv")
REQUETES = os.path.join(DATA_DIR, "requetes.csv")
CENTRES = os.path.join(DATA_DIR, "centres.csv")
UA = {"User-Agent": "Mozilla/5.0 (veille perso)"}
TZ = ZoneInfo("Europe/Paris")

DATE_RE = re.compile(r'name="dateCheckbox-desktop"[^>]*value="([^"]+)"')
COUNT_RE = re.compile(r"^(\d+)\s*Cr[ée]neaux?\s+disponibles?$", re.I)
NONE_RE = re.compile(r"^(Aucun cr[ée]neau disponible|Centre ferm[ée].*)$", re.I)
CP_RE = re.compile(r"\b(\d{5})\b")

H_EVENTS = ["timestamp", "type", "date_jeu", "centre", "arrondissement", "avant", "apres", "creneau"]


SESSION = requests.Session()
SESSION.headers.update(UA)


def retry(call):
    """Le site renvoie parfois une erreur 502 passagère : on réessaie une fois après 5 s."""
    try:
        r = call()
        r.raise_for_status()
    except requests.RequestException:
        time.sleep(5)
        r = call()
        r.raise_for_status()
    return r


def fetch(params):
    return retry(lambda: SESSION.get(BASE, params=params, timeout=30)).text


def search(date_label):
    """Le filtre de date passe par le formulaire (POST) : une simple URL avec date1 est ignorée."""
    data = {
        "action": "search", "fieldSport-desktop": SPORT,
        "dateCheckbox-desktop": date_label,
        "selectHourFrom-desktop": "08:00", "selectHourTo-desktop": "22:00",
        "mobileViewType": "list",
    }
    return retry(lambda: SESSION.post(BASE, params={"version": "desktop", "page": "paristennis", "sport": SPORT},
                                      data=data, timeout=30)).text


def lines_of(page):
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    txt = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [re.sub(r"\s+", " ", l).strip() for l in txt.split("\n") if l.strip()]


def arrondissement(cp):
    if not cp:
        return "?"
    if cp == "75116":
        return "16e"
    if cp.startswith("750") and 1 <= int(cp[3:]) <= 20:
        n = int(cp[3:])
        return "1er" if n == 1 else f"{n}e"
    return cp  # hors Paris : code postal


HOME = {"page": ""}
DAY_RE = re.compile(r"^(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche) \d{2} \S+$")
SLOT_RE = re.compile(r"^\d{2}:\d{2}$")
COURT_RE = re.compile(r"^Court n°\s*0*(\d+)", re.I)
HORAIRES = os.path.join(DATA_DIR, "horaires.json")
HORAIRES_EVERY_MIN = int(os.environ.get("HORAIRES_EVERY_MIN", "10"))
ID_RE = re.compile(r"page=centre-sportif(?:&amp;|&)id=(\d+)")
TITLE_RE = re.compile(r"<title>\s*TENNIS PADEL\s*-\s*(.*?)\s*-\s*Consultation", re.I | re.S)


MOIS = {"janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
        "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12}


def date_of(label):
    """'Lundi 07 décembre' -> '2026-12-07' (année déduite : jamais plus de 6 mois dans le passé)."""
    m = re.match(r"^\S+ (\d{1,2}) (\S+)$", label)
    if not m or m.group(2) not in MOIS:
        return None
    today = datetime.now(TZ).date()
    d = datetime(today.year, MOIS[m.group(2)], int(m.group(1))).date()
    if (today - d).days > 180:
        d = d.replace(year=today.year + 1)
    return d.isoformat()


def available_dates():
    page = fetch({"page": "paristennis", "sport": SPORT})
    HOME["page"] = page
    out = []
    for d in DATE_RE.findall(page):
        d = html.unescape(d)
        if d not in out:
            out.append(d)
    return out


def centre_ids():
    if CENTRES_SUIVIS:
        return list(CENTRES_SUIVIS)
    return list(dict.fromkeys(ID_RE.findall(HOME["page"])))


def centre_page(cid):
    """Page d'un centre : (nom, {jour: nb de créneaux libres}) sur toute la fenêtre de réservation."""
    # Requête SANS les cookies de la session : sinon la page du centre reprend le filtre
    # de date de la dernière recherche et n'affiche qu'un seul jour.
    page = retry(lambda: requests.get(BASE, params={"page": "centre-sportif", "id": cid},
                                      headers=UA, timeout=60)).text
    m = TITLE_RE.search(page)
    name = html.unescape(m.group(1)).strip() if m else f"centre {cid}"
    days, detail, cur, court = {}, {}, None, ""
    for l in lines_of(page):
        if DAY_RE.match(l):
            cur, court = l, ""
            days.setdefault(cur, 0)
            detail.setdefault(cur, [])
        elif COURT_RE.match(l):
            court = COURT_RE.match(l).group(1)
        elif cur and SLOT_RE.match(l):
            days[cur] += 1
            detail[cur].append([l, court])
    return name, days, detail


def centres_for(date_label):
    """Retourne {centre: (arrondissement, nb_creneaux)} pour un jour de jeu."""
    ls = lines_of(search(date_label))
    res = {}
    for i, l in enumerate(ls):
        m = COUNT_RE.match(l)
        if not (m or NONE_RE.match(l)) or i == 0:
            continue
        name = ls[i - 1]
        cp = None
        for nxt in ls[i + 1:i + 4]:
            c = CP_RE.search(nxt)
            if c:
                cp = c.group(1)
                break
        if suivi(name):
            res[name] = (arrondissement(cp), int(m.group(1)) if m else 0)
    return res


def append(path, header, row):
    if os.path.exists(path):  # ancien format -> archivé
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != ",".join(header):
                os.replace(path, path.replace(".csv", "_ancien.csv"))
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def notify(title, msg, click=None):
    # rien du contenu dans les journaux publics de GitHub Actions
    print("notification envoyée" if NTFY_TOPIC else "notification non envoyée (NTFY_TOPIC absent)", flush=True)
    if NTFY_TOPIC:
        import base64
        headers = {"Title": "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?="}
        if click:
            headers["Click"] = click
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                          headers=headers, timeout=10)
        except requests.RequestException:
            pass


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    state = {"dates": [], "centres": {}}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("centres", {})
    state.setdefault("arr", {})
    state.setdefault("remplissage", {})
    state.setdefault("ouv_centres", {})   # {jour: {centre: heure à laquelle il a ouvert ce jour}}

    try:
        dates = available_dates()
        if not dates:
            raise RuntimeError("aucune date trouvée (la page a peut-être changé)")

        # --- vraie ouverture : nouveau jour sur la page du centre témoin (J à J+14) ---
        s_name, s_days, s_detail = centre_page(SENTINEL_ID)
        s_list = list(s_days)
        prev_s = state.get("sentinel_days") or []
        # Page anormale : 1 seul jour (filtre de recherche resté actif) ou chute brutale du nombre
        # de jours. Une fenêtre qui raccourcit d'1 jour par jour (pause des ouvertures) reste normale.
        if len(s_list) <= 1:
            raise RuntimeError(f"page du centre témoin anormale ({len(s_list)} jour(s))")
        chute = len(prev_s) >= 5 and len(s_list) < len(prev_s) - 3   # signalé dans le journal, sans bloquer
        # Ouverture = jour plus lointain que le plus lointain jamais vu. Un jour qui réapparaît
        # (centre témoin complet puis annulation) n'est donc pas pris pour une ouverture.
        dated = {d: date_of(d) for d in s_list}
        max_vu = state.get("max_jour")
        if not max_vu and len(prev_s) >= 10:          # migration depuis l'ancien état
            max_vu = max((date_of(d) for d in prev_s), default=None)
        opened = [d for d in s_list if max_vu and dated[d] and dated[d] > max_vu] if max_vu else []
        candidats = [v for v in dated.values() if v] + ([max_vu] if max_vu else [])
        state["max_jour"] = max(candidats) if candidats else max_vu

        # Jours suivis : les derniers de la recherche + ceux au-delà (J+14), que la recherche
        # accepte même s'ils ne sont pas proposés dans sa liste de dates.
        extra = [d for d in s_list if d not in dates]
        # + les jours concernés par une alerte (ex. tous les vendredis de la fenêtre)
        alert_days = [d for d in s_list if any(d.split()[0] == a["jour"] for a in ALERTES)]
        watched = list(dict.fromkeys(dates[-WATCH_LAST_N_DAYS:] + extra + opened + alert_days))
        alert_check = False
        resume_ouvertures = []
        visibles = set(dates[-WATCH_LAST_N_DAYS:] + extra + opened)  # jours publiés dans log/centres

        new_state_centres = {}
        totals = []
        arr_of = {**ARR_CONNUS, **state["arr"]}
        for d in watched:
            centres = centres_for(d)
            new_state_centres[d] = {c: n for c, (_, n) in centres.items()}
            totals.append(tuple(sorted(new_state_centres[d].items())))
            arr_of.update({c: a for c, (a, _) in centres.items()})
            if d in visibles:
                append(LOG, ["timestamp", "date_jeu", "creneaux_dispo", "centres_avec_dispo"],
                       [now, d, sum(n for _, n in centres.values()),
                        sum(1 for _, n in centres.values() if n > 0)])

            before_day = state["centres"].get(d)
            if d in opened:
                state["ouv_centres"][d] = {}
            # Ouverture centre par centre : un centre est « ouvert » pour ce jour dès qu'il y a des
            # créneaux. Ceux qui ouvrent après le centre témoin sont notés à leur propre heure.
            oc = state["ouv_centres"].get(d)
            if oc is not None:
                for c, (a, n) in sorted(centres.items(), key=lambda x: x[1][0]):
                    if n > 0 and c not in oc:
                        oc[c] = now
                        append(EVENTS, H_EVENTS, [now, "nouveau_jour", d, c, a, 0, n, ""])
            if d in opened:
                arrs = sorted({a for a, n in centres.values() if n > 0})
                total = sum(n for _, n in centres.values())
                state["remplissage"][d] = {"ouverture": now, "n0": total}
                resume_ouvertures.append(f"{d} ({total} créneaux, arr. {', '.join(arrs)})")
            elif before_day is not None:
                # alerte rapide (toutes les 2 min) ; l'horaire précis est enregistré au relevé des 15 min
                ups = [f"{c} ({a}) +{n - before_day[c]}" for c, (a, n) in centres.items()
                       if before_day.get(c) is not None and n > before_day[c]]
                if ups and NOTIF_GENERALES:
                    notify("Créneaux libérés", f"{d} : " + " ; ".join(ups))
                # hausse sur un centre/jour d'alerte -> vérifier tout de suite l'horaire précis
                for a in ALERTES:
                    c = a["centre"]
                    if d.split()[0] == a["jour"] and c in centres and before_day.get(c) is not None \
                            and centres[c][1] > before_day[c]:
                        alert_check = True
            # vitesse de remplissage : moment où le jour ouvert passe sous la moitié de ses créneaux
            rec = state["remplissage"].get(d)
            if rec and "moitie" not in rec and rec.get("n0"):
                total = sum(n for _, n in centres.values())
                if total <= rec["n0"] / 2:
                    t0 = datetime.strptime(rec["ouverture"], "%Y-%m-%d %H:%M:%S")
                    t1 = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                    rec["moitie"] = now
                    rec["minutes"] = round((t1 - t0).total_seconds() / 60)
            time.sleep(1)

        # suivi par centre : on oublie les jours qui ne sont plus suivis
        state["ouv_centres"] = {d: v for d, v in state["ouv_centres"].items() if d in watched}

        # une seule notification, même si plusieurs jours ouvrent d'un coup
        if resume_ouvertures:
            titre = "Nouveau jour ouvert" if len(resume_ouvertures) == 1 else f"{len(resume_ouvertures)} nouveaux jours ouverts"
            notify(titre, f"à {now[11:16]} : " + " ; ".join(resume_ouvertures))

        # garder 60 jours d'historique de remplissage
        limite = (datetime.now(TZ).replace(tzinfo=None)).timestamp() - 60 * 86400
        state["remplissage"] = {d: r for d, r in state["remplissage"].items()
                                if datetime.strptime(r["ouverture"], "%Y-%m-%d %H:%M:%S").timestamp() >= limite}

        suspect = len(totals) > 1 and len(set(totals)) == 1
        detail = ("ouverture : " + ", ".join(opened)) if opened else ""
        if suspect:
            detail = (detail + " ; " if detail else "") + "filtre de date non appliqué (jours identiques)"
        if chute:
            detail = (detail + " ; " if detail else "") + f"fenêtre réduite : {len(prev_s)} -> {len(s_list)} jours"
        append(REQUETES, ["timestamp", "statut", "nb_jours", "dernier_jour", "detail"],
               [now, "suspect" if suspect else "ok", len(s_list), s_list[-1] if s_list else "", detail])
        with open(CENTRES, "w", newline="", encoding="utf-8") as f:  # instantané, réécrit à chaque passage
            w = csv.writer(f)
            vis = [d for d in watched if d in visibles]
            w.writerow(["centre", "arrondissement"] + vis)
            for c in sorted(arr_of):
                w.writerow([c, arr_of[c]] + [new_state_centres[d].get(c, "") for d in vis])
        # --- horaires précis par centre, rafraîchis toutes les HORAIRES_EVERY_MIN minutes ---
        last_h = state.get("horaires_ts", "")
        due = not last_h or (datetime.now(TZ) - datetime.strptime(last_h, "%Y-%m-%d %H:%M:%S")
                             .replace(tzinfo=TZ)).total_seconds() >= HORAIRES_EVERY_MIN * 60 - 30
        if due or opened:
            old = {}
            if os.path.exists(HORAIRES):
                try:
                    with open(HORAIRES, encoding="utf-8") as f:
                        old = json.load(f).get("centres", {})
                except ValueError:
                    old = {}
            hor = {"maj": now, "intervalle_min": HORAIRES_EVERY_MIN, "centres": {}}
            freed = []
            for cid in centre_ids():
                name, _, slots = (s_name, s_days, s_detail) if cid == SENTINEL_ID else centre_page(cid)
                arr = arr_of.get(name, "?")
                hor["centres"][name] = {"arrondissement": arr, "id": cid, "jours": slots}
                # créneau libéré = horaire présent maintenant mais absent au relevé précédent
                old_days = old.get(name, {}).get("jours", {})
                for d, lst in slots.items():
                    if d not in old_days or d in opened:
                        continue
                    before = {tuple(x) for x in old_days[d]}
                    for t, court in lst:
                        if (t, court) not in before:
                            append(EVENTS, H_EVENTS, [now, "liberation", d, name, arr, "", "",
                                                      f"{t} (court {court})" if court else t])
                            freed.append(f"{name} {d} {t}")
                if cid != SENTINEL_ID:
                    time.sleep(1)
            if freed and NOTIF_GENERALES:
                notify("Horaires libérés", " ; ".join(freed[:10]) + (" …" if len(freed) > 10 else ""))
            with open(HORAIRES, "w", encoding="utf-8") as f:
                json.dump(hor, f, ensure_ascii=False, separators=(",", ":"))
            last_h = now

        # --- alertes ciblées (définies dans le secret ALERTES) ---
        if ALERTES:
            slots_by_name = None
            if due or opened:
                slots_by_name = {n: c["jours"] for n, c in hor["centres"].items()}
            elif alert_check:
                ids = {v.lower(): k for k, v in CENTRES_SUIVIS.items()}
                slots_by_name = {}
                for a in ALERTES:
                    cid = ids.get(a["centre"].lower())
                    if cid and a["centre"] not in slots_by_name:
                        name, _, slots = centre_page(cid)
                        slots_by_name[a["centre"]] = slots
            if slots_by_name is not None:
                current = []
                for a in ALERTES:
                    for d, lst in slots_by_name.get(a["centre"], {}).items():
                        if d.split()[0] != a["jour"]:
                            continue
                        for t, court in lst:
                            if t == a["heure"]:
                                current.append(f"{a['centre']}|{d}|{t}|{court}")
                import hashlib
                h = lambda k: hashlib.sha256((NTFY_TOPIC + k).encode()).hexdigest()[:16]  # illisible dans le dépôt
                prev = set(state.get("alertes_libres", []))
                for k in current:
                    if h(k) not in prev:
                        c, d, t, court = k.split("|")
                        cid = {v.lower(): k2 for k2, v in CENTRES_SUIVIS.items()}.get(c.lower(), "")
                        notify(f"{c} : {d} {t} libre",
                               f"{c} ({arr_of.get(c, '?')}) - {d} à {t}" + (f", court {court}" if court else "")
                               + " - touche la notification pour réserver",
                               click=f"{BASE}?page=centre-sportif&id={cid}" if cid else None)
                state["alertes_libres"] = [h(k) for k in current]

        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"dates": dates, "sentinel_days": s_list, "dernier_jour_ouvert": s_list[-1] if s_list else "",
                       "arr": arr_of, "centres": new_state_centres, "horaires_ts": last_h,
                       "remplissage": state["remplissage"], "max_jour": state.get("max_jour"),
                       "ouv_centres": state["ouv_centres"],
                       "alertes_libres": state.get("alertes_libres", []), "last_run": now},
                      f, ensure_ascii=False, indent=1)
        print(f"{now} : recherche jusqu'au {dates[-1]} ; réservable jusqu'au {s_list[-1] if s_list else '?'}")
    except Exception as e:
        append(REQUETES, ["timestamp", "statut", "nb_jours", "dernier_jour", "detail"],
               [now, "erreur", "", "", str(e)[:200]])
        print(f"{now} : erreur : {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    passes = int(os.environ.get("PASSES", "1"))
    pause = int(os.environ.get("PAUSE_S", "60"))
    for i in range(passes):
        try:
            main()
        except Exception:
            if passes == 1:
                sys.exit(1)
        if i < passes - 1:
            time.sleep(pause)
