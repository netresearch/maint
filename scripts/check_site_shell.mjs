/** Integration checks for the vendored header against each site's real CSS. */
async function state(page) {
  return page.evaluate(() => ({
    preference: document.documentElement.dataset.themePreference,
    effective: document.documentElement.dataset.theme,
    dark: document.documentElement.classList.contains('dark'),
    checked: document.querySelector('input[name="nr-shell-theme"]:checked')?.value,
    background: getComputedStyle(document.body).backgroundColor,
  }));
}

async function choose(page, value) {
  if (!(await page.$eval('[data-nr-theme-picker]', (picker) => picker.open))) {
    await page.click('[data-nr-theme-picker] > summary');
  }
  await page.click(`input[name="nr-shell-theme"][value="${value}"]`);
}

async function menuChecks(page, issues) {
  await page.setViewport({ width: 320, height: 240 });
  const summary = '[data-nr-mobile-menu] > summary';
  await page.focus(summary);
  await page.keyboard.press('Enter');
  if (!(await page.$eval('[data-nr-mobile-menu]', (menu) => menu.open))) issues.push('Native mobile disclosure did not open with Enter');
  await page.focus('[data-nr-mobile-menu] nav a:last-child');
  const bounds = await page.$eval('[data-nr-mobile-menu] nav a:last-child', (link) => ({ top: link.getBoundingClientRect().top, bottom: link.getBoundingClientRect().bottom }));
  if (bounds.top < 0 || bounds.bottom > 240) issues.push('Last mobile-menu link cannot be focused into a short viewport');
  await page.keyboard.press('Escape');
  if (!(await page.$eval('[data-nr-mobile-menu]', (menu) => !menu.open && document.activeElement === menu.querySelector('summary')))) issues.push('Escape did not close mobile menu and return focus');
  await page.click('[data-nr-theme-picker] > summary');
  await page.focus('input[name="nr-shell-theme"][value="dark"]');
  const option = await page.$eval('input[name="nr-shell-theme"][value="dark"]', (radio) => ({ top: radio.getBoundingClientRect().top, bottom: radio.getBoundingClientRect().bottom }));
  if (option.top < 0 || option.bottom > 240) issues.push('Last appearance option cannot be focused fully into a short viewport');
  await page.keyboard.press('Escape');
}

async function noScriptChecks(page, issues) {
  await page.setJavaScriptEnabled(false);
  await page.reload({ waitUntil: 'networkidle0' });
  if (!(await page.$eval('[data-nr-theme-picker]', (picker) => getComputedStyle(picker).display === 'none'))) issues.push('NoJS exposes a nonfunctional appearance picker');
  await page.focus('[data-nr-mobile-menu] > summary');
  await page.keyboard.press('Enter');
  if (!(await page.$eval('[data-nr-mobile-menu]', (menu) => menu.open && menu.querySelectorAll('nav a').length === 5))) issues.push('NoJS native menu did not expose all global destinations');
  const localeBounds = await page.$$eval('.nr-shell-locales a', (links) => links.map((link) => ({ width: link.getBoundingClientRect().width, height: link.getBoundingClientRect().height })));
  if (localeBounds.some((rect) => rect.width < 48 || rect.height < 48)) issues.push('NoJS locale target is smaller than 48px');
  if ((await page.$eval('html', (root) => root.scrollWidth)) > 320) issues.push('NoJS header causes horizontal document overflow');
  await page.setJavaScriptEnabled(true);
  await page.reload({ waitUntil: 'networkidle0' });
}

async function headerTextChecks(page, issues) {
  await page.evaluate(() => {
    const sizes = [...document.querySelectorAll('[data-nr-shell], [data-nr-shell] *')]
      .map((element) => [element, Number.parseFloat(getComputedStyle(element).fontSize)]);
    for (const [element, size] of sizes) element.style.fontSize = `${size * 2}px`;
  });
  await page.click('[data-nr-theme-picker] > summary');
  await page.focus('input[name="nr-shell-theme"][value="dark"]');
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const enlarged = await page.evaluate(() => {
    const radio = document.querySelector('input[name="nr-shell-theme"][value="dark"]');
    const rect = radio.getBoundingClientRect();
    return document.documentElement.scrollWidth <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
  });
  if (!enlarged) issues.push('200% header text clips appearance choices or overflows the document');
  await page.reload({ waitUntil: 'networkidle0' });
  await page.addStyleTag({ content: '[data-nr-shell] *{line-height:1.5!important;letter-spacing:.12em!important;word-spacing:.16em!important}' });
  await page.click('[data-nr-theme-picker] > summary');
  await page.focus('input[name="nr-shell-theme"][value="dark"]');
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const spaced = await page.evaluate(() => {
    const rect = document.querySelector('input[name="nr-shell-theme"][value="dark"]').getBoundingClientRect();
    return document.documentElement.scrollWidth <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
  });
  if (!spaced) issues.push('WCAG text spacing clips appearance choices or overflows the document');
  await page.reload({ waitUntil: 'networkidle0' });
}

async function blockedStorageChecks(page, issues, scheme) {
  const context = await page.browser().createBrowserContext();
  try {
    const blocked = await context.newPage();
    const errors = [];
    blocked.on('pageerror', (error) => errors.push(error.message));
    await blocked.setViewport({ width: 320, height: 900 });
    await blocked.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
    await blocked.evaluateOnNewDocument(() => Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Blocked', 'SecurityError'); } }));
    await blocked.goto(page.url(), { waitUntil: 'networkidle0' });
    await choose(blocked, 'dark');
    if ((await state(blocked)).effective !== 'dark' || errors.length) issues.push('Blocked storage prevents an in-memory appearance choice');
  } finally {
    await context.close();
  }
}

async function initialChecks(page, scheme, viewport, issues) {
  await page.focus('.skip-link');
  const skip = await page.$eval('.skip-link', (link) => {
    const rect = link.getBoundingClientRect();
    return rect.top >= 0 && rect.bottom <= innerHeight && document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)?.closest('.skip-link') === link;
  });
  if (!skip) issues.push('Focused skip link is obscured by the shared header');
  const first = await state(page);
  if (first.preference !== 'system' || first.effective !== scheme || first.dark !== (scheme === 'dark')) issues.push('System preference does not materialize the effective data-theme and dark class');
  const height = await page.$eval('[data-nr-shell]', (header) => header.getBoundingClientRect().height);
  if (height !== (viewport.width < 1024 ? 64 : 72)) issues.push(`Shared bar height is ${height}px`);
  return first;
}

async function keyboardChecks(page, issues) {
  await page.click('[data-nr-theme-picker] > summary');
  await page.focus('input[name="nr-shell-theme"][value="system"]');
  await page.keyboard.press('ArrowRight');
  if ((await state(page)).preference !== 'light') issues.push('Radio ArrowRight did not select Light');
  await page.keyboard.press('ArrowRight');
  if ((await state(page)).preference !== 'dark') issues.push('Radio ArrowRight did not select Dark');
  await page.keyboard.press('Escape');
  if (!(await page.$eval('[data-nr-theme-picker]', (picker) => !picker.open && document.activeElement === picker.querySelector('summary')))) issues.push('Appearance Escape did not restore trigger focus');
}

async function preferenceChecks(page, scheme, first, issues, onThemeChange) {
  const opposite = scheme === 'dark' ? 'light' : 'dark';
  await choose(page, opposite);
  const changed = await state(page);
  if (changed.background === first.background || changed.effective !== opposite) issues.push('Explicit radio selection did not change the actual site palette');
  if (onThemeChange) issues.push(...(await onThemeChange(page)));
  await page.reload({ waitUntil: 'networkidle0' });
  if ((await state(page)).checked !== opposite) issues.push('Radio preference did not survive reload');
  await page.evaluate(() => localStorage.setItem('nr-color-theme', 'dark'));
  await choose(page, 'system');
  await page.reload({ waitUntil: 'networkidle0' });
  const reset = await state(page);
  if (reset.background !== first.background || reset.preference !== 'system' || reset.checked !== 'system') issues.push('Explicit System reset was overridden by legacy Dark');
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: opposite }]);
  await page.waitForFunction((value) => document.documentElement.dataset.theme === value, {}, opposite);
  if ((await state(page)).background !== changed.background) issues.push('System preference does not follow OS changes');
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
  await page.evaluate(() => localStorage.removeItem('nr-theme'));
  await page.reload({ waitUntil: 'networkidle0' });
  if ((await state(page)).checked !== 'dark') issues.push('Legacy preference was not recognised when the canonical key was absent');
  await page.evaluate(() => { localStorage.removeItem('nr-color-theme'); window.nrSiteTheme.setPreference('system'); });
  await page.reload({ waitUntil: 'networkidle0' });
}

export async function checkSiteShell(page, scheme, onThemeChange) {
  const issues = [];
  const viewport = page.viewport();
  const first = await initialChecks(page, scheme, viewport, issues);
  await keyboardChecks(page, issues);
  await preferenceChecks(page, scheme, first, issues, onThemeChange);
  if (viewport.width === 320) {
    await menuChecks(page, issues);
    if (scheme === 'light') {
      await headerTextChecks(page, issues);
      await noScriptChecks(page, issues);
      await blockedStorageChecks(page, issues, scheme);
    }
    await page.setViewport(viewport);
  }
  return issues;
}
