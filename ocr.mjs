import { createWorker } from 'tesseract.js';
import french from '@tesseract.js-data/fra';
import { createCanvas } from '@napi-rs/canvas';
export async function ocrPage(page, worker) {
  const original = page.getViewport({ scale: 1 });
  const viewport = page.getViewport({
    scale: Math.min(2.5, 2500 / Math.max(original.width, original.height)),
  });
  const canvas = createCanvas(
    Math.ceil(viewport.width),
    Math.ceil(viewport.height),
  );
  await page.render({ canvasContext: canvas.getContext('2d'), viewport })
    .promise;
  return (
    await worker.recognize(canvas.toBuffer('image/png'))
  ).data.text.trim();
}
export const frenchWorker = () =>
  createWorker('fra', 1, { langPath: french.langPath, cacheMethod: 'none' });
