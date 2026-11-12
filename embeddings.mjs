export async function embedTexts(texts, signal) {
  if (
    !Array.isArray(texts) ||
    !texts.length ||
    texts.length > 24 ||
    texts.some((t) => typeof t !== 'string' || t.length > 1600)
  )
    throw Error('Lot de passages invalide.');
  const res = await fetch('http://127.0.0.1:11434/api/embed', {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: process.env.EMBED_MODEL || 'embeddinggemma',
      input: texts,
      keep_alive: '5m',
    }),
  });
  if (!res.ok) throw Error('Index sémantique indisponible.');
  const { embeddings } = await res.json();
  if (embeddings?.length !== texts.length) throw Error('Index incomplet.');
  return embeddings;
}
