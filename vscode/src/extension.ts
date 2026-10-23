import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { registerCommands } from './commands';
import { createExt, Ext, fileFolders } from './ext';
import { ENDPOINT_BEAT_MS, beatEndpoint, endpointFileName, endpointJson, newToken, newWindowId, writeAtomically } from './registry';
import { apiUrl, createApiServer, listen } from './server';
import { CONFIG_FILE } from './sharedSettings';
import { followShared, followSharedDefault, followSharedSettings, settingsChanged } from './sharedSync';
import { createStatus, refreshStatus } from './status';
import { adopt, forget, openOnStartup, tabHost } from './tabs';

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const ext = createExt(context);
  const status = createStatus();
  const refresh = () => refreshStatus(ext, status);
  const follow = () => followShared(ext.sync);
  refresh();
  status.show();

  context.subscriptions.push(
    ext.log,
    status,
    ...registerCommands(ext),
    vscode.window.onDidOpenTerminal(terminal => adopt(ext, [terminal])),
    vscode.window.onDidCloseTerminal(terminal => forget(ext, terminal)),
    vscode.window.onDidChangeActiveColorTheme(() => refresh()),
    vscode.window.onDidChangeWindowState(state => {
      if (!state.focused) return;
      follow();
      refresh();
    }),
    vscode.workspace.onDidChangeConfiguration(e => {
      if (settingsChanged(ext.sync, section => e.affectsConfiguration(section))) refresh();
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(e => openOnStartup(ext, e.added)),
  );

  await followSharedDefault(ext.sync);
  await followSharedSettings(ext.sync);
  const sharedConfig = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(ext.home), CONFIG_FILE));
  context.subscriptions.push(sharedConfig, sharedConfig.onDidChange(follow), sharedConfig.onDidCreate(follow));
  refresh();
  const revived = adopt(ext, vscode.window.terminals);
  if (revived > 0) ext.log.info(`Found ${revived} agent tab${revived === 1 ? '' : 's'} from before the extension host restarted`);
  else openOnStartup(ext, fileFolders());

  await serveApi(ext);
}

async function serveApi(ext: Ext): Promise<void> {
  const token = newToken();
  const server = createApiServer(token, tabHost(ext), ext.settings, message => ext.log.warn(message));
  ext.context.subscriptions.push({ dispose: () => server.close() });
  let port: number;
  try {
    port = await listen(server);
  } catch (e) {
    ext.log.error(`Could not start the agent tab server: ${(e as Error).message}`);
    return;
  }
  registerEndpoint(ext, port, token);
}

function registerEndpoint(ext: Ext, port: number, token: string): void {
  const { log } = ext;
  const file = path.join(ext.home, 'endpoints', endpointFileName(process.pid, newWindowId()));
  try {
    const content = endpointJson({
      product: vscode.env.appName,
      version: vscode.version,
      pid: process.pid,
      url: apiUrl(port),
      token,
      startedAt: Date.now(),
      beatMs: ENDPOINT_BEAT_MS,
    });
    writeAtomically(file, content, true);
    const beat = setInterval(() => {
      try {
        beatEndpoint(file, content);
      } catch (e) {
        log.error(`Could not refresh ${file}: ${(e as Error).message}`);
      }
    }, ENDPOINT_BEAT_MS);
    ext.context.subscriptions.push({
      dispose: () => {
        clearInterval(beat);
        fs.rmSync(file, { force: true });
      },
    });
    log.info(`Agent tab server at ${apiUrl(port)}, registered in ${file}`);
  } catch (e) {
    log.error(`Could not write ${file}: ${(e as Error).message}`);
  }
}

export function deactivate(): void {}
