---
name: bauen
description: Baut einen bestätigten Auftrag aus einem GitHub-Issue in vertikalen Scheiben mit Test zuerst, Gate nach jeder Scheibe, PR, Prüfung und Merge nach Stufe. Nutzen in einer Bau-Sitzung mit Issue-Nummer.
---

# Bauen in Scheiben

Skripte liegen unter `.claude/fabrik/`. Befehle des Projekts stehen in `.fabrik/fabrik.yml`.

## Lange Läufe

1. Gate, `review.py` und `gh pr checks --watch` dauern mehrere Minuten. Ruf sie im Vordergrund mit einer Zeitgrenze von zehn Minuten auf und warte auf das Ergebnis.
2. Beende deine Runde nie mit „ich warte auf …“. Läuft etwas im Hintergrund, warte in derselben Runde darauf. Nachts stößt dich niemand wieder an.

## Start

3. Lies das Issue: `gh issue view N --json title,body,comments`. Das Auftragsblatt ist der Auftrag.
- Rufe vor einem neuen Auftrag `python3 .claude/fabrik/build.py budget` auf. Prüfe Claudes Wochenverbrauch vor jedem neuen Auftrag und vor jedem Claude-Einsatz in diesem Skill, auch nachts. Starte nur unter 70 %. Ab 25 % darf der Verbrauch dem verstrichenen Wochenanteil höchstens 10 Prozentpunkte voraus sein. Bei Claude-Bremse schreibe die Übergabe und halte an. Bei Exitcode 14 schreibe die Übergabe mit Reset-Zeit, setze `Stapel` und halte an, außer das Issue trägt `Dringend`.
4. Gibt es einen Kommentar „Übergabe“, mach genau dort weiter. Nichts doppelt bauen.
5. Fehlt ein Teil des Blatts oder ein Beleg ist unerreichbar, halte an und melde das dem Leitstand. Nicht raten.
6. Setze Label `In Arbeit`. Arbeite auf einem eigenen Branch `fabrik/N-kurzname`, nie auf dem Hauptzweig.

## Jede Scheibe

7. Schneide nach prüfbarem Verhalten, Ziel 100 bis 200 geänderte Zeilen.
8. Starte den Partner mit `python3 .claude/fabrik/build.py start --issue N --slice K/N --task "<Zuschnitt>"`.
- Ist dieselbe Scheibe zweimal am Gate oder in der Prüfung gescheitert, ergänze beim nächsten Start `--eskalation`. Das Skript merkt sich den einmaligen Tiefbau am Issue.
9. Nach erfolgreichem Start rufe `python3 .claude/fabrik/build.py wait` im Vordergrund mit zehn Minuten Zeitgrenze auf. Wiederhole bei Exitcode 9 in derselben Runde.
10. Bei Exitcode 0 lies die Zusammenfassung, prüfe den Diff samt neuen Dateien und den Test. Nimm den geprüften Partnerstand in den Index und committe ihn mit dem ausgegebenen Befehl. Er hängt `Gebaut-von: <Partner>` und die Trailer aus `commit_trailers` in `.fabrik/fabrik.yml` als letzten Absatz an. Danach behebe kleine Mängel selbst und committe sie mit dem ausgegebenen Befehl für eigene Korrekturen, ohne `Gebaut-von`. Für größere Nachbesserungen starte aus dem sauberen Stand einen weiteren Partnerbau mit konkretem Auftrag und prüfe sein Ergebnis ebenso.
11. Bei Exitcode 3 prüfe zuerst alle verwertbaren Teiländerungen des Partners und committe sie mit dem ausgegebenen Partnerbefehl samt `Gebaut-von`. Danach prüfe Claudes Kontingent. Sobald es frei ist, nutze für den Subagenten `sonnet`. War der Bau mit `--eskalation` angefordert, rufe unmittelbar vor dem Subagentenstart `python3 .claude/fabrik/build.py eskalation --issue N --slice K/N` auf und nutze das dort ausgegebene Modell. Committe diese Arbeit in einem eigenen Commit ohne `Gebaut-von`. Jeder eigene Commit trägt die Trailer aus `commit_trailers` im letzten Absatz. Bei Verhaltensänderungen schreibe zuerst den fehlschlagenden Test.
- Bei Exitcode 14 sichere verwertbare Teiländerungen mit dem ausgegebenen Partnerbefehl. Schreibe die Übergabe mit Reset-Zeit, setze `Stapel` und halte an.
12. Bei Exitcode 8 oder 2 prüfe die Ursache und den Arbeitsstand. Bei geändertem HEAD folge dem Wiederherstellungshinweis von `wait`. Andere offene Ergebnisse hole mit `wait` ab. Sonst starte nach Behebung einmal neu. Scheitert der Versuch erneut, gehe wie bei Exitcode 3 vor.
13. Gate: `python3 .claude/fabrik/gate.py --slice-base <Commit vor der Scheibe>`, bei Verhaltensänderung mit `--behavior-change`.
14. Gate rot: dieselbe Scheibe nachbessern und das Gate wiederholen. Nach zwei gescheiterten Runden nutze die Bau-Eskalation. Bei Rot prüft kein Modell.
15. Geschützte Pfade sind rot, weil sie nicht geändert werden dürfen. Nicht umgehen, sondern melden.

## Nach jeder grünen Scheibe

16. Pushen.
17. Nur nach der ersten Scheibe: den PR als Entwurf öffnen, `gh pr create --draft`. Die Prüfung braucht ihn; weitere Scheiben landen im selben PR.
18. Im Text: `Refs #N`, nie `Closes` oder `Fixes`. Das Issue schließt erst die Abnahme.
19. Schreibe in den PR-Text die Zeile `Verhaltensänderung: ja` oder `Verhaltensänderung: nein`, wie im Blatt.
20. Dann prüfen mit dem Skill `pruefen`. Gib die Scheibe K/N an und nenne bei einer Zwischenprüfung die noch geplante Arbeit. Stufe ansehen mit `python3 .claude/fabrik/risk.py`. Die CI setzt das Label selbst.

## Prüfung und Merge

21. `CHANGES`: Funde in derselben Scheibe beheben, dann Gate und `pruefen` wiederholen.
22. `ESCALATE` oder `Festgefahren`: anhalten, Übergabe schreiben, dem Leitstand die eine Frage melden.
23. Nach der letzten Scheibe mit `APPROVE`: `gh pr ready`, dann auf die CI warten mit `gh pr checks --watch`.
24. CI ohne neuen Commit neu starten: `gh run rerun <id>`. Nie einen leeren Commit, er entwertet das Prüfurteil.
25. Merge mit `gh pr merge <PR> --squash --delete-branch`. Der Stufen-Hook entscheidet: T0 und T1 laufen durch, T2 und T3 fragen.
26. Lehnt der Hook ab oder fragt nachts ohne Antwort: nicht umgehen, kein Skript, kein API-Umweg. Übergabe schreiben, Label `Wartet auf dich`, weiter mit dem nächsten Issue.
27. Nach dem Merge weiter mit dem Skill `abnahme`.

## Übergabe bei jedem Halt

28. Hältst du an, egal warum, schreibe ins Issue:
    `gh issue comment N --body "Übergabe: Commit <sha>, PR #<nr>, letzter Beleg <was>, nächster Schritt <genau einer>"`.
29. Melde dem Leitstand kurz Stand und nächsten Schritt.
