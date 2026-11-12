import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, join } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const bin = join(process.env.CARGO_HOME || join(homedir(), '.cargo'), 'bin');
const env = { ...process.env };
if (existsSync(join(bin, 'cargo.exe')))
  env.Path = bin + delimiter + process.env.Path;
const cli = require.resolve('@tauri-apps/cli/tauri.js');
const proc = spawn(process.execPath, [cli, ...process.argv.slice(2)], {
  stdio: 'inherit',
  env,
});
proc.on('error', (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
proc.on('exit', (code) => {
  process.exitCode = code ?? 1;
});
