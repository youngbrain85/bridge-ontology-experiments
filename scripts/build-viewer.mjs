import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
await build({
  entryPoints: [path.join(root, 'webapp/viewer-src.js')],
  outfile: path.join(root, 'webapp/static/model-viewer.js'),
  bundle: true,
  minify: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  legalComments: 'inline',
});
console.log('Built webapp/static/model-viewer.js');
