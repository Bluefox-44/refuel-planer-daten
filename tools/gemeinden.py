#!/usr/bin/env python3
"""Baut den Gemeinde-Datensatz (gemeinden.geojson) fuer das Spiel "Gemeinden sammeln".

Aufruf:
    python tools/gemeinden.py --pbf gemeinden.osm.pbf --out site --cache terrain-cache

Eingabe: vorgefilterte PBF (siehe Workflow): Relationen mit admin_level 4/8 samt
Mitgliedern, Strassen (highway), place=city|town und mountain_pass=yes.

Abhaengigkeiten: osmium (pyosmium), shapely >= 2.0 (mit numpy), pillow.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import osmium
import shapely
from PIL import Image
from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

# --- Konstanten -------------------------------------------------------------

ROAD_TYPES = {"residential", "unclassified", "tertiary", "secondary", "primary",
              "trunk", "service", "track"}
PASS_MIN_ELE = 1500.0

# Lokale metrische Projektion (equirectangular um 46.8 N)
LAT0 = 46.8
KY = 111_132.0                                   # Meter pro Grad Breite
KX = 111_320.0 * math.cos(math.radians(LAT0))    # Meter pro Grad Laenge
SCALE = np.array([KX, KY])

# Hoehenraster
GRID_STEP_M = 300.0
GRID_MIN_PTS = 5
GRID_MAX_PTS = 400
TERRAIN_Z = 11
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

# Gewichte des Wert-Scores (Summe = 1)
WEIGHTS = {"max": 0.40, "median": 0.20, "roads": 0.15, "remote": 0.15, "pass": 0.10}
# Anteile der Stufen von unten (Stufe 1) nach oben (Stufe 4)
TIER_SHARES = [("Tal", 0.45, 10), ("Hügel", 0.30, 20), ("Berg", 0.17, 40),
               ("Pass / abgelegen", 0.08, 80)]

SIMPLIFY_STEPS = (0.0004, 0.0006, 0.0008, 0.0012)  # Grad
MAX_GEOJSON_BYTES = 6_000_000


def proj(geom):
    """lon/lat -> Meter (shapely-Geometrie)."""
    return shapely.transform(geom, lambda c: c * SCALE)


def parse_ele(s):
    if s is None:
        return None
    m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


# --- Pass 1: Verwaltungsgrenzen ---------------------------------------------

class AdminHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkbfab = osmium.geom.WKBFactory()
        self.cantons = []   # (code, geom)
        self.gemeinden = []  # dict(rel, name, bfs, geom)
        self.errors = []

    def _geom(self, a):
        try:
            g = shapely.from_wkb(self.wkbfab.create_multipolygon(a))
        except Exception:
            # Fallback: aus den Ringen selbst bauen
            polys = []
            for outer in a.outer_rings():
                shell = [(n.lon, n.lat) for n in outer]
                holes = [[(n.lon, n.lat) for n in inner] for inner in a.inner_rings(outer)]
                polys.append(Polygon(shell, holes))
            g = unary_union([p if p.is_valid else p.buffer(0) for p in polys])
        if not g.is_valid:
            g = g.buffer(0)
        return g

    def area(self, a):
        if a.from_way():
            return
        tags = {t.k: t.v for t in a.tags}
        if tags.get("boundary") != "administrative":
            return
        lvl = tags.get("admin_level")
        if lvl not in ("4", "8"):
            return
        rel = a.orig_id()
        if lvl == "4":
            iso = tags.get("ISO3166-2", "")
            if not iso.startswith("CH-"):
                return
            try:
                self.cantons.append((iso[3:5], self._geom(a)))
            except Exception as e:
                self.errors.append("Kanton %s: %s" % (iso, e))
            return
        try:
            g = self._geom(a)
        except Exception as e:
            self.errors.append("Relation %d: %s" % (rel, e))
            return
        if g.is_empty:
            self.errors.append("Relation %d (%s): leere Geometrie" % (rel, tags.get("name")))
            return
        bfs = tags.get("swisstopo:BFS_NUMMER")
        bfs = int(bfs) if bfs and bfs.strip().isdigit() else None
        self.gemeinden.append({"rel": rel, "name": tags.get("name") or "?", "bfs": bfs, "geom": g})


# --- Pass 2: Strassen, Orte, Paesse -----------------------------------------

class PoiHandler(osmium.SimpleHandler):
    def __init__(self, tree, polys_m, prepped):
        super().__init__()
        self.tree = tree
        self.polys_m = polys_m
        self.prepped = prepped
        self.road_m = np.zeros(len(polys_m))
        self.places = []  # (x, y) in Metern
        self.passes = []  # (x, y) in Metern
        self.n_ways = 0

    def node(self, n):
        if not n.tags:
            return
        place = n.tags.get("place")
        mp = n.tags.get("mountain_pass")
        if place not in ("city", "town") and mp != "yes":
            return
        if not n.location.valid():
            return
        x, y = n.location.lon * KX, n.location.lat * KY
        if place in ("city", "town"):
            self.places.append((x, y))
        if mp == "yes":
            ele = parse_ele(n.tags.get("ele"))
            if ele is not None and ele >= PASS_MIN_ELE:
                self.passes.append((x, y))

    def way(self, w):
        if w.tags.get("highway") not in ROAD_TYPES:
            return
        try:
            pts = [(n.lon * KX, n.lat * KY) for n in w.nodes]
        except Exception:  # fehlende Positionen
            return
        if len(pts) < 2:
            return
        line = shapely.LineString(pts)
        self.n_ways += 1
        for i in self.tree.query(line, predicate="intersects"):
            if self.prepped[i].contains(line):
                self.road_m[i] += line.length
            else:
                self.road_m[i] += line.intersection(self.polys_m[i]).length


# --- Hoehen -----------------------------------------------------------------

def tile_xy(lon, lat, z):
    """Fliesskomma-Kachelkoordinaten (Web-Mercator, Arrays)."""
    n = 2 ** z
    xf = (lon + 180.0) / 360.0 * n
    lat_r = np.radians(lat)
    yf = (1.0 - np.arcsinh(np.tan(lat_r)) / math.pi) / 2.0 * n
    return xf, yf


def fetch_tile(z, x, y, cache_dir):
    path = os.path.join(cache_dir, "%d_%d_%d.png" % (z, x, y))
    if not os.path.exists(path):
        url = TERRAIN_URL.format(z=z, x=x, y=y)
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
        if data is None:
            print("Warnung: Kachel nicht ladbar:", url, file=sys.stderr)
            return None
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    try:
        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception as e:
        print("Warnung: Kachel defekt (%s): %s" % (path, e), file=sys.stderr)
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def load_tiles(needed, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    tiles = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_tile, TERRAIN_Z, x, y, cache_dir): (x, y) for (x, y) in needed}
        for fut in concurrent.futures.as_completed(futs):
            tiles[futs[fut]] = fut.result()
    return tiles


def sample_elevation(lon, lat, tiles):
    xf, yf = tile_xy(lon, lat, TERRAIN_Z)
    tx, ty = np.floor(xf).astype(int), np.floor(yf).astype(int)
    px = np.clip(((xf - tx) * 256).astype(int), 0, 255)
    py = np.clip(((yf - ty) * 256).astype(int), 0, 255)
    out = np.full(lon.shape, np.nan)
    for key in set(zip(tx.tolist(), ty.tolist())):
        arr = tiles.get(key)
        if arr is None:
            continue
        m = (tx == key[0]) & (ty == key[1])
        out[m] = arr[py[m], px[m]]
    return out


def grid_points(poly_m, rep_m, area_m2):
    """Rasterpunkte (Meter) innerhalb des Polygons: ~300 m, min 5, max ~400."""
    step = GRID_STEP_M
    n_est = area_m2 / step ** 2
    if n_est > GRID_MAX_PTS:
        step = math.sqrt(area_m2 / GRID_MAX_PTS)
    elif n_est < GRID_MIN_PTS:
        step = math.sqrt(area_m2 / GRID_MIN_PTS)
    minx, miny, maxx, maxy = poly_m.bounds
    xs = ys = None
    for _ in range(10):
        gx = np.arange(minx + step / 2, maxx, step)
        gy = np.arange(miny + step / 2, maxy, step)
        if len(gx) and len(gy):
            xx, yy = np.meshgrid(gx, gy)
            xx, yy = xx.ravel(), yy.ravel()
            mask = shapely.contains_xy(poly_m, xx, yy)
            xs, ys = xx[mask], yy[mask]
            if len(xs) >= GRID_MIN_PTS:
                break
        step *= 0.7
    if xs is None:
        xs = ys = np.empty(0)
    xs = np.append(xs, rep_m.x)
    ys = np.append(ys, rep_m.y)
    return xs, ys


# --- Score ------------------------------------------------------------------

def robust_norm(values):
    """Perzentil-Normalisierung auf 0..1 (p5 -> 0, p95 -> 1, dazwischen linear)."""
    v = np.asarray(values, dtype=float)
    lo, hi = np.percentile(v, 5), np.percentile(v, 95)
    if hi - lo <= 1e-9:
        return np.zeros_like(v)
    return np.clip((v - lo) / (hi - lo), 0.0, 1.0)


def round_coords(c):
    if isinstance(c[0], (int, float)):
        return [round(c[0], 5), round(c[1], 5)]
    return [round_coords(x) for x in c]


def to_geojson_geom(geom, tol):
    g = geom.simplify(tol, preserve_topology=True)
    if g.is_empty:
        g = geom
    if isinstance(g, Polygon):
        g = orient(g)
    elif isinstance(g, MultiPolygon):
        g = MultiPolygon([orient(p) for p in g.geoms])
    m = mapping(g)
    return {"type": m["type"], "coordinates": round_coords(m["coordinates"])}


# --- Hauptprogramm ----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="terrain-cache")
    args = ap.parse_args()
    t0 = time.time()

    # 1. Grenzen
    print("Pass 1: Verwaltungsgrenzen ...")
    ah = AdminHandler()
    ah.apply_file(args.pbf, locations=True, idx="flex_mem")
    print("  Kantone: %d, Level-8-Flaechen: %d, Fehler: %d (%.0f s)" % (
        len(ah.cantons), len(ah.gemeinden), len(ah.errors), time.time() - t0))
    if not ah.cantons:
        sys.exit("Keine Kantone gefunden - Filter/PBF pruefen.")
    skipped = [("Geometriefehler", e) for e in ah.errors]

    cantons = [(code, g, prep(g)) for code, g in ah.cantons]

    kept = []
    seen = set()
    for g in ah.gemeinden:
        rep = g["geom"].representative_point()
        canton = None
        for code, cg, pg in cantons:
            if pg.contains(rep):
                canton = code
                break
        if canton is None:
            skipped.append(("ausserhalb Kanton", "%s (osm:%d)" % (g["name"], g["rel"])))
            continue
        gid = str(g["bfs"]) if g["bfs"] is not None else "osm:%d" % g["rel"]
        if gid in seen:
            skipped.append(("doppelte ID", "%s (%s)" % (g["name"], gid)))
            continue
        seen.add(gid)
        g.update({"id": gid, "canton": canton, "rep": rep})
        kept.append(g)
    kept.sort(key=lambda g: (g["canton"], g["name"], g["id"]))
    n = len(kept)
    print("  Gemeinden in Schweizer Kantonen: %d" % n)
    if n == 0:
        sys.exit("Keine Gemeinden gefunden.")

    # 2. Projektion, Index
    polys_m = [proj(g["geom"]) for g in kept]
    prepped = [prep(p) for p in polys_m]
    tree = STRtree(polys_m)

    # 3. Strassen, Orte, Paesse
    print("Pass 2: Strassen, Orte, Paesse ...")
    ph = PoiHandler(tree, polys_m, prepped)
    ph.apply_file(args.pbf, locations=True, idx="flex_mem")
    print("  Strassen-Ways: %d, Orte: %d, Paesse >= %d m: %d (%.0f s)" % (
        ph.n_ways, len(ph.places), PASS_MIN_ELE, len(ph.passes), time.time() - t0))

    has_pass = np.zeros(n, dtype=bool)
    for x, y in ph.passes:
        for i in tree.query(Point(x, y), predicate="intersects"):
            has_pass[i] = True
    places = np.array(ph.places) if ph.places else np.empty((0, 2))

    # 4. Punktraster und Hoehen
    print("Hoehen: Raster und Terrain-Kacheln ...")
    pts = []  # pro Gemeinde (lon[], lat[])
    needed = set()
    for i, g in enumerate(kept):
        area_m2 = polys_m[i].area
        rep_m = Point(g["rep"].x * KX, g["rep"].y * KY)
        xs, ys = grid_points(polys_m[i], rep_m, area_m2)
        lon, lat = xs / KX, ys / KY
        pts.append((lon, lat))
        xf, yf = tile_xy(lon, lat, TERRAIN_Z)
        needed.update(zip(np.floor(xf).astype(int).tolist(), np.floor(yf).astype(int).tolist()))
    print("  %d Kacheln noetig (Cache: %s)" % (len(needed), args.cache))
    tiles = load_tiles(needed, args.cache)
    missing = sum(1 for v in tiles.values() if v is None)
    if missing:
        print("  Warnung: %d Kacheln fehlen" % missing)

    # 5. Faktoren
    ele_med = np.zeros(n)
    ele_max = np.zeros(n)
    no_ele = 0
    for i in range(n):
        e = sample_elevation(*pts[i], tiles)
        e = e[~np.isnan(e)]
        if len(e) == 0:
            no_ele += 1
            continue
        ele_med[i] = np.median(e)
        ele_max[i] = np.max(e)
    if no_ele:
        print("  Warnung: %d Gemeinden ohne Hoehenwerte (0 m angenommen)" % no_ele)

    area_km2 = np.zeros(n)
    for i, g in enumerate(kept):
        # Flaeche der Projektion auf die lokale Breite korrigieren
        k = math.cos(math.radians(g["rep"].y)) / math.cos(math.radians(LAT0))
        area_km2[i] = polys_m[i].area * k / 1e6
    road_density = np.where(area_km2 > 0, ph.road_m / 1000.0 / np.maximum(area_km2, 1e-9), 0.0)

    remote_km = np.zeros(n)
    if len(places):
        for i, g in enumerate(kept):
            d = np.hypot(places[:, 0] - g["rep"].x * KX, places[:, 1] - g["rep"].y * KY)
            remote_km[i] = d.min() / 1000.0

    # 6. Nachbarn
    print("Nachbarn ...")
    buffered = [p.simplify(5, preserve_topology=True).buffer(10) for p in polys_m]
    btree = STRtree(buffered)
    neighbours = []
    for i in range(n):
        hits = btree.query(buffered[i], predicate="intersects")
        neighbours.append(sorted(kept[j]["id"] for j in hits if j != i))

    # 7. Score und Stufen
    s_max = robust_norm(ele_max)
    s_med = robust_norm(ele_med)
    s_roads = 1.0 - robust_norm(road_density)
    s_remote = robust_norm(remote_km)
    s_pass = has_pass.astype(float)
    score = (WEIGHTS["max"] * s_max + WEIGHTS["median"] * s_med + WEIGHTS["roads"] * s_roads
             + WEIGHTS["remote"] * s_remote + WEIGHTS["pass"] * s_pass)

    order = sorted(range(n), key=lambda i: (score[i], kept[i]["id"]))  # aufsteigend
    tier = [1] * n
    pos = 0.0
    for t, (_, share, _) in enumerate(TIER_SHARES, start=1):
        lo, hi = int(round(pos * n)), int(round((pos + share) * n))
        for r in range(lo, hi):
            tier[order[r]] = t
        pos += share
    for r in range(int(round(pos * n)), n):  # Rundungsrest
        tier[order[r]] = len(TIER_SHARES)
    points_of = {t: TIER_SHARES[t - 1][2] for t in range(1, len(TIER_SHARES) + 1)}

    # 8. GeoJSON (Genauigkeit bei Bedarf reduzieren, bis die Datei klein genug ist)
    props = []
    for i, g in enumerate(kept):
        props.append({
            "id": g["id"],
            "name": g["name"],
            "bfs": g["bfs"],
            "canton": g["canton"],
            "tier": tier[i],
            "points": points_of[tier[i]],
            "area_km2": round(float(area_km2[i]), 2),
            "ele_median": int(round(ele_med[i])),
            "ele_max": int(round(ele_max[i])),
            "road_density": round(float(road_density[i]), 2),
            "remote_km": round(float(remote_km[i]), 2),
            "has_pass": bool(has_pass[i]),
            "neighbours": neighbours[i],
            "centroid": [round(g["rep"].x, 5), round(g["rep"].y, 5)],
            "sub": {
                "max": round(float(s_max[i]), 3),
                "median": round(float(s_med[i]), 3),
                "roads": round(float(s_roads[i]), 3),
                "remote": round(float(s_remote[i]), 3),
                "pass": round(float(s_pass[i]), 3),
                "score": round(float(score[i]), 3),
            },
        })
    data = b""
    for tol in SIMPLIFY_STEPS:
        features = [{"type": "Feature", "properties": props[i],
                     "geometry": to_geojson_geom(g["geom"], tol)} for i, g in enumerate(kept)]
        fc = {"type": "FeatureCollection", "features": features}
        data = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        print("  Vereinfachung %.4f Grad: %.2f MB" % (tol, len(data) / 1e6))
        if len(data) <= MAX_GEOJSON_BYTES:
            break
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "gemeinden.geojson"), "wb") as f:
        f.write(data)

    # 9. meta.json ergaenzen (bestehende Schluessel bleiben unveraendert)
    meta_path = os.path.join(args.out, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    else:
        print("Warnung: meta.json fehlt - zuerst tools/build.py ausfuehren.", file=sys.stderr)
    meta["gemeinden"] = {"count": n, "file": "gemeinden.geojson", "version": 1}
    with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, separators=(",", ":"), ensure_ascii=False)

    # 10. Zusammenfassung
    print("")
    print("Gemeinden: %d (erwartet ca. 2100-2140)" % n)
    print("gemeinden.geojson: %.2f MB" % (len(data) / 1e6))
    for t, (label, _, pts_) in enumerate(TIER_SHARES, start=1):
        c = sum(1 for x in tier if x == t)
        print("  Stufe %d %-17s %4d Gemeinden (%.1f %%), %d Punkte" % (
            t, label, c, 100.0 * c / n, pts_))
    print("Top 10 nach Wert:")
    for i in sorted(range(n), key=lambda i: -score[i])[:10]:
        print("  %-28s %s  Stufe %d  Score %.3f  max %d m  Strassen %.1f km/km2  Ort %.1f km%s" % (
            kept[i]["name"], kept[i]["canton"], tier[i], score[i], ele_max[i],
            road_density[i], remote_km[i], "  Pass" if has_pass[i] else ""))
    print("Uebersprungen: %d" % len(skipped))
    for reason, what in skipped[:40]:
        print("  [%s] %s" % (reason, what))
    if len(skipped) > 40:
        print("  ... %d weitere" % (len(skipped) - 40))
    print("Laufzeit: %.0f s" % (time.time() - t0))


if __name__ == "__main__":
    main()
