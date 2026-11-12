import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const exec = promisify(execFile);
export async function teamsPid() {
  if (process.platform !== 'win32')
    throw Error('Teams Desktop nécessite Windows.');
  const { stdout } = await exec(
    'powershell.exe',
    [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      '(Get-Process -Name ms-teams,Teams -ErrorAction SilentlyContinue | Sort-Object @{Expression={ $_.MainWindowHandle -ne 0 };Descending=$true},StartTime | Select-Object -First 1).Id',
    ],
    { windowsHide: true, timeout: 8000 },
  ).catch((e) => ({ stdout: e.stdout || '' }));
  const pid = Number(stdout.trim());
  if (!Number.isInteger(pid) || pid <= 0)
    throw Error('Ouvre Teams Desktop puis relance l’écoute.');
  return pid;
}
export async function captureProcess(pid, onAudio) {
  const { default: loopback } = await import('loopback-capture');
  const capture = new loopback.LoopbackCapture();
  capture.start(pid, onAudio);
  return () => capture.stop();
}
