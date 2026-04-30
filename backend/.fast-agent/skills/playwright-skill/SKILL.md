---
name: playwright-skill
description: >
  Browser automation and E2E testing with Playwright (Node.js). Handles dev server
  detection, test script execution, responsive viewport testing, and form automation.
  Use when QE needs to test frontend UI, verify user flows, or take screenshots.
---

# Playwright Browser Automation

Write custom Playwright scripts, execute via universal executor. Scripts go to `/tmp` (works on macOS/Linux/Docker — all Jarvis target environments). Never write to skill or project directory.

## Setup (First Time Only)

```bash
cd $SKILL_DIR && npm run setup
```

## Critical Workflow

1. **Detect dev servers** (for localhost testing):
   ```bash
   cd $SKILL_DIR && node -e "require('./lib/helpers').detectDevServers().then(s => console.log(JSON.stringify(s)))"
   ```
   - 1 server → use it automatically
   - Multiple → ask user which one
   - None → ask for URL or help start server

2. **Write script to /tmp** — NEVER to skill directory or project:
   ```javascript
   // /tmp/playwright-test-xxx.js
   const { chromium } = require('playwright');
   const TARGET_URL = 'http://localhost:3001'; // Auto-detected
   (async () => {
     const browser = await chromium.launch({ headless: false });
     const page = await browser.newPage();
     await page.goto(TARGET_URL);
     console.log('Page loaded:', await page.title());
     await page.screenshot({ path: '/tmp/screenshot.png', fullPage: true });
     await browser.close();
   })();
   ```

3. **Execute from skill directory**:
   ```bash
   cd $SKILL_DIR && node run.js /tmp/playwright-test-xxx.js
   ```

## Defaults

| Setting | Default | Override |
|---------|---------|----------|
| Browser | Chromium | `headless: false` by default |
| Viewport | 1280×720 | `page.setViewportSize(...)` |
| Scripts | `/tmp/playwright-test-*.js` | Never write to project dir |
| URLs | Auto-detected via `detectDevServers()` | `TARGET_URL` constant at top |

## Anti-Patterns (from browser-automation best practices)

| ❌ Don't | ✅ Do |
|----------|-------|
| `waitForTimeout(5000)` | `waitForSelector('.element')` or `waitForURL(...)` |
| `page.locator('.btn-primary')` | `page.getByRole('button', { name: 'Submit' })` |
| Single browser context for all tests | Fresh context per test |
| CSS/XPath first | User-facing locators: `text=`, `role=`, `data-testid=` |

## Available Helpers (`lib/helpers.js`)

```javascript
const helpers = require('./lib/helpers');

await helpers.detectDevServers();           // Find running servers
await helpers.safeClick(page, 'button', { retries: 3 });
await helpers.safeType(page, '#input', 'text');
await helpers.takeScreenshot(page, 'name');
await helpers.handleCookieBanner(page);
await helpers.extractTableData(page, 'table');
const ctx = await helpers.createContext(browser); // With env headers
```

## Inline Execution (Quick Tasks)

```bash
cd $SKILL_DIR && node run.js "
const browser = await chromium.launch({ headless: false });
const page = await browser.newPage();
await page.goto('http://localhost:3001');
await page.screenshot({ path: '/tmp/quick.png', fullPage: true });
await browser.close();
"
```

Use **inline** for one-off tasks. Use **files** for complex/reusable tests.

## Custom HTTP Headers

```bash
PW_HEADER_NAME=X-Automated-By PW_HEADER_VALUE=playwright-skill \
  cd $SKILL_DIR && node run.js /tmp/script.js
```

## Reference Files

- [API_REFERENCE.md](API_REFERENCE.md) — Full Playwright API: selectors, network interception, auth, visual testing, mobile emulation, POM pattern, CI/CD integration
