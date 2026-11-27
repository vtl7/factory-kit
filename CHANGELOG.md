# Versionshistorie

Alle nennenswerten Änderungen am Fabrik-Kit. Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die Nummern folgen [Semantic Versioning](https://semver.org/lang/de/).

Welche Version ein Projekt nutzt, steht nach `fabrik init` in `.claude/fabrik/VERSION` und im Kopf von `.fabrik/fabrik.yml`. `fabrik version` zeigt beide Stände.

Solange die Hauptnummer 0 ist, kann jede Nebenversion (0.x) Inkompatibles mitbringen. Was ein Projekt dann anpassen muss, steht im Abschnitt „Geändert“.

## Unveröffentlicht

## 0.4.0 – 2026-11-27

Erste öffentliche Fassung.

### Hinzugefügt

- MIT-Lizenz (`LICENSE`).
- Versionshistorie (`CHANGELOG.md`) und Ablauf für Veröffentlichungen (`RELEASING.md`).
- `fabrik version` und `fabrik --version`: zeigen die Version des Kits und, im Projektordner, die dort eingebundene Version.
- Test, dass `kit/VERSION` eine gültige Versionsnummer ist und in der Versionshistorie steht.
- Test gegen persönliche Daten im ganzen Repo, mit optionaler lokaler Wortliste (`.fabrik-private-words`, nicht im Repo).

### Geändert

- Doku von persönlichen Daten, privaten Repos und Auswertungen eigener Projekte bereinigt. Projektbezogene Arbeitsnotizen unter `docs/` entfernt.
- Arbeitsregeln (`AGENTS.md`) um Lizenz- und Versionsregeln ergänzt.

## 0.3.0 – 2026-11-23

### Hinzugefügt

- Partner baut jede Scheibe, Claude steuert und prüft den Diff.
- Modellstufen `standard` und `tief` mit automatischer Wahl für Design, T2/T3-Prüfungen und Bau-Eskalation.
- Partnerbremse ab 90 % im 5-Stunden-Fenster, Claude-Kontingentregeln, `build.py budget`.
- Nutzungsprotokoll `~/.fabrik/usage.jsonl` mit `usage.py snapshot`.
- Commit-Trailer `Gebaut-von` und projektweite `commit_trailers`.
- Bekannte Grenzen (`known_limits.md`) im Prüfprompt.
- Prüfung von PRs ohne Auftragsblatt und Schlussscheibe.

### Geändert

- Schlanke Prüfaufträge: Folgerunden prüfen bei passender Historie nur die Änderungen seit der Vorrunde.
- Prüfung gehärtet: Grenzen nur aus der Merge-Base, Teildiff nur bei gleichem Umfang, `APPROVE` nur aus der Schlusszeile.

## 0.2.0 – 2026-11-21

### Hinzugefügt

- `bauen` öffnet vor der ersten Prüfung einen Entwurfs-PR.
- `review.py`: Angabe der Scheibe, ein Satz je Kriterium, PR-Kommentare im Prompt.

### Geändert

- CI-Gate läuft ohne Token, das Risiko-Label setzt ein eigener Job.
- Skills warten lange Gate- und Prüfläufe in derselben Runde ab.

### Behoben

- Kit-Skripte sind vom Formatierer und Linter des Projekts ausgenommen.

## 0.1.0 – 2026-11-18

### Hinzugefügt

- Kit-Kern: Skills `auftrag`, `bauen`, `pruefen`, `abnahme`.
- Gate, Risiko-Skript mit Stufen T0 bis T3, Stufen-Hook, Prüfung über Kreuz mit Rundenzähler.
- CI-Workflow mit Gate und Merge-Prüfung.
- `fabrik init` und `fabrik setup`.
