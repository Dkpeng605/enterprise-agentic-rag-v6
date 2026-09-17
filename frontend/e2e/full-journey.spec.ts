import { expect, test } from '@playwright/test'

const adminEmail = process.env.E2E_ADMIN_EMAIL ?? 'admin'
const adminPassword = process.env.E2E_ADMIN_PASSWORD ?? 'admin'

test('anonymous full journey and isolated administrator login', async ({ page }) => {
  const suffix = Date.now().toString(36)
  const collectionName = `M7 浏览器验收 ${suffix}`

  await page.goto('/workspace/documents')
  await expect(page.getByRole('heading', { name: '文档与切分' })).toBeVisible()

  await page.getByRole('button', { name: '新建集合' }).click()
  const collectionDialog = page.getByRole('dialog')
  await collectionDialog.getByLabel('集合名称').fill(collectionName)
  await collectionDialog.getByLabel('描述').fill('Playwright M7-08 可回收验收集合')
  await collectionDialog.getByRole('button', { name: '保存集合' }).click()
  await expect(page.getByRole('status')).toHaveText('集合已创建，可立即上传文档。')

  await page.getByRole('button', { name: collectionName, exact: false }).first().click()
  await page.getByRole('button', { name: '上传文档', exact: true }).click()
  const uploadDialog = page.getByRole('dialog')
  await uploadDialog.locator('input[type="file"]').setInputFiles(
    'e2e/fixtures/e2e-knowledge.md',
  )
  await uploadDialog.getByLabel('标题').fill('Atlas 发布手册')
  await uploadDialog.getByLabel('组织').fill('M7 E2E')
  await uploadDialog.getByRole('button', { name: '上传并创建任务' }).click()
  await expect(uploadDialog.getByText('查看处理进度')).toBeVisible()
  await uploadDialog.getByText('查看处理进度').click()
  const selectedJob = page.getByTestId('selected-job')
  await expect(selectedJob.getByText('已完成')).toBeVisible({ timeout: 45_000 })
  await expect(selectedJob).toContainText('100%')

  await page.getByRole('link', { name: '知识问答' }).click()
  const collectionScope = page.getByRole('checkbox', { name: collectionName, exact: false })
  await expect(collectionScope.locator('..')).toContainText('1 个可查询文档')
  await collectionScope.check()
  await page.getByLabel('问题').fill('Atlas 项目的部署验证口令是什么？')
  await page.getByRole('button', { name: '发送', exact: false }).click()
  await expect(page.getByText('已通过证据核验')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.chat-turn--assistant')).toContainText('蓝鲸-7429')
  await expect(page.getByTestId('citations')).toContainText('Atlas 发布手册')
  await expect(page.getByTestId('citations')).toContainText('蓝鲸-7429')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(page.getByRole('checkbox', { name: collectionName, exact: false })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.setViewportSize({ width: 1280, height: 720 })

  await page.getByRole('link', { name: '问答链路观测' }).click()
  await expect(page.getByTestId('waterfall')).toBeVisible()
  await expect(page.getByTestId('rank-table')).toContainText('leaf_')

  await page.getByRole('link', { name: '文档处理观测' }).click()
  await expect(page.getByTestId('ingestion-waterfall')).toBeVisible()
  await expect(page.getByTestId('ingestion-batches')).toBeVisible()

  await page.getByRole('link', { name: 'MCP 能力目录' }).click()
  await expect(page.getByRole('heading', { name: 'MCP 能力目录' })).toBeVisible()
  await expect(page.locator('[aria-labelledby="mcp-tools-title"]')).toContainText(
    'query_knowledge_base',
  )
  await expect(page.locator('[aria-labelledby="mcp-resources-title"]')).toContainText(
    'rag://documents/{document_id}',
  )
  if (process.env.E2E_MAC_RUNTIME === '1') {
    await expect(page.getByText('当前已挂载', { exact: true })).toBeVisible()
    await expect(page.getByText('http://127.0.0.1:8000/mcp', { exact: true })).toBeVisible()
  } else {
    await expect(page.getByText('需要外部组合', { exact: true })).toBeVisible()
  }

  await page.getByRole('link', { name: 'RAG 效果评测' }).click()
  await page.getByLabel('最大 Case').fill('3')
  await page.getByRole('button', { name: '启动评测' }).click()
  await expect(page.getByTestId('evaluation-metrics')).toBeVisible({ timeout: 45_000 })
  await expect(page.getByTestId('evaluation-run-list')).toContainText('已完成')

  await page.getByRole('link', { name: '管理员登录' }).click()
  await page.getByLabel('管理员账号').fill(adminEmail)
  await page.getByLabel('密码').fill(adminPassword)
  await page.getByRole('button', { name: '安全登录' }).click()
  await expect(page).toHaveURL(/\/admin\/providers$/)
  await expect(page.getByRole('heading', { name: '模型选配与索引' })).toBeVisible()
  await expect(page.getByText('管理员', { exact: true })).toBeVisible()
})
