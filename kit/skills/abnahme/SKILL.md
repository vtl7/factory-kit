---
name: abnahme
description: Liefert nach dem Merge aus, führt Verify aus, gleicht jedes Kriterium mit dem Belegvertrag ab und schließt das Issue erst danach. Nutzen nach dem Merge eines Auftrags-PR.
---

# Abnahme

Die Abnahme prüft gegen den Belegvertrag im Issue, nicht gegen neue Wünsche.

## Ausliefern

1. Hauptzweig holen: `git checkout <main_branch> && git pull`.
2. Steht `ship` in `.fabrik/fabrik.yml`, führe genau diesen Befehl aus. Keinen anderen Weg in die Produktion.
3. Der Stufen-Hook entscheidet: T0 und T1 laufen durch, T2 und T3 fragen.
4. Lehnt er ab oder bleibt die Frage nachts offen: nicht umgehen. Übergabe schreiben, Label `Wartet auf dich`, anhalten.
5. Schlägt Ship fehl: nichts reparieren auf dem Hauptzweig. Übergabe schreiben, dem Leitstand melden.
6. Steht `verify` in der Konfiguration, führe es danach aus und hebe die Ausgabe auf.

## Belege abgleichen

7. Lies den Belegvertrag im Issue.
8. Gehe Kriterium für Kriterium durch und nenne den Beleg: Test, Prüfbefehl mit Exitcode, Verify-Ausgabe, Bildschirmfoto.
9. Nur Belege, die es wirklich gibt. Nichts schätzen, nichts als belegt ausgeben, was nicht lief.
10. Fehlt ein Beleg, ist das ein Hinweis im Bericht, keine Sperre und kein Grund, neu zu bauen.
11. Ist ein Kriterium als „Abnahme durch den Product Owner“ markiert, frage ihn im Leitstand und warte auf seine Antwort.

## Abschluss

12. Schreibe den Abnahme-Kommentar ins Issue:
    je Kriterium „erfüllt“, „nicht belegt“ oder „wartet auf Product Owner“, mit Beleg.
13. Sind alle Kriterien erfüllt oder nur Belege offen: Issue schließen mit `gh issue close N --comment "Abgenommen."`.
14. Wartet ein Kriterium auf den Product Owner: Issue offen lassen, Label `Wartet auf dich`.
15. Entferne `In Arbeit`, wenn das Issue geschlossen ist.
16. Melde dem Leitstand in drei Sätzen: was geliefert ist, welche Belege, was offen bleibt.

## Vorfall

17. Ein Vorfall ist nur unerwartetes Verhalten: Ship oder Verify brechen ab, Daten falsch, Hook oder Gate verhalten sich anders als beschrieben.
18. Dann ein Issue mit Label `Vorfall`: was passiert ist, was erwartet war, welcher Check es früher hätte finden können.
19. Warten auf den Product Owner ist Status, kein Vorfall.
