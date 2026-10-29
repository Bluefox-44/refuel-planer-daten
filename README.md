# Refuel-Planer Daten

Dieses Repository erzeugt die Kartendaten für den Refuel-Planer. Aus dem OpenStreetMap-Auszug der Schweiz (Geofabrik) werden Verpflegungsstellen, Trinkwasser, Unterstände, Hütten und Bahnhöfe herausgefiltert und in kleine Kacheln (JSON) aufgeteilt. Die Dateien werden über GitHub Pages ausgeliefert, damit die App nicht mehr pro Kachel Overpass abfragen muss.

## Datenformat

### meta.json

```json
{
  "version": 1,
  "generated": "2026-11-02T03:25:00Z",
  "osmTimestamp": "2026-11-01T21:00:00Z",
  "tileLat": 0.05,
  "tileLon": 0.07,
  "complete": { "938_121": 42, "939_121": 0 },
  "partial": { "940_85": 7 }
}
```

- `complete`: Kacheln, die ganz im Abdeckungsgebiet (Schweiz) liegen. Ein leeres Ergebnis ist hier verlässlich.
- `partial`: Kacheln, die nur teilweise im Gebiet liegen. Dort können Daten fehlen; die App sollte ergänzend Overpass abfragen.
- Wert = Anzahl Elemente. Bei 0 gibt es keine Datei.
- Kacheln ausserhalb des Gebiets fehlen in beiden Listen.
- `osmTimestamp` ist der Datenstand der OSM-Daten (oder `null`).

### Kacheln

Kachel-ID: `${Math.floor(lat/0.05)}_${Math.floor(lon/0.07)}` (also `y_x`, kann negativ sein).
Datei: `tiles/{id}.json`, ein JSON-Array mit Elementen im Stil von Overpass (`out center tags`):

```json
[{"type":"node","id":123,"lat":46.8,"lon":8.4,"tags":{"amenity":"cafe","name":"Beispiel"}}]
```

`type` ist `node`, `way` oder `relation`. Bei Ways und Relationen ist `lat`/`lon` der Mittelpunkt der Bounding Box. Übersetzte Namen (`name:*`), `alt_name*`, `old_name*`, `official_name*`, `wikidata`, `wikipedia`, `source*`, `note*`, `fixme` und `check_date*` werden weggelassen.

Enthalten sind: `shop` (bakery, pastry, supermarket, convenience, kiosk, deli, farm), `amenity` (cafe, restaurant, fast_food, ice_cream, fuel, drinking_water, shelter, Verkaufsautomaten für Getränke/Essen/Süsses/Wasser, Trinkbrunnen), Bahnhöfe und Haltestellen mit Namen, Bushaltestellen mit Dach, Alp- und Wildnishütten.

## Gemeinden (gemeinden.geojson)

Für das Spiel «Gemeinden sammeln». `tools/gemeinden.py` baut aus den Grenzen (`boundary=administrative`, `admin_level=8`) alle Schweizer Gemeinden. Nur Flächen, deren Repräsentationspunkt in einem Kanton (`admin_level=4`, `ISO3166-2=CH-XX`) liegt, werden übernommen (Liechtenstein, Enklaven und Randgebiete des Auszugs fallen weg). In `meta.json` steht zusätzlich:

```json
"gemeinden": { "count": 2131, "file": "gemeinden.geojson", "version": 1 }
```

Die übrigen Schlüssel von `meta.json` bleiben unverändert.

### Felder (`properties` je Feature, Polygon oder MultiPolygon)

| Feld | Bedeutung |
|---|---|
| `id` | BFS-Nummer als String (`swisstopo:BFS_NUMMER`), sonst `osm:<Relations-ID>` |
| `name`, `bfs` (Zahl oder `null`), `canton` (2 Buchstaben) | Stammdaten |
| `tier`, `points` | Wertstufe 1 bis 4 und Punkte (10/20/40/80) |
| `area_km2` | Fläche (Seeanteile sind je nach OSM-Grenze enthalten) |
| `ele_median`, `ele_max` | Höhe in m aus einem Raster (~300 m, 5 bis ~400 Punkte) |
| `road_density` | Strassen in km pro km² |
| `remote_km` | Luftlinie vom Repräsentationspunkt zur nächsten Stadt/Ortschaft (`place=town\|city`) |
| `has_pass` | `true`, wenn ein `mountain_pass=yes` mit `ele` ≥ 1500 m in der Gemeinde liegt |
| `neighbours` | IDs der Nachbargemeinden (Grenzen im Abstand von ~20 m) |
| `centroid` | `[lon, lat]`, `representative_point()` (liegt sicher in der Fläche) |
| `sub` | Teilwerte 0..1: `max`, `median`, `roads`, `remote`, `pass`, sowie `score` |

Die Geometrie ist mit Toleranz 0.0004° vereinfacht (`preserve_topology`, 5 Nachkommastellen); bei mehr als 6 MB wird automatisch stärker vereinfacht. Jede Gemeinde wird einzeln vereinfacht, an Grenzen können daher feine Lücken oder Überlappungen entstehen.

### Wert und Stufen

Jeder Faktor wird robust normalisiert: 5. Perzentil → 0, 95. Perzentil → 1, dazwischen linear, ausserhalb abgeschnitten.

```
s_max    = norm(ele_max)
s_median = norm(ele_median)
s_roads  = 1 - norm(road_density)     (wenig Strassen = hoch)
s_remote = norm(remote_km)
s_pass   = 1 wenn has_pass, sonst 0

score = 0.40*s_max + 0.20*s_median + 0.15*s_roads + 0.15*s_remote + 0.10*s_pass
```

Nach `score` sortiert, werden die Gemeinden nach Anteil in Stufen eingeteilt:

| Stufe | Name | Anteil | Punkte |
|---|---|---|---|
| 1 | Tal | 45 % | 10 |
| 2 | Hügel | 30 % | 20 |
| 3 | Berg | 17 % | 40 |
| 4 | Pass / abgelegen | 8 % | 80 |

Die Höhen stammen vom 300-m-Raster, ein einzelner Gipfel kann daher knapp verfehlt werden.

## Aktualisierung

Der Workflow `.github/workflows/build.yml` läuft jeden Montag um 03:17 UTC, bei Änderungen an `tools/` oder `.github/workflows/` und manuell. Ablauf: Download von Geofabrik, Vorfilterung mit `osmium tags-filter`, `tools/build.py`, zweite Vorfilterung und `tools/gemeinden.py`, Veröffentlichung auf GitHub Pages. Zusätzlich wird `STAND.md` aktualisiert, damit GitHub den Zeitplan nicht nach 60 Tagen Inaktivität deaktiviert.

Manuell starten: Reiter **Actions**, Workflow auswählen, **Run workflow**.

Voraussetzung: In den Repository-Einstellungen unter **Pages** als Quelle **GitHub Actions** wählen.

Lokal (mit Python 3.12, `pip install osmium "shapely>=2.0" pillow numpy`):

```
python tools/build.py --pbf filtered.osm.pbf --poly switzerland.poly --out site
python tools/gemeinden.py --pbf gemeinden.osm.pbf --out site --cache terrain-cache
```

## Lizenz und Quellenangabe

Daten © OpenStreetMap-Mitwirkende, verfügbar unter der Open Database License (ODbL). Siehe <https://www.openstreetmap.org/copyright>. Abgeleitete Datenbanken müssen unter derselben Lizenz weitergegeben und mit dieser Quelle angegeben werden. Die Rohdaten stammen von Geofabrik.

Höhen der Gemeinden: Terrain Tiles, Mapzen / AWS Open Data (Quellen SRTM u.a.), <https://registry.opendata.aws/terrain-tiles/>.
