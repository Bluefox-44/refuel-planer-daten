#!/usr/bin/env python3
"""Baut die Kachel-Daten fuer den Refuel-Planer aus einer vorgefilterten OSM-PBF.

Aufruf:
    python tools/build.py --pbf filtered.osm.pbf --poly switzerland.poly --out site

Abhaengigkeiten: osmium (pyosmium), shapely.
"""
import argparse
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sys
from collections import Counter

import osmium
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

TILE_LAT = 0.05
TILE_LON = 0.07

SHOP_RE = re.compile(r"^(bakery|pastry|supermarket|convenience|kiosk|deli|farm)$")
AMENITY_RE = re.compile(r"^(cafe|restaurant|fast_food|ice_cream|fuel|drinking_water)$")
VENDING_RE = re.compile(r"drinks|food|sweets|water")  # Overpass "~" = Teilstring-Treffer
RAILWAY_RE = re.compile(r"^(station|halt)$")
TOURISM_RE = re.compile(r"^(alpine_hut|wilderness_hut)$")

DROP_PREFIXES = ("name:", "alt_name", "old_name", "official_name", "wikipedia:",
                 "source", "note", "check_date")
DROP_EXACT = {"wikidata", "wikipedia", "fixme", "FIXME"}
DROP_SUFFIXES = (":wikidata", ":wikipedia")


def classify(tags):
    """Gibt die Kategorie zurueck, wenn der Overpass-Filter greift, sonst None."""
    shop = tags.get("shop")
    if shop is not None and SHOP_RE.match(shop):
        return "shop"
    amenity = tags.get("amenity")
    if amenity is not None:
        if AMENITY_RE.match(amenity):
            return "amenity"
        if amenity == "vending_machine":
            v = tags.get("vending")
            if v is not None and VENDING_RE.search(v):
                return "vending_machine"
        if amenity == "fountain" and tags.get("drinking_water") == "yes":
            return "fountain"
        if amenity == "shelter":
            return "shelter"
    railway = tags.get("railway")
    if railway is not None and RAILWAY_RE.match(railway) and "name" in tags:
        return "railway"
    if tags.get("highway") == "bus_stop" and tags.get("shelter") == "yes":
        return "bus_stop"
    tourism = tags.get("tourism")
    if tourism is not None and TOURISM_RE.match(tourism):
        return "tourism"
    return None


def clean_tags(tags):
    out = {}
    for k, v in tags.items():
        if k == "name":
            out[k] = v
            continue
        if k in DROP_EXACT or k.startswith(DROP_PREFIXES) or k.endswith(DROP_SUFFIXES):
            continue
        out[k] = v
    return out


def tile_of(lat, lon):
    return math.floor(lat / TILE_LAT), math.floor(lon / TILE_LON)


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.elements = []  # (category, element-dict)
        self.skipped_no_location = 0

    def _add(self, cat, typ, oid, lat, lon, tags):
        self.elements.append((cat, {
            "type": typ,
            "id": int(oid),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "tags": clean_tags(tags),
        }))

    def node(self, n):
        if not n.tags:
            return
        tags = {t.k: t.v for t in n.tags}
        cat = classify(tags)
        if cat is None:
            return
        loc = n.location
        if not loc.valid():
            self.skipped_no_location += 1
            return
        self._add(cat, "node", n.id, loc.lat, loc.lon, tags)

    def way(self, w):
        if not w.tags:
            return
        tags = {t.k: t.v for t in w.tags}
        cat = classify(tags)
        if cat is None:
            return
        min_lat = min_lon = float("inf")
        max_lat = max_lon = float("-inf")
        ok = False
        try:
            for nr in w.nodes:
                loc = nr.location
                if not loc.valid():
                    continue
                lat, lon = loc.lat, loc.lon
                min_lat = min(min_lat, lat)
                max_lat = max(max_lat, lat)
                min_lon = min(min_lon, lon)
                max_lon = max(max_lon, lon)
                ok = True
        except Exception:  # z.B. InvalidLocationError
            ok = False
        if not ok:
            self.skipped_no_location += 1
            return
        self._add(cat, "way", w.id, (min_lat + max_lat) / 2, (min_lon + max_lon) / 2, tags)

    def area(self, a):
        # Areas aus geschlossenen Ways werden schon in way() behandelt.
        if a.from_way():
            return
        if not a.tags:
            return
        tags = {t.k: t.v for t in a.tags}
        cat = classify(tags)
        if cat is None:
            return
        min_lat = min_lon = float("inf")
        max_lat = max_lon = float("-inf")
        ok = False
        try:
            for ring in a.outer_rings():
                for nr in ring:
                    loc = nr.location
                    if not loc.valid():
                        continue
                    lat, lon = loc.lat, loc.lon
                    min_lat = min(min_lat, lat)
                    max_lat = max(max_lat, lat)
                    min_lon = min(min_lon, lon)
                    max_lon = max(max_lon, lon)
                    ok = True
        except Exception:
            ok = False
        if not ok:
            self.skipped_no_location += 1
            return
        self._add(cat, "relation", a.orig_id(), (min_lat + max_lat) / 2,
                  (min_lon + max_lon) / 2, tags)


def parse_poly(path):
    """Liest das Geofabrik/Osmosis-.poly-Format. Ringe mit '!' sind Loecher."""
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    outers, holes = [], []
    i = 1  # Zeile 0 ist der Name der Datei
    while i < len(lines):
        head = lines[i]
        if head == "END":
            break
        is_hole = head.startswith("!")
        i += 1
        coords = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            coords.append((float(parts[0]), float(parts[1])))
            i += 1
        i += 1  # END des Rings
        if len(coords) < 3:
            continue
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        (holes if is_hole else outers).append(poly)
    if not outers:
        raise ValueError("Keine Aussenringe in " + path)
    geom = unary_union(outers)
    if holes:
        geom = geom.difference(unary_union(holes))
    return geom


def pbf_timestamp(path):
    try:
        reader = osmium.io.Reader(path)
        try:
            ts = reader.header().get("osmosis_replication_timestamp")
        finally:
            reader.close()
        return ts or None
    except Exception as e:  # Header-API je nach pyosmium-Version verschieden
        print("Hinweis: PBF-Zeitstempel nicht lesbar:", e, file=sys.stderr)
        return None


def write_index(out_dir, generated, osm_ts, n_elem, n_complete, n_partial):
    stand = generated[:10]
    osm_line = ""
    if osm_ts:
        osm_line = "<p>OSM-Datenstand: %s</p>\n" % html.escape(osm_ts)
    page = """<!doctype html>
<html lang="de-CH">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Refuel-Planer Daten</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5}</style>
</head>
<body>
<h1>Refuel-Planer Daten</h1>
<p>Wöchentlich aktualisierte Kachel-Daten (Verpflegung, Wasser, Unterstände) aus OpenStreetMap für den Refuel-Planer.</p>
<p>Stand: %(stand)s</p>
%(osm)s<ul>
<li>Elemente: %(n)d</li>
<li>Vollständige Kacheln: %(c)d</li>
<li>Teilweise abgedeckte Kacheln: %(p)d</li>
</ul>
<p><a href="meta.json">meta.json</a></p>
<p>Daten © OpenStreetMap-Mitwirkende, ODbL. <a href="https://www.openstreetmap.org/copyright">openstreetmap.org/copyright</a></p>
</body>
</html>
""" % {"stand": html.escape(stand), "osm": osm_line, "n": n_elem, "c": n_complete, "p": n_partial}
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--poly", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    region = parse_poly(args.poly)
    prepared = prep(region)

    handler = Collector()
    handler.apply_file(args.pbf, locations=True, idx="flex_mem")

    # Intersecting tiles bestimmen
    minx, miny, maxx, maxy = region.bounds
    ty0, ty1 = math.floor(miny / TILE_LAT), math.floor(maxy / TILE_LAT)
    tx0, tx1 = math.floor(minx / TILE_LON), math.floor(maxx / TILE_LON)
    status = {}  # id -> "complete" | "partial"
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            tb = box(tx * TILE_LON, ty * TILE_LAT, (tx + 1) * TILE_LON, (ty + 1) * TILE_LAT)
            if not prepared.intersects(tb):
                continue
            status["%d_%d" % (ty, tx)] = "complete" if prepared.covers(tb) else "partial"

    # Elemente den Kacheln zuordnen
    tiles = {}
    cats = Counter()
    for cat, el in handler.elements:
        ty, tx = tile_of(el["lat"], el["lon"])
        tid = "%d_%d" % (ty, tx)
        if tid not in status:
            continue
        tiles.setdefault(tid, []).append(el)
        cats[cat] += 1

    tiles_dir = os.path.join(args.out, "tiles")
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    os.makedirs(tiles_dir, exist_ok=True)

    total_bytes = 0
    n_elem = 0
    complete, partial = {}, {}
    for tid in sorted(status):
        els = tiles.get(tid, [])
        els.sort(key=lambda e: (e["type"], e["id"]))
        (complete if status[tid] == "complete" else partial)[tid] = len(els)
        if not els:
            continue
        data = json.dumps(els, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(os.path.join(tiles_dir, tid + ".json"), "wb") as f:
            f.write(data)
        total_bytes += len(data)
        n_elem += len(els)

    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    osm_ts = pbf_timestamp(args.pbf)
    meta = {
        "version": 1,
        "generated": generated,
        "osmTimestamp": osm_ts,
        "tileLat": TILE_LAT,
        "tileLon": TILE_LON,
        "complete": complete,
        "partial": partial,
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, separators=(",", ":"), ensure_ascii=False)

    write_index(args.out, generated, osm_ts, n_elem, len(complete), len(partial))
    with open(os.path.join(args.out, ".nojekyll"), "w") as f:
        f.write("")

    print("Elemente gesamt: %d" % n_elem)
    for c, n in sorted(cats.items()):
        print("  %-16s %d" % (c, n))
    print("Ohne gueltige Position uebersprungen: %d" % handler.skipped_no_location)
    print("Kacheln vollstaendig: %d, teilweise: %d, mit Datei: %d" % (
        len(complete), len(partial), sum(1 for v in list(complete.values()) + list(partial.values()) if v)))
    print("Tile-Bytes gesamt: %d" % total_bytes)
    print("Stand %s | %d Elemente | %d vollstaendige, %d teilweise Kacheln | %.1f MB" % (
        generated[:10], n_elem, len(complete), len(partial), total_bytes / 1e6))


if __name__ == "__main__":
    main()
