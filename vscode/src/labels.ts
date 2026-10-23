export const VIA_LABEL = 'via OpenRouter';

export const commandKey = (name: string) => name.replace(/-(\w)/g, (_, c: string) => c.toUpperCase());

export const escapeMarkdown = (text: string) => text.replace(/[\\`*_{}[\]()#+\-.!|<>]/g, '\\$&');

const iconHtml = (src: string) => `<img src="${src}" width="16" height="16" align="absmiddle">`;

export function agentLink(name: string, label: string, logo: string | undefined, viaLogo: string | undefined): string {
  const args = encodeURIComponent(JSON.stringify([name]));
  const img = logo ? `${iconHtml(logo)} ` : '';
  const via = viaLogo ? ` ${iconHtml(viaLogo)} ${VIA_LABEL}` : '';
  return `${img}[${escapeMarkdown(label)}](command:ideAgentTabs.openAgent?${args})${via}`;
}

export function statusTooltip(links: readonly string[], missing: readonly string[], mac: boolean): string {
  const lines = [
    links.length > 0 ? `Open an agent in an editor tab.  \nNew tab: **${mac ? '⌘⌥A' : 'Ctrl+Alt+A'}**` : 'No agent CLI found on PATH.',
    links.join('  \n'),
    missing.length > 0 ? `Not installed: ${missing.map(escapeMarkdown).join(', ')}` : '',
    '[$(gear) Settings](command:ideAgentTabs.openSettings)',
  ];
  return lines.filter(Boolean).join('\n\n');
}
