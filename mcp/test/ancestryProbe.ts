import { findAgentProcess } from '../src/shared/ancestry.js';

process.stdout.write(`${JSON.stringify((await findAgentProcess(process.pid, 60_000)) ?? null)}\n`);
