import {
  fixture,
  passages,
  evidenceFor,
  addressed,
  firstAnswer,
} from './engine.mjs';
const $ = (id) => document.getElementById(id);
const docs = [],
  semantic = [];
async function vectors(texts, signal) {
  const response = await fetch('/embed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ texts }),
    signal,
  });
  if (!response.ok) throw Error('Recherche sémantique indisponible.');
  return response.json();
}
async function prepare(pages) {
  const items = passages(pages);
  for (let i = 0; i < items.length; i += 16) {
    const batch = items.slice(i, i + 16);
    const result = await vectors(
      batch.map((p) => 'title: ' + p.name.slice(0, 200) + ' | text: ' + p.text),
    );
    batch.forEach((p, n) =>
      semantic.push({
        name: p.name,
        location: p.location,
        text: p.text,
        vector: result[n],
      }),
    );
  }
}
async function enrich(question, evidence, signal) {
  if (!session?.live || !semantic.length) return evidence;
  try {
    const [q] = await vectors(
      ['task: search result | query: ' + question.slice(0, 1300)],
      AbortSignal.any([signal, AbortSignal.timeout(2500)]),
    );
    const top = semantic
      .map((p) => ({
        p,
        score: p.vector.reduce((sum, x, i) => sum + x * q[i], 0),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, 3);
    const result = [...evidence];
    for (const { p } of top) {
      if (
        !result.some(
          (r) =>
            r.name === p.name && r.location === p.location && r.text === p.text,
        )
      )
        result.push({ name: p.name, location: p.location, text: p.text });
    }
    return result.slice(0, 6);
  } catch {
    return evidence;
  }
}
let preview = null;
let session = null,
  last = '',
  candidate = [],
  index = [],
  history = [],
  suggestions = [],
  request;
const scripted = [
  [3.268, 'Le nouveau plafond du budget est de 80 000 euros.'],
  [7.505, 'Julie, est-ce que tu peux envoyer le devis ?'],
  [
    13.808,
    'Camille, peut-on valider le devis de 90 000 euros avec ce budget ?',
  ],
];
const configured = (await (await fetch('/status')).json()).configured;
const status = (msg) => {
  $('status').textContent = msg;
};
function buttons() {
  for (const id of ['demo', 'cloud', 'live', 'teams'])
    $(id).disabled = !!session || (id !== 'demo' && !configured);
  $('stop').disabled = !session;
}
buttons();
if (window.meetingDesktop) {
  $('live').hidden = true;
  $('teams').insertAdjacentText('afterend', ' Pour un onglet Teams Web ou Meet, utilise le menu « Ouvrir dans Chrome ». ');
}
if (!configured) status('Démo hors ligne disponible · clé Deepgram absente.');
$('files').onchange = async (e) => {
  for (const file of e.target.files) {
    try {
      const res = await fetch('/import', {
        method: 'POST',
        headers: { 'x-format': file.name.split('.').pop().toLowerCase() },
        body: file,
      });
      if (!res.ok) throw Error(await res.text());
      const pages = (await res.json()).filter((p) => p.text.trim());
      if (!pages.length) throw Error('Aucun texte extractible.');
      const imported = pages.map((p) => ({ ...p, name: file.name }));
      docs.push(...imported);
      prepare(imported).catch(() => {
        $('docs').title =
          'Index lexical disponible. Le modèle sémantique local est indisponible.';
      });
      if (session?.live) index = passages(docs);
      $('docs').textContent = [...new Set(docs.map((p) => p.name))].join(' · ');
    } catch (err) {
      status(file.name + ' — ' + err.message);
    }
  }
};
function show(acousticEnd) {
  const question = last,
    evidence = candidate;
  request?.abort();
  const controller = new AbortController();
  request = controller;
  const answer = firstAnswer(question, evidence);
  const previewAt = preview?.answer === answer ? preview.at : null;
  preview = null;
  $('preview').textContent = '';
  $('answer').textContent = answer;
  $('source').textContent = evidence
    .map((p) => p.name + ' · ' + p.location + ' — ' + p.text)
    .join('\n');
  $('latency').textContent = '';
  if (Number.isFinite(acousticEnd))
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (request === controller) {
          const confirmation = Math.round(performance.now() - acousticEnd);
          const first =
            previewAt === null
              ? confirmation
              : Math.max(0, Math.round(previewAt - acousticEnd));
          record.timing = { first, confirmation };
          $('latency').textContent =
            'Suggestion disponible : ' +
            first +
            ' ms · confirmation : ' +
            confirmation +
            ' ms après le dernier mot (estimation).';
        }
      }),
    );
  const record = {
    question,
    answer: $('answer').textContent,
    sources: evidence,
  };
  suggestions.push(record);
  $('deep').textContent = 'Analyse en cours sur ce PC…';
  enrich(question, evidence, controller.signal)
    .then((proofs) =>
      fetch('/synthesis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          question,
          evidence: proofs,
          context: history.slice(-6).map((t) => t.text),
        }),
      }),
    )
    .then(async (res) => {
      if (!res.ok) throw Error(await res.text());
      return res.json();
    })
    .then((data) => {
      if (request !== controller) return;
      record.analysis = data;
      $('deep').replaceChildren();
      for (const line of [
        data.answer,
        ...data.arguments,
        ...data.sources.map(
          (p) => p.name + ' · ' + p.location + ' — ' + p.text,
        ),
      ]) {
        const item = document.createElement('p');
        item.textContent = line;
        $('deep').append(item);
      }
    })
    .catch((err) => {
      if (!controller.signal.aborted) $('deep').textContent = err.message;
    });
}
function receive(channel, text, final, acousticEnd) {
  $(channel).textContent = text;
  if (channel === 'remote') {
    last = text;
    candidate = evidenceFor(text, index, history);
    const name = $('name').value.trim() || (session?.live ? '' : 'Camille');
    const direct = addressed(text, name, history.at(-1));
    if (!final && direct && candidate.length && text.split(/\s+/).length >= 5) {
      const answer = firstAnswer(text, candidate);
      if (preview?.answer !== answer) {
        preview = { answer, at: performance.now() };
        $('preview').textContent = 'En cours de question · ' + answer;
      }
    } else if (!direct) {
      preview = null;
      $('preview').textContent = '';
    }
    if (final && direct) {
      const knownEnd = session.clipStart + session.clipEnd * 1000;
      show(
        Number.isFinite(knownEnd) && performance.now() >= knownEnd
          ? knownEnd
          : acousticEnd,
      );
    }
  }
  if (final) {
    const seconds = Math.floor((performance.now() - session.started) / 1000);
    const time =
      Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
    history.push({ channel, text, time, at: performance.now() });
    const li = document.createElement('li');
    li.textContent =
      time +
      ' · ' +
      (channel === 'local' ? 'Moi' : 'Interlocuteur') +
      ' — ' +
      text;
    if (channel === 'remote') {
      const record =
        final && suggestions.at(-1)?.question === text
          ? suggestions.at(-1)
          : null;
      const review = document.createElement('select');
      review.setAttribute('aria-label', 'Évaluer cette intervention');
      const choices = record
        ? [
            'Évaluer la suggestion',
            'Réponse utile',
            'Réponse incorrecte',
            'Question pour un autre participant',
          ]
        : [
            'Évaluer cette intervention',
            'Question pour moi manquée',
            'Aucune suggestion attendue',
          ];
      choices.forEach((label, i) =>
        review.add(new Option(label, i ? label : '')),
      );
      const entry = history.at(-1);
      review.onchange = () => {
        entry.review = review.value;
      };
      li.append(' ', review);
    }
    $('history').append(li);
    $('export').disabled = false;
  }
}
$('manual').onclick = () => {
  if (last) show();
};
function end(message) {
  preview = null;
  $('preview').textContent = '';
  for (const channel of ['local', 'remote']) $(channel + '-level').value = 0;
  const old = session;
  session = null;
  for (const clean of old?.clean || []) clean();
  buttons();
  status(message);
}
$('stop').onclick = () => end('Écoute arrêtée.');
async function start(mode) {
  const s = {
    clean: [],
    started: performance.now(),
    live: mode === 'live' || mode === 'teams',
  };
  session = s;
  buttons();
  status('Ouverture de la capture audio…');
  const own = (cleanup) => {
    if (session !== s) {
      cleanup();
      throw Error('Session terminée');
    }
    s.clean.push(cleanup);
  };
  const track = (stream) => {
    own(() => stream.getTracks().forEach((t) => t.stop()));
    return stream;
  };
  index = passages(s.live ? docs : [fixture]);
  history = [];
  suggestions = [];
  $('export').disabled = true;
  request?.abort();
  request = null;
  $('history').replaceChildren();
  last = '';
  candidate = null;
  for (const id of ['local', 'remote', 'source', 'answer', 'latency', 'deep'])
    $(id).textContent = '—';
  try {
    let stream, audio;
    if (s.live) {
      if (mode === 'live') {
        const display = track(
          await navigator.mediaDevices.getDisplayMedia({
            video: true,
            audio: true,
          }),
        );
        if (
          !display.getAudioTracks().length ||
          display.getVideoTracks()[0].getSettings().displaySurface !== 'browser'
        )
          throw Error('Choisis un onglet avec partage audio activé.');
        stream = new MediaStream(display.getAudioTracks());
        display.getVideoTracks()[0].onended = () => {
          if (session === s) end('Partage terminé.');
        };
      }
      const mic = track(
        await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true },
        }),
      );
      await Promise.all([
        listen(stream, 'remote', mode === 'teams'),
        listen(mic, 'local'),
      ]);
      if (session === s) status('Écoute en direct · Deepgram');
    } else {
      const decision = $('scenario').value === 'decision';
      s.clipEnd = decision ? 13.808 : 3.18;
      audio = new Audio(decision ? '/decision.wav' : '/question.wav');
      own(() => audio.pause());
      if (mode === 'cloud') {
        const ctx = new AudioContext();
        own(() => ctx.close());
        await ctx.resume();
        const node = ctx.createMediaElementSource(audio),
          dest = ctx.createMediaStreamDestination();
        node.connect(ctx.destination);
        node.connect(dest);
        await listen(track(dest.stream), 'remote');
      } else {
        let turn = 0;
        audio.ontimeupdate = () => {
          if (session !== s) return;
          if (decision) {
            while (
              turn < scripted.length &&
              audio.currentTime >= scripted[turn][0]
            ) {
              const [end, text] = scripted[turn++];
              receive(
                'remote',
                text,
                true,
                performance.now() - (audio.currentTime - end) * 1000,
              );
            }
            return;
          }
          if (audio.currentTime > 0.5)
            receive(
              'remote',
              audio.currentTime < audio.duration * 0.6
                ? 'Camille, quel est le plafond'
                : 'Camille, quel est le plafond du budget pour le projet',
              false,
            );
        };
      }
      audio.onended = () => {
        if (session !== s) return;
        if (mode === 'demo') {
          if (!decision)
            receive(
              'remote',
              'Camille, quel est le plafond du budget pour le projet ?',
              true,
            );
          end('Démo terminée · transcription témoin préenregistrée.');
        } else {
          const timer = setTimeout(() => {
            if (session === s) end('Démo Deepgram terminée.');
          }, 3000);
          own(() => clearTimeout(timer));
        }
      };
      if (session !== s) return;
      status(
        mode === 'cloud'
          ? 'Démo audio · transcription Deepgram'
          : 'Démo audio · transcription témoin',
      );
      audio.onplaying = () => {
        s.clipStart = performance.now() - audio.currentTime * 1000;
      };
      await audio.play();
    }
  } catch (err) {
    if (session === s) end(err.message);
  }
  async function listen(input, channel, desktop = false) {
    let started, lastWord;
    let ws,
      lastMeter = 0;
    let begin = () => {};
    if (input) {
      const ctx = new AudioContext({ sampleRate: 48000 });
      own(() => ctx.close());
      await ctx.audioWorklet.addModule('/pcm.js');
      await ctx.resume();
      const source = ctx.createMediaStreamSource(input);
      const tap = new AudioWorkletNode(ctx, 'pcm-capture', {
        numberOfOutputs: 0,
        channelCount: 1,
        channelCountMode: 'explicit',
      });
      own(() => {
        source.disconnect();
        tap.disconnect();
        tap.port.close();
      });
      tap.port.onmessage = (event) => {
        if (session !== s) return;
        if (performance.now() - lastMeter > 80) {
          $(channel + '-level').value = Math.min(
            1,
            Math.sqrt(event.data.rms) * 2,
          );
          lastMeter = performance.now();
        }
        if (ws?.readyState === 1) ws.send(event.data.pcm);
      };
      begin = () => source.connect(tap);
    }
    ws = new WebSocket(
      'ws://' +
        location.host +
        (desktop ? '/teams' : '/listen') +
        '?' +
        new URLSearchParams({
          name: $('name').value.trim() || (s.live ? '' : 'Camille'),
        }),
    );
    own(() => ws.close());
    let prefix = '';
    await new Promise((resolve, reject) => {
      ws.onclose = (event) => {
        reject(Error('Connexion Deepgram interrompue.'));
        if (session === s)
          end(
            event.reason ||
              'Connexion Deepgram interrompue. Vérifie la clé et le réseau.',
          );
      };
      ws.onmessage = (e) => {
        if (session !== s) return;
        const msg = JSON.parse(e.data);
        if (msg.type === 'ready') {
          started = performance.now();
          begin();
          resolve();
          return;
        }
        if (msg.type === 'level') {
          $(channel + '-level').value = msg.value;
          return;
        }
        if (msg.type !== 'Results') return;
        const part = msg.channel.alternatives[0]?.transcript || '';
        const full = (prefix + ' ' + part).trim();
        const end = msg.channel.alternatives[0]?.words?.at(-1)?.end;
        if (Number.isFinite(end)) lastWord = started + end * 1000;
        if (full) receive(channel, full, msg.speech_final, lastWord);
        if (msg.is_final) prefix = full;
        if (msg.speech_final) prefix = '';
      };
    });
  }
}
for (const id of ['demo', 'cloud', 'live', 'teams'])
  $(id).onclick = () => start(id);

$('export').onclick = () => {
  const lines = [
    '# Compte rendu de réunion',
    '',
    ...history.map(
      (t) =>
        t.time +
        ' · ' +
        (t.channel === 'local' ? 'Moi' : 'Interlocuteur') +
        ' — ' +
        t.text +
        (t.review ? ' [' + t.review + ']' : ''),
    ),
    '',
    '## Suggestions de l’assistant',
    '',
  ];
  for (const item of suggestions) {
    lines.push(
      '### ' + item.question,
      item.answer,
      ...(item.timing
        ? [
            'Suggestion disponible en ' +
              item.timing.first +
              ' ms, confirmée en ' +
              item.timing.confirmation +
              ' ms.',
          ]
        : []),
      ...(item.analysis
        ? [item.analysis.answer, ...item.analysis.arguments]
        : []),
      ...[
        ...new Map(
          [...item.sources, ...(item.analysis?.sources || [])].map((p) => [
            JSON.stringify(p),
            p,
          ]),
        ).values(),
      ].map((p) => '- ' + p.name + ' · ' + p.location + ' — ' + p.text),
      '',
    );
  }
  const url = URL.createObjectURL(
    new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = 'compte-rendu-reunion.md';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
