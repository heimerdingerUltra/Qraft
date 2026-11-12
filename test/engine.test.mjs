import test from 'node:test';
import assert from 'node:assert/strict';
import { lookup, fixture } from '../public/engine.mjs';
import { extract } from '../documents.mjs';
import JSZip from 'jszip';

test('Recherche budgétaire et question hors dossier', () => {
  assert.equal(
    lookup('Quel est le plafond du budget ?', [fixture]).text,
    fixture.text,
  );
  assert.equal(lookup('Combien de serveurs disponibles ?', [fixture]), null);
});
test('Respecte l’ordre visible des diapositives', async () => {
  const zip = new JSZip();
  zip.file(
    'ppt/presentation.xml',
    '<p:presentation><p:sldIdLst><p:sldId r:id="r2"/><p:sldId r:id="r1"/></p:sldIdLst></p:presentation>',
  );
  zip.file(
    'ppt/_rels/presentation.xml.rels',
    '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>',
  );
  for (const n of [1, 2])
    zip.file(
      'ppt/slides/slide' + n + '.xml',
      '<p:sld><a:r><a:t>Page ' + n + '</a:t></a:r></p:sld>',
    );
  const pages = await extract(
    await zip.generateAsync({ type: 'nodebuffer' }),
    'pptx',
  );
  assert.deepEqual(
    pages.map((p) => p.text),
    ['Page 2', 'Page 1'],
  );
});
import {
  isQuestion,
  addressed,
  passages,
  evidenceFor,
  firstAnswer,
} from '../public/engine.mjs';
test('Relance après ma prise de parole et question explicitement adressée à Julie', () => {
  const previous = { channel: 'local', at: performance.now() };
  assert.equal(addressed('Et le délai ?', 'Camille', previous), true);
  assert.equal(
    addressed('Julie, quel est le délai ?', 'Camille', previous),
    false,
  );
  assert.equal(isQuestion('Le budget passe à 80 000 euros.'), false);
});
test('Le devis utilise le plafond annoncé et conserve la référence du dossier', () => {
  const q =
    'Camille, peut-on valider le devis de 90 000 euros avec ce budget ?';
  const history = [
    {
      channel: 'remote',
      time: '0:04',
      text: 'Le nouveau plafond du budget est de 80 000 euros.',
    },
  ];
  const refs = evidenceFor(q, passages([fixture]), history);
  assert.equal(refs.length, 2);
  assert.match(firstAnswer(q, refs), /10.000 €/);
});
test('Prénom composé et mention d’un tiers', () => {
  assert.equal(
    addressed('Jean-Pierre, quel est le délai ?', 'Jean-Pierre', null),
    true,
  );
  assert.equal(
    addressed('Julie, peux-tu consulter Camille ?', 'Camille', null),
    false,
  );
  assert.equal(
    Boolean(addressed('Quel est le délai selon Camille ?', 'Camille', null)),
    false,
  );
});
