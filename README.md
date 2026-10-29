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

## Aktualisierung

Der Workflow `.github/workflows/build.yml` läuft jeden Montag um 03:17 UTC, bei Änderungen an `tools/` oder `.github/workflows/` und manuell. Ablauf: Download von Geofabrik, Vorfilterung mit `osmium tags-filter`, `tools/build.py`, Veröffentlichung auf GitHub Pages. Zusätzlich wird `STAND.md` aktualisiert, damit GitHub den Zeitplan nicht nach 60 Tagen Inaktivität deaktiviert.

Manuell starten: Reiter **Actions**, Workflow auswählen, **Run workflow**.

Voraussetzung: In den Repository-Einstellungen unter **Pages** als Quelle **GitHub Actions** wählen.

Lokal (mit Python 3.12, `pip install osmium shapely`):

```
python tools/build.py --pbf filtered.osm.pbf --poly switzerland.poly --out site
```

## Lizenz und Quellenangabe

Daten © OpenStreetMap-Mitwirkende, verfügbar unter der Open Database License (ODbL). Siehe <https://www.openstreetmap.org/copyright>. Abgeleitete Datenbanken müssen unter derselben Lizenz weitergegeben und mit dieser Quelle angegeben werden. Die Rohdaten stammen von Geofabrik.
