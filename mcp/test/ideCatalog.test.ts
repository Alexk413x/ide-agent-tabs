import assert from 'node:assert/strict';
import { test } from 'node:test';
import { entryForProduct, entryForProductInfo, findIdeEntry, IDE_CATALOG, productMatchesName } from '../src/ideCatalog.js';

test('an IDE name matches its key, product name or alias, ignoring case, spaces and punctuation', () => {
  const key = (name: string) => findIdeEntry(name)?.key;
  for (const name of ['Android Studio', 'android-studio', 'STUDIO', 'android studio', 'android']) assert.equal(key(name), 'android-studio', name);
  for (const name of ['IntelliJ IDEA', 'idea', 'intellij', 'IntelliJ IDEA Ultimate', 'intellij-idea-community']) assert.equal(key(name), 'idea', name);
  for (const name of ['VS Code', 'vscode', 'code', 'Visual Studio Code']) assert.equal(key(name), 'vscode', name);
  assert.equal(key('Visual Studio Code - Insiders'), 'code-insiders');
  assert.equal(key('code-insiders'), 'code-insiders');
  assert.equal(key('codium'), 'vscodium');
  assert.equal(key('VSCodium'), 'vscodium');
  assert.equal(key('PyCharm Community Edition'), 'pycharm');
  assert.equal(key('antigravity-ide'), 'antigravity');
  for (const name of ['webstorm', 'goland', 'rider', 'clion', 'rustrover', 'phpstorm', 'rubymine', 'cursor', 'windsurf', 'kiro', 'positron', 'trae']) {
    assert.equal(key(name), name);
  }
  assert.equal(key('notepad'), undefined);
  assert.equal(key(''), undefined);
  assert.equal(key('jetbrains-12345'), undefined);
});

test('a running product maps to its entry without confusing VS Code with its Insiders build', () => {
  assert.equal(entryForProduct('Visual Studio Code')?.key, 'vscode');
  assert.equal(entryForProduct('Visual Studio Code - Insiders')?.key, 'code-insiders');
  assert.equal(entryForProduct('IntelliJ IDEA Ultimate')?.key, 'idea');
  assert.equal(entryForProduct('Android Studio')?.key, 'android-studio');
  assert.equal(entryForProduct('Fake Studio'), undefined);
  assert.ok(productMatchesName('Android Studio', 'studio'));
  assert.ok(!productMatchesName('Visual Studio Code - Insiders', 'vscode'));
  assert.ok(productMatchesName('Fake Studio', 'fake studio'), 'a product outside the catalog matches its own name');
  assert.ok(!productMatchesName('Fake Studio', 'studio'));
});

test('product-info.json maps by name, else by product code', () => {
  assert.equal(entryForProductInfo('PyCharm', 'PY')?.key, 'pycharm');
  assert.equal(entryForProductInfo('Something New', 'ai')?.key, 'android-studio');
  assert.equal(entryForProductInfo('JetBrains Gateway', 'GW'), undefined);
  assert.equal(new Set(IDE_CATALOG.map((e) => e.key)).size, IDE_CATALOG.length);
});
