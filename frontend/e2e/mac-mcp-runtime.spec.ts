import { expect, test } from '@playwright/test'

test.skip(
  process.env.E2E_MAC_RUNTIME !== '1',
  'requires the live Mac FastAPI composition on 127.0.0.1:8000',
)

test('live Mac workspace reports the actually mounted MCP endpoint', async ({ page }) => {
  await page.goto('/workspace/mcp')

  await expect(page.getByRole('heading', { name: 'MCP 生态' })).toBeVisible()
  await expect(page.getByText('当前已挂载', { exact: true })).toBeVisible()
  await expect(page.getByText('http://127.0.0.1:8000/mcp', { exact: true })).toBeVisible()
  await expect(page.getByText('0 个当前端点已挂载')).not.toBeVisible()
})
