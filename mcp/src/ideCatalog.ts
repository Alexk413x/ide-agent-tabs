export type IdeKind = 'vscode' | 'jetbrains';

export interface IdeEntry {
  key: string;
  name: string;
  kind: IdeKind;
  aliases: readonly string[];
  product: RegExp;
  cli?: string;
  productCodes?: readonly string[];
}

const vscode = (key: string, name: string, cli: string, product: RegExp, aliases: string[] = []): IdeEntry => ({ key, name, kind: 'vscode', cli, product, aliases });
const jetbrains = (key: string, name: string, codes: string[], aliases: string[] = []): IdeEntry => ({
  key,
  name,
  kind: 'jetbrains',
  product: new RegExp(`^${name}\\b`, 'i'),
  productCodes: codes,
  aliases,
});

export const IDE_CATALOG: readonly IdeEntry[] = [
  vscode('vscode', 'VS Code', 'code', /^visual studio code$/i, ['code', 'visual-studio-code']),
  vscode('code-insiders', 'VS Code Insiders', 'code-insiders', /^visual studio code - insiders$/i, ['vscode-insiders', 'insiders', 'visual-studio-code-insiders']),
  vscode('cursor', 'Cursor', 'cursor', /^cursor$/i),
  vscode('windsurf', 'Windsurf', 'windsurf', /^windsurf\b/i),
  vscode('vscodium', 'VSCodium', 'codium', /^vscodium\b/i, ['codium']),
  vscode('antigravity', 'Antigravity', 'antigravity-ide', /^antigravity\b/i, ['antigravity-ide']),
  vscode('kiro', 'Kiro', 'kiro', /^kiro\b/i),
  vscode('positron', 'Positron', 'positron', /^positron\b/i),
  vscode('trae', 'Trae', 'trae', /^trae\b/i),
  jetbrains('android-studio', 'Android Studio', ['AI'], ['studio', 'android']),
  jetbrains('idea', 'IntelliJ IDEA', ['IU', 'IC', 'IE', 'II'], ['intellij', 'intellij-idea-ultimate', 'intellij-idea-community', 'idea-ultimate', 'idea-community']),
  jetbrains('pycharm', 'PyCharm', ['PY', 'PC', 'PE']),
  jetbrains('webstorm', 'WebStorm', ['WS']),
  jetbrains('goland', 'GoLand', ['GO']),
  jetbrains('rider', 'Rider', ['RD']),
  jetbrains('clion', 'CLion', ['CL']),
  jetbrains('rustrover', 'RustRover', ['RR']),
  jetbrains('phpstorm', 'PhpStorm', ['PS']),
  jetbrains('rubymine', 'RubyMine', ['RM']),
  jetbrains('datagrip', 'DataGrip', ['DB']),
  jetbrains('dataspell', 'DataSpell', ['DS']),
];

export const normalizeIdeName = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, '');

export function findIdeEntry(name: string): IdeEntry | undefined {
  const wanted = normalizeIdeName(name);
  if (wanted === '') return undefined;
  return (
    IDE_CATALOG.find((e) => [e.key, e.name, ...e.aliases].some((n) => normalizeIdeName(n) === wanted)) ??
    IDE_CATALOG.find((e) => e.product.test(name.trim()))
  );
}

export function entryForProduct(product: string): IdeEntry | undefined {
  return IDE_CATALOG.find((e) => e.product.test(product.trim()));
}

export function productMatchesName(product: string, name: string): boolean {
  const entry = findIdeEntry(name);
  if (entry) return entry.product.test(product.trim());
  return normalizeIdeName(product) !== '' && normalizeIdeName(product) === normalizeIdeName(name);
}

export function entryForProductInfo(name: string | undefined, productCode: string | undefined): IdeEntry | undefined {
  return (
    (name !== undefined ? entryForProduct(name) : undefined) ??
    (productCode !== undefined ? IDE_CATALOG.find((e) => e.productCodes?.includes(productCode.toUpperCase())) : undefined)
  );
}
