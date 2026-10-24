// Pool-Anbindung fuer SeriesAI: verteilte Transkription (Whisper im Browser, jeder Abschnitt wird doppelt geprueft).
// Eingang: pool/input (nur dort abgelegte Dateien werden verteilt), Ergebnis: pool/results/<datei>.transcript.json
const path = require('path');
const { createQueue } = require('../../server/chunk-queue');
const log = require('../../server/logger').for('pool-seriesai');
const queue = createQueue({ id: 'seriesai', dir: __dirname, model: 'onnx-community/whisper-base', language: 'german', sources: [path.join(__dirname, 'Series', 'Input'), path.join(__dirname, 'Series_Source')], log });

module.exports = {
  id: 'seriesai',
  name: 'SeriesAI',
  description: 'Baut aus TV-Folgen ein Serien-Gehirn und erzeugt neue Episoden. Im Pool: Sprach-Transkription von Audio-Abschnitten.',
  enabled: true,
  // The NAS supervisor must bring up the dashboard first.  Model downloads
  // and optional GPU services are explicit jobs; otherwise an interrupted
  // download can hold the single-instance lock and make the dashboard vanish.
  nasArgs: ['serve', '--yes', '--no-install', '--no-llm', '--no-render-worker'],
  alive: 'http://127.0.0.1:8770/',   // Antwort = start.py laeuft; der Pool startet das Projekt nie selbst
  browserOnly: true,
  createTask: ctx => queue.nextTask(ctx.userId),
  hasWork: () => queue.hasWork(),
  queueSummary: () => queue.summary(),
  queueOverview: () => queue.overview(),
  getChunk: task => queue.getChunk(task),
  acceptResult: (task, result, ctx) => queue.accept(task, result, ctx.userId, ctx.trusted),
};
