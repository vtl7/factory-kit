# fabrik-kit

Skills, Hooks und Skripte, die den Auftragsablauf der Software-Fabrik in Claude Code abbilden: Auftrag, Bauen in Scheiben, Prüfung über Kreuz, Abnahme, Risikostufen T0 bis T3.

- Konzept: [KONZEPT.md](KONZEPT.md)
- Arbeitsregeln: [AGENTS.md](AGENTS.md)
- Versionshistorie: [CHANGELOG.md](CHANGELOG.md)
- Lizenz: [MIT](LICENSE)

Voraussetzungen: `python3` ab 3.9, `git`, [`gh`](https://cli.github.com/) und Claude Code. Optional ein Partner wie Codex für die Prüfung über Kreuz.

## Einbinden

Kit einmal holen, am besten auf einem festen Versions-Tag:

```bash
git clone --branch v0.4.0 <URL dieses Repos> ~/fabrik-kit
```

Einmal je Rechner (prüft `gh`, legt den Partner an):

```bash
~/fabrik-kit/bin/fabrik setup --partner codex
```

Im Projektordner:

```bash
~/fabrik-kit/bin/fabrik init
```

`init` erkennt Prüf- und Testbefehl, Ship und Verify, legt `.fabrik/fabrik.yml` an (eine vorhandene bleibt), kopiert Skills nach `.claude/skills/` und Skripte nach `.claude/fabrik/`, trägt den Stufen-Hook in `.claude/settings.json` ein (vorhandene Einträge bleiben), ergänzt `AGENTS.md`, legt `.github/workflows/fabrik.yml` und die Labels an und erlaubt GitHub Actions, Revert-PRs zu öffnen. Danach die Änderungen über einen PR einreichen.

## Was im Projekt landet

| Datei | Aufgabe |
| --- | --- |
| `.claude/skills/{auftrag,bauen,pruefen,abnahme}/` | Ablauf vom Auftragsblatt bis zur Abnahme |
| `.claude/fabrik/build.py` | Partnerbau starten und abholen. `build.py budget` zeigt Kontingente und Bremsentscheidungen. |
| `.claude/fabrik/usage.py` | `snapshot` protokolliert lesbare Kontingente. `fabrik setup` zeigt den Crontab-Eintrag für alle 30 Minuten. |
| `.claude/fabrik/gate.py` | Prüfbefehl, geschützte Pfade, Rot-Grün-Probe |
| `.claude/fabrik/risk.py` | Risikostufe T0 bis T3 nach Pfaden, Label an PR und Issue |
| `.claude/fabrik/stage_hook.py` | PreToolUse-Hook: Merge, Push auf den Hauptzweig, Ship nach Stufe |
| `.claude/fabrik/review.py` | Prüfung über Kreuz, festes Urteil, drei Runden je Auftrag |
| `.claude/fabrik/known_limits.md` | Bekannte Grenzen des Kits, gelten im Prüfprompt als „kein Fund“, wenn die Datei regulär in der Merge-Base vorliegt |
| `.claude/fabrik/merge_audit.py` | CI nach dem Merge: ohne Prüfurteil Vorfall und Revert-PR |
| `.github/workflows/fabrik.yml` | PR-Gate mit Leserechten, eigener Job für Labels an PR und Issues, Merge-Prüfung mit Schreibrechten für Vorfälle und Revert-PRs |
| `.fabrik/fabrik.yml` | Adapter des Projekts, mit `commit_trailers` für zusätzliche Trailer an Agenten-Commits |

## Partner

`~/.config/fabrik/partner.yml` enthält `bauen`, `pruefen` und `kontingent`. `modelle` und `aufwand` konfigurieren die Stufen `standard` und `tief`. Die Befehle können die Platzhalter `{modell}` und `{aufwand}` ohne Shell-Anführungszeichen einsetzen. Optional liefert `claude_kontingent` den Verbrauch von Claude für `budget` und `snapshot`.

Hat der Partner mitgebaut, prüft ein frischer Claude-Subagent. Ab 90 % im 5-Stunden-Fenster wartet der Auftrag auf den Reset. Nur mit Label `Dringend` darf Claude bei freiem eigenem Kontingent übernehmen. `fabrik init` legt das Label an. Das Nutzungsprotokoll liegt in `~/.fabrik/usage.jsonl`. `FABRIK_USAGE_LOG` überschreibt den Pfad.

## Versionen

Das Kit folgt [Semantic Versioning](https://semver.org/lang/de/). Die Nummer steht in `kit/VERSION`, die Änderungen je Version in [CHANGELOG.md](CHANGELOG.md). `fabrik init` schreibt die Kit-Version samt Commit nach `.claude/fabrik/VERSION` und in den Kopf von `.fabrik/fabrik.yml`. So ist in jedem Projekt nachvollziehbar, welcher Stand eingebunden ist:

```bash
~/fabrik-kit/bin/fabrik version
```

Im Projektordner zeigt der Befehl zusätzlich die eingebundene Version und meldet eine Abweichung. Wie eine neue Version entsteht, steht in [RELEASING.md](RELEASING.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

Mit einer lokalen Datei `.fabrik-private-words` (ein Begriff je Zeile, von Git ignoriert) prüfen die Tests zusätzlich, dass keiner dieser Begriffe in einer eingecheckten Datei steht. Gedacht für eigene Namen und Projekte vor einer Veröffentlichung.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Nutzung, Änderung und Weitergabe sind auch kommerziell erlaubt, solange der Lizenztext mitgegeben wird.
