export const fixture = {
  name: 'Dossier de démonstration',
  location: 'note de cadrage',
  text: 'Le plafond du budget du projet est de 100 000 euros.',
};
export const normalize = (s) =>
  s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().replace(/['’]/g, ' ');
const ignored = new Set(
  'le la les des du de un une quel quelle quels quelles est ce que qui nous pour dans avec peux tu vous pouvez projet on et en mon ma mes ton ta son sa peut il elle sont sur au aux je me se y'.split(
    ' ',
  ),
);
const aliases = {
  cout: 'budget',
  couts: 'budget',
  tarif: 'budget',
  depense: 'budget',
  enveloppe: 'plafond',
  limite: 'plafond',
  echeance: 'delai',
  date: 'delai',
  risques: 'risque',
  prestataire: 'fournisseur',
};
function tokens(text) {
  return new Set(
    normalize(text)
      .split(/[^a-z0-9]+/)
      .filter((x) => x.length > 1 && !ignored.has(x))
      .map((x) => aliases[x] || x),
  );
}
export function passages(pages) {
  return pages.flatMap((p) =>
    p.text
      .split(/(?<=[.!?])\s+|\n+/)
      .filter(Boolean)
      .flatMap((s) => s.match(/.{1,600}(?:\s|$)|.{1,600}/gs) || [])
      .map((text) => ({ ...p, text: text.trim(), terms: tokens(text) })),
  );
}
export function rank(question, index) {
  const query = tokens(question);
  return index
    .map((p) => {
      const hits = [...query].filter((t) => p.terms.has(t)).length;
      return { p, score: hits / Math.sqrt(Math.max(1, p.terms.size)) };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 5)
    .map((x) => ({ name: x.p.name, location: x.p.location, text: x.p.text }));
}
export function lookup(q, pages) {
  return rank(q, passages(pages))[0] || null;
}
export function isQuestion(text) {
  return /\?|\b(quel(?:le)?s?|combien|pourquoi|comment|confirme[sz]?|peux|pouvez|est ce que|peut on|qu en penses|ton avis)\b/.test(
    normalize(text).replaceAll('-', ' '),
  );
}
export function addressed(text, name, previous) {
  if (!isQuestion(text)) return false;
  const canonical = (value) =>
    normalize(value)
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  const own = canonical(name || '');
  const opening = text
    .trim()
    .match(
      /^(?:(?:Bonjour|Alors)\s+)?(\p{Lu}\p{L}*(?:[- ]\p{Lu}\p{L}*)*),/u,
    )?.[1];
  if (opening && own && canonical(opening) === own) return true;
  if (opening && !/^(alors|donc|mais|oui|sinon|bonjour)$/i.test(opening))
    return false;
  if (own) {
    const phrase = ' ' + canonical(text) + ' ';
    const position = phrase.indexOf(' ' + own + ' ');
    if (
      position >= 0 &&
      !/(?:avec|selon|concernant|a|pour|de)$/.test(
        phrase.slice(0, position).trimEnd().split(' ').pop() || '',
      )
    )
      return true;
  }
  if (!name) return true;
  return (
    previous?.channel === 'local' && performance.now() - previous.at < 25000
  );
}
export function evidenceFor(question, index, history) {
  const earlier = history.filter((t) => !isQuestion(t.text)).slice(-12);
  const spoken = earlier.map((t) => ({
    name: t.channel === 'local' ? 'Mon intervention' : 'Intervention distante',
    location: t.time,
    text: t.text,
  }));
  const recentTopic = history
    .slice(-3)
    .map((t) => t.text)
    .join(' ');
  const docs = rank(question, index);
  if (
    recentTopic &&
    docs.length < 3 &&
    (tokens(question).size < 3 ||
      /cette option|dans ce cas|et alors|ce choix/i.test(question))
  ) {
    for (const p of rank(recentTopic, index)) {
      if (
        !docs.some(
          (d) =>
            d.name === p.name && d.location === p.location && d.text === p.text,
        )
      )
        docs.push(p);
      if (docs.length >= 3) break;
    }
  }
  const oral = rank(question, passages(spoken.reverse()));
  return [...docs.slice(0, 3), ...oral.slice(0, 2)];
}
function euroAmount(text) {
  const match = text.match(
    /(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)\s*(k\s*)?(?:€|euros?)/i,
  );
  return match
    ? Number(match[1].replace(/\s/g, '').replace(',', '.')) *
        (match[2] ? 1000 : 1)
    : null;
}
export function firstAnswer(question, evidence) {
  const offer = euroAmount(question);
  if (offer !== null && /devis|offre/i.test(question)) {
    const cap = evidence.find(
      (p) =>
        ['Intervention distante', 'Mon intervention'].includes(p.name) &&
        /plafond|enveloppe/i.test(p.text) &&
        !/hypoth|propos|si |pourrait|serait|pas |ancien/i.test(p.text) &&
        euroAmount(p.text) !== null,
    );
    if (cap) {
      const delta = offer - euroAmount(cap.text);
      const format = (n) => n.toLocaleString('fr-FR') + ' €';
      return delta > 0
        ? 'Avec le plafond annoncé de ' +
            format(euroAmount(cap.text)) +
            ', le devis dépasse le budget de ' +
            format(delta) +
            '. Je propose de confirmer ce plafond avant validation.'
        : 'Avec le plafond annoncé de ' +
            format(euroAmount(cap.text)) +
            ', ce devis laisse une marge de ' +
            format(-delta) +
            '.';
    }
  }
  if (!evidence.length)
    return 'Il me manque un élément du dossier pour répondre avec certitude.';
  if (/valider|recommand|arbitr|pourquoi|comparer/i.test(question))
    return 'Le dossier donne ce premier élément : « ' + evidence[0].text + ' »';
  return evidence[0].text;
}
export function reviewSummary(history) {
  const reviewed = history.filter(t => t.review);
  if (!reviewed.length) return [];
  const expected = reviewed.filter(t => ['Réponse utile', 'Réponse incorrecte', 'Question pour moi manquée'].includes(t.review));
  const useful = expected.filter(t => t.review === 'Réponse utile');
  const fast = useful.filter(t => Number.isFinite(t.record?.timing?.first) && t.record.timing.first < 1000);
  const timings = useful.map(t => t.record?.timing?.first).filter(Number.isFinite).sort((a,b)=>a-b);
  const falseTriggers = reviewed.filter(t => t.review === 'Question pour un autre participant').length;
  const lines = ['## Évaluation manuelle', '', reviewed.length+' interventions évaluées.'];
  if(expected.length) lines.push(fast.length+' / '+expected.length+' questions adressées ont reçu une suggestion jugée utile et mesurée en moins d’une seconde.');
  if(timings.length) lines.push('P95 des suggestions utiles chronométrées : '+timings[Math.ceil(timings.length*.95)-1]+' ms ('+timings.length+' mesures).');
  lines.push('Suggestions adressées à tort : '+falseTriggers+'.','');
  return lines;
}
