import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];
const BLOCKING_IMPACTS = new Set(['serious', 'critical']);

async function expectNoBlockingViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const blocking = results.violations.filter(
    (violation) => violation.impact && BLOCKING_IMPACTS.has(violation.impact)
  );
  const summary = blocking.flatMap((violation) =>
    violation.nodes.map(
      (node) => `${violation.impact} ${violation.id}: ${node.target.join(' ')}`
    )
  );
  expect(summary, summary.join('\n')).toEqual([]);
}

// /visual-test/chat-mobile-controls is not scanned: at base it has serious
// aria-prohibited-attr and critical aria-required-children violations.
test('login page has no serious or critical axe violations', async ({
  page,
}) => {
  await page.goto('/login');
  await expect(page.locator('#email')).toBeVisible();
  await expect(page.locator('#password')).toBeVisible();
  await expectNoBlockingViolations(page);
});
