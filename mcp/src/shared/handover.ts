import { compareVersions } from '../version.js';
import { askToStop, probe, verifiedState } from './client.js';
import { SERVICE } from './state.js';

export const HANDOVER_MS = 5_000;
const RETRY_MS = 100;

export interface Listener {
  listen(): Promise<void>;
}

// A newer build takes the port from an older one; the stop request goes only to a holder whose pid and port
// match this user's state file, with that file's own token, so a program squatting on the port gets no credential.
export async function claimPort(options: { front: Listener; port: number; home: string; version: string; log: (message: string) => void; waitMs?: number }): Promise<boolean> {
  const { front, port, home, version, log } = options;
  try {
    await front.listen();
    return true;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'EADDRINUSE') throw e;
  }
  const found = await probe(port);
  if (found.kind !== 'ours') {
    log(found.kind === 'other' ? `port ${port} belongs to another program` : `port ${port} is in use`);
    return false;
  }
  if (compareVersions(version, found.health.version) <= 0) return false;
  const state = await verifiedState(home, port, found.health);
  if (state === undefined) {
    log(`port ${port} answers as ${SERVICE} ${found.health.version} but matches no state file; leaving it`);
    return false;
  }
  log(`asking ${SERVICE} ${found.health.version} (pid ${state.pid}) to hand over port ${port}`);
  await askToStop(port, state.shutdownToken);
  const deadline = Date.now() + (options.waitMs ?? HANDOVER_MS);
  while (Date.now() < deadline) {
    try {
      await front.listen();
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, RETRY_MS));
    }
  }
  log(`${SERVICE} ${found.health.version} did not hand over port ${port}`);
  return false;
}
