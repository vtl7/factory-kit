---
name: auftrag
description: Klärt einen Auftrag oder eine Idee im Leitstand und legt das Auftragsblatt mit Belegvertrag als GitHub-Issue an. Nutzen, wenn der Product Owner etwas bauen, ändern oder festhalten will.
---

# Auftrag klären

Du bist der Leitstand. Du klärst, schreibst das Auftragsblatt und legst Issues an. Du baust nicht selbst.

## Nur ein Gedanke?

1. Ist es kein klarer Auftrag, lege ein Issue mit Label `Idee` an und höre auf: `gh issue create --label Idee --title ... --body ...`.

## Budget

- Rufe vor einem neuen Auftrag `python3 .claude/fabrik/build.py budget` auf. Prüfe Claudes Wochenverbrauch vor jedem neuen Auftrag und vor jedem Claude-Einsatz in diesem Skill, auch nachts. Starte nur unter 70 %. Ab 25 % darf der Verbrauch dem verstrichenen Wochenanteil höchstens 10 Prozentpunkte voraus sein. Bei Claude-Bremse beende die Arbeit mit einem Übergabe-Kommentar am Issue. Gibt es noch kein Issue, hinterlasse die Übergabe im Leitstand.
- Bei Exitcode 14 warte auf den Reset. Nur bei einem bestehenden Issue mit Label `Dringend` darf Claude übernehmen, sofern sein Wochenkontingent frei ist.

## Auftragsblatt

2. Lies `AGENTS.md` und `.fabrik/fabrik.yml` (Befehle, `repos`).
3. Suche die Fundstellen im Code selbst. Frage nichts, was der Code beantwortet.
4. Schreibe das Blatt mit genau diesen Teilen:
   - **Ziel:** ein Satz in Alltagssprache.
   - **Kriterien:** nummeriert, jedes prüfbar.
   - **Belegvertrag:** je Kriterium die Beleg-Art (Test, Prüfbefehl, Verify-Ausgabe, Bildschirmfoto, Abnahme durch den Product Owner) und der konkrete Beleg.
   - **Verhaltensänderung:** ja oder nein.
   - **Fundstellen:** Dateien und Stellen.
   - **Größe:** S, M oder L.
   - **Fragen:** höchstens drei, nur fachliche.
5. Belegvertrag prüfen, bevor gebaut wird: Gibt es den Beleg wirklich, mit den Befehlen und Daten dieses Repos?
6. Unerreichbare Belege nie stehen lassen. Kriterium streichen, in ein eigenes Issue abtrennen oder als „Abnahme durch den Product Owner“ markieren.
7. Keine Kriterien zu Form oder Aufbau von Text, den ein Modell schreibt.

## Größe L

8. Bei L erarbeitet ein frischer Claude-Subagent mit `opus` das Design. Gib ihm zuerst die Fragen an den Code.
9. Lass das Design mit `python3 .claude/fabrik/review.py design < design.md` tief gegenprüfen. Hänge bei einem bestehenden Issue `--issue N` an. Bei Exitcode 14 hinterlasse die Übergabe mit Reset-Zeit und halte an. Bei Exitcode 3 prüft ein frischer Claude-Subagent mit `opus`, sofern sein Kontingent frei ist.
10. Zeige dem Product Owner höchstens 15 Zeilen in Alltagssprache. Er gibt frei, sonst kein Bau.

## Schnitt und Issues

11. Gehört der Auftrag zu mehreren Repos, schneide ihn nach `repos` und der Zuständigkeitskarte in `AGENTS.md`.
12. Lege das Sammel-Issue im Dach-Repo an, je Repo ein Unter-Issue mit dem Teil des Blatts. Bei einem Repo reicht ein Issue.
13. Zeige das Blatt und warte auf die Bestätigung des Product Owners.
14. Nach der Bestätigung: Label `Stapel`. Soll es nachts laufen, zusätzlich `nachts`.
15. ADR nur, wenn eine bleibende Wahl getroffen wird. Höchstens 30 Zeilen.

## Starten

16. Startet die Arbeit gleich, setze `In Arbeit` und starte je Teil eine Bau-Sitzung mit eigenem Worktree, die Teile strikt nacheinander.
17. Gib der Bau-Sitzung nur Repo, Issue-Nummer und den Hinweis auf den Skill `bauen` mit. Das Blatt steht im Issue.
18. Kommt ein Bericht zurück, prüfe ihn gegen das Issue und starte den nächsten Teil.
