# Versionsverlauf

Dieses Dokument hält nutzerrelevante Änderungen fest. Versionen folgen dem
Schema `MAJOR.MINOR.PATCH`.

## 0.2.1 — 2026-10-24

### Behoben

- Die zentrale Render-Statusanzeige verwendet nun ihren eigenen sicheren
  Text-Encoder. Sie bleibt bei blockierter Queue auch während jeder
  Hintergrundaktualisierung auf **Blockiert** sichtbar.

## 0.2.0 — 2026-10-24

### Verbessert

- Neues Jobs-Tab nach dem Bedienmuster von MusicAI und ASMRAI: vereinheitlichte
  Ansicht für Pipeline- und CLI-Jobs mit Filtern für laufende, wartende,
  fertige und fehlgeschlagene Einträge sowie einsehbaren CLI-Logs.
- Der Job-Schalter oben öffnet direkt die Jobs-Ansicht statt das Terminal.
- Fehlerdiagnosen liegen in der Jobs-Ansicht als einklappbarer Verlauf und
  überlagern die operative Übersicht nicht mehr.
- Die Renderblockierung wird aus einem gemeinsamen Statusmodell dargestellt;
  konkurrierende Aktualisierungen können ETA und Warnung nicht mehr kurz
  ausblenden.

## 0.1.9 — 2026-10-22

### Behoben

- Der NAS-Pool startet SeriesAI nun als reines Dashboard ohne Modell-Downloads,
  LLM oder Render-Worker. Dadurch kann ein abgebrochener Vision-Download den
  öffentlichen Start nicht mehr blockieren.
- Ein alter Start-Lock wird nur dann kontrolliert zurückgewonnen, wenn sein
  nachweislich zu SeriesAI gehörender Prozess seit mindestens zwei Minuten
  kein Dashboard mehr bereitstellt. Aktive Instanzen bleiben geschützt.

## 0.1.8 — 2026-10-21

### Behoben

- Der Job-Schalter im Kopf bleibt bei einer blockierten Render-Warteschlange
  konsistent auf **„Render blockiert“**, statt sich im Aktualisierungstakt
  mit dem überholten Hinweis auf den laufenden Job abzuwechseln.

## 0.1.7 — 2026-10-21

### Behoben

- Eine nicht erreichbare ComfyUI-API oder ein festgefahrener Render-Shot wird
  im Dashboard nun als **blockiert** ausgewiesen. Die frühere, irreführende
  Render-ETA wird in diesem Zustand nicht mehr angezeigt.
- Die Jobanzeige oben und die Render-Warteschlange zeigen den Blockierungsgrund
  statt den Job fälschlich als aktiv zu präsentieren.

## 0.1.6 — 2026-10-19

### Behoben

- Ein expliziter Dashboard-Job repariert fehlende lokale NumPy- und
  Szenenerkennungs-Abhängigkeiten in der tatsächlich verwendeten NAS-Umgebung.
  Seine vollständige Pip-Ausgabe bleibt in der Job-Historie nachvollziehbar.

## 0.1.5 — 2026-10-19

### Behoben

- Die Job-Verwaltung lädt keine optionale Szenenerkennungs-Bibliothek mehr
  beim Öffnen des Dashboards. Dadurch bleiben Terminal, Befehlsliste und
  Job-Historie bedienbar, auch wenn eine Produktionsabhängigkeit noch fehlt.
- Fehlende Stimmen-Abhängigkeiten liefern eine konkrete Installationsmeldung
  statt einen abgebrochenen Proxy-Aufruf.
- Die aktive Jobanzeige oben rechts ist nun eine Schaltfläche und öffnet direkt
  die Terminal-Ansicht mit der vollständigen Job-Historie und Live-Ausgabe.

## 0.1.4 — 2026-10-19

### Behoben

- Optionale Audio-/ML-Abhängigkeiten werden erst beim tatsächlich benötigten
  Vertonungsschritt geladen. Ein fehlendes `numpy` kann damit nicht mehr den
  Start des Coordinators oder des Dashboards blockieren oder eine Fehlerschleife
  in der Episodenwarteschlange auslösen.

## 0.1.3 — 2026-10-18

### Behoben

- Der Projektstarter akzeptiert jetzt den vom All-for-One-Launcher verwendeten
  Aufruf `start.py serve --yes`. Zuvor beendete sich SeriesAI wegen unbekannter
  Argumente, bevor das Dashboard starten konnte.

## 0.1.2 — 2026-10-18

### Behoben

- Dashboard-Pfadparameter werden nicht mehr als frei editierbare Textfelder
  angezeigt. Stattdessen erfolgt die Auswahl über einen integrierten NAS-Datei-
  beziehungsweise Ordnerdialog.
- Der Server akzeptiert bei Dashboard-CLI-Jobs ausschließlich Pfade innerhalb
  der lokalen SeriesAI-NAS-Projektfreigabe. Direkte oder manipulierte Anfragen
  auf lokale PC-Laufwerke werden abgewiesen.

## 0.1.1 — 2026-10-17

### Behoben

- Remote-Renderworker halten einen aktiven Shot jetzt während langer ComfyUI-
  Renderläufe und MP4-Uploads per Heartbeat aktiv. Der Coordinator gibt ihn
  dadurch nicht mehr nach dem Worker-Timeout fälschlich erneut frei.
- Die zwei Transkriptions-Backend-Angaben werden beim Laden der Konfiguration
  auf Gleichheit geprüft. Die Standardkonfiguration verwendet konsistent
  `faster_whisper`.

### Verbessert

- Anwendungs- und Fehlerlogs rotieren nun bei 10 MiB (fünf Sicherungen) und
  enthalten Prozess- und Thread-Kontext.
- Renderfehler enthalten vollständige Python-Stacktraces in `logs/errors.log`.

## 0.1.0

- Erste lokale, fortsetzbare Series-Brain- und Render-Queue-Grundlage.
