import { z } from 'zod';
import type { Jev } from './service.js';
import { JEV_TOOLS } from './tools.js';

export interface CliIo {
  readStdin: () => Promise<string>;
  stdout: (text: string) => void;
  stderr: (text: string) => void;
}

export const processIo: CliIo = {
  readStdin: async () => {
    let text = '';
    for await (const chunk of process.stdin) text += String(chunk);
    return text;
  },
  stdout: (text) => process.stdout.write(text),
  stderr: (text) => process.stderr.write(text),
};

const SUBCOMMANDS = JEV_TOOLS.map((t) => t.name.replace(/^jev_/, ''));

function parseRequest(text: string): unknown {
  if (text.trim() === '') return {};
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new Error(`stdin is not JSON: ${(e as Error).message}`);
  }
}

export async function runJevCli(args: string[], jev: Jev | undefined, offMessage: string, io: CliIo = processIo): Promise<number> {
  try {
    const tool = JEV_TOOLS.find((t) => t.name === `jev_${args[0]}`);
    if (args.length !== 1 || !tool) {
      throw new Error(`Usage: node mcp-server.mjs jev <${SUBCOMMANDS.join('|')}>, with one JSON request on stdin.`);
    }
    if (!jev) throw new Error(offMessage);
    const parsed = z.object(tool.inputSchema).safeParse(parseRequest(await io.readStdin()));
    if (!parsed.success) throw new Error(z.prettifyError(parsed.error));
    io.stdout(`${JSON.stringify(await tool.run(jev, parsed.data), null, 2)}\n`);
    return 0;
  } catch (e) {
    io.stderr(`${JSON.stringify({ error: e instanceof Error ? e.message : String(e) })}\n`);
    return 1;
  }
}
