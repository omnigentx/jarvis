# Playwright Skill - Complete API Reference

For quick-start execution patterns, see [SKILL.md](SKILL.md).

## Selectors & Locators

### Priority Order (most stable → least stable)

```javascript
// 1. Data attributes (BEST — most stable)
await page.locator('[data-testid="submit-button"]').click();

// 2. Role-based (accessible + stable)
await page.getByRole('button', { name: 'Submit' }).click();
await page.getByRole('textbox', { name: 'Email' }).fill('user@example.com');

// 3. Text content (for unique text)
await page.getByText('Sign in').click();
await page.getByText(/welcome back/i).click();

// 4. Semantic HTML (OK)
await page.locator('button[type="submit"]').click();

// 5. CSS classes (AVOID — fragile)
await page.locator('.btn-primary').click();
```

### Advanced Patterns

```javascript
// Filter and chain
const row = page.locator('tr').filter({ hasText: 'John Doe' });
await row.locator('button').click();

// Nth element
await page.locator('button').nth(2).click();

// Combining conditions
await page.locator('button').and(page.locator('[disabled]')).count();
```

## Common Actions

### Form Interactions

```javascript
// Text input
await page.getByLabel('Email').fill('user@example.com');
await page.locator('#username').clear();
await page.locator('#username').type('newuser', { delay: 100 });

// Checkbox / Radio
await page.getByLabel('I agree').check();
await page.getByLabel('Option 2').check();

// Select dropdown
await page.selectOption('select#country', 'usa');
await page.selectOption('select#country', { label: 'United States' });

// Multi-select
await page.selectOption('select#colors', ['red', 'blue', 'green']);

// File upload
await page.setInputFiles('input[type="file"]', 'path/to/file.pdf');
```

### Mouse & Keyboard

```javascript
// Click variations
await page.click('button', { button: 'right' });  // Right click
await page.dblclick('button');                      // Double click
await page.hover('.menu-item');                     // Hover
await page.dragAndDrop('#source', '#target');       // Drag & drop

// Keyboard
await page.keyboard.press('Control+A');
await page.keyboard.press('Enter');
await page.keyboard.type('Hello', { delay: 100 });
```

## Waiting Strategies

```javascript
// Element states
await page.locator('button').waitFor({ state: 'visible' });
await page.locator('.spinner').waitFor({ state: 'hidden' });

// URL changes
await page.waitForURL('**/success');

// Network
await page.waitForLoadState('networkidle');

// Response
const responsePromise = page.waitForResponse('**/api/users');
await page.click('button#load');
const response = await responsePromise;

// Custom condition
await page.waitForFunction(() => document.querySelector('.loaded'));
```

## Assertions

```javascript
import { expect } from '@playwright/test';

// Page
await expect(page).toHaveTitle('My App');
await expect(page).toHaveURL(/.*dashboard/);

// Elements
await expect(page.locator('h1')).toHaveText('Welcome');
await expect(page.locator('.message')).toContainText('success');
await expect(page.locator('button')).toBeEnabled();
await expect(page.locator('.items')).toHaveCount(5);
await expect(page.locator('input')).toHaveValue('test@example.com');
```

## Network Interception

```javascript
// Mock API responses
await page.route('**/api/users', route => {
  route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{ id: 1, name: 'John' }])
  });
});

// Modify request headers
await page.route('**/api/**', route => {
  const headers = { ...route.request().headers(), 'X-Custom': 'value' };
  route.continue({ headers });
});

// Block resources
await page.route('**/*.{png,jpg,gif}', route => route.abort());
```

## Page Object Model

```javascript
class LoginPage {
  constructor(page) {
    this.page = page;
    this.usernameInput = page.locator('input[name="username"]');
    this.passwordInput = page.locator('input[name="password"]');
    this.submitButton = page.locator('button[type="submit"]');
  }
  async login(username, password) {
    await this.usernameInput.fill(username);
    await this.passwordInput.fill(password);
    await this.submitButton.click();
  }
}
```

## Mobile & Responsive Testing

```javascript
const { devices } = require('playwright');
const iPhone = devices['iPhone 12'];
const context = await browser.newContext({
  ...iPhone,
  locale: 'en-US',
  permissions: ['geolocation'],
  geolocation: { latitude: 37.7749, longitude: -122.4194 }
});
```

## Common Patterns

### Popups
```javascript
const [popup] = await Promise.all([
  page.waitForEvent('popup'),
  page.click('button.open-popup')
]);
await popup.waitForLoadState();
```

### File Downloads
```javascript
const [download] = await Promise.all([
  page.waitForEvent('download'),
  page.click('button.download')
]);
await download.saveAs(`./downloads/${download.suggestedFilename()}`);
```

### iFrames
```javascript
const frame = page.frameLocator('#my-iframe');
await frame.locator('button').click();
```

### Console Logs
```javascript
page.on('console', msg => console.log('Browser log:', msg.text()));
page.on('pageerror', error => console.log('Page error:', error));
```

## Debugging

```bash
# Debug mode with inspector
npx playwright test --debug

# Headed + slow motion
npx playwright test --headed --slowmo=1000

# Code generation
npx playwright codegen https://example.com
```

## Resources
- [Playwright Docs](https://playwright.dev/docs/intro)
- [API Reference](https://playwright.dev/docs/api/class-playwright)
- [Best Practices](https://playwright.dev/docs/best-practices)
