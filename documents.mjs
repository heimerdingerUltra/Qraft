import JSZip from 'jszip';
import { XMLParser } from 'fast-xml-parser';
export async function extract(buffer, format) {
  if (format === 'pdf') {
    const { getDocument } = await import('pdfjs-dist/legacy/build/pdf.mjs');
    const task = getDocument({
      data: new Uint8Array(buffer),
      isEvalSupported: false,
    });
    const doc = await task.promise;
    let worker;
    try {
      const pages = [];
      for (let i = 1; i <= doc.numPages; i++) {
        const page = await doc.getPage(i);
        const content = await page.getTextContent();
        let text = content.items
          .map((x) => x.str || '')
          .join(' ')
          .trim();
        if (!text) {
          const { ocrPage, frenchWorker } = await import('./ocr.mjs');
          worker ??= await frenchWorker();
          text = await ocrPage(page, worker);
        }
        pages.push({ location: 'page ' + i, text });
      }
      return pages;
    } finally {
      try {
        await worker?.terminate();
      } finally {
        await task.destroy();
      }
    }
  }
  if (format !== 'pptx') throw Error('Format');
  const zip = await JSZip.loadAsync(buffer);
  const parser = new XMLParser({
    ignoreAttributes: false,
    parseTagValue: false,
  });
  const xml = async (name) =>
    parser.parse(await zip.file(name).async('string'));
  const slides = (await xml('ppt/presentation.xml'))['p:presentation'][
    'p:sldIdLst'
  ]['p:sldId'];
  const rels = (await xml('ppt/_rels/presentation.xml.rels')).Relationships
    .Relationship;
  const pages = [];
  function collect(node, out) {
    if (!node || typeof node !== 'object') return;
    for (const [key, value] of Object.entries(node)) {
      if (key === 'a:t')
        out.push(typeof value === 'object' ? value['#text'] : value);
      else collect(value, out);
    }
  }
  for (const slide of [slides].flat()) {
    const target = [rels].flat().find((r) => r['@_Id'] === slide['@_r:id'])[
      '@_Target'
    ];
    const path = new URL(target, 'https://internal/ppt/').pathname.slice(1);
    const words = [];
    collect(await xml(path), words);
    pages.push({
      location: 'diapo ' + (pages.length + 1),
      text: words.join(' '),
    });
  }
  return pages;
}
