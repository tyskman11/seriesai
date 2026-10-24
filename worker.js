// Laeuft im Web Worker des Pools: Whisper-Transkription eines Audio-Abschnitts.
importScripts('/whisper-task.js');
registerProject('seriesai', (task, ctx) => self.runWhisperTask(task, ctx));
