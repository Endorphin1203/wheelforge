import { expect, test, type Page } from '@playwright/test'

import { installMockApi } from './fixtures/mockApi'

async function login(page: Page, screenshotPath?: string): Promise<void> {
  await page.goto('/#/login')
  await expect(page.getByRole('heading', { name: 'WheelForge' })).toBeVisible()
  if (screenshotPath) await page.screenshot({ path: screenshotPath, fullPage: true })
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('Admin-Password-123!')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/#\/build\/new$/)
}

async function assertNoOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }))
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.width)
}

test.beforeEach(async ({ page }) => installMockApi(page))

test('desktop build, result, artifacts, and administrator workflow', async ({ page }, testInfo) => {
  await login(page, testInfo.outputPath('desktop-login.png'))
  await expect(page.getByRole('heading', { name: '新建离线依赖构建' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-build-workspace.png'), fullPage: true })
  await page.locator('input[type="file"]').setInputFiles({ name: 'requirements.txt', mimeType: 'text/plain', buffer: Buffer.from('numpy==2.0.0\nfastapi==0.115.0\n') })
  await expect(page.getByText('numpy==2.0.0')).toBeVisible()
  await page.getByRole('button', { name: '开始构建' }).click()
  await expect(page).toHaveURL(/#\/tasks\/task-1$/)
  await expect(page.getByText('部分成功').first()).toBeVisible()
  await expect(page.getByText('静态兼容性校验，不执行目标环境安装，不能证明目标机器一定安装成功。')).toBeVisible()
  await page.getByRole('tab', { name: '版本对比' }).click()
  await expect(page.getByText('降级')).toBeVisible()
  await expect(page.getByText('升级')).toBeVisible()
  await assertNoOverflow(page)
  await page.screenshot({ path: testInfo.outputPath('desktop-task-detail.png'), fullPage: true })

  await page.getByRole('button', { name: '产物管理' }).click()
  await expect(page.getByText('wheelforge-linux-arm64-py311.zip')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-artifacts.png'), fullPage: true })
  await page.getByRole('button', { name: '下载源' }).click()
  await expect(page.getByText('清华镜像')).toBeVisible()
  await expect(page.getByText('https://pypi.tuna.tsinghua.edu.cn/simple')).toBeVisible()
  await assertNoOverflow(page)
  const screenshot = await page.screenshot({ path: testInfo.outputPath('desktop-admin-sources.png'), fullPage: true })
  expect(screenshot.byteLength).toBeGreaterThan(10_000)
})

test('mobile navigation and build workspace remain usable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await login(page)
  await expect(page.getByRole('button', { name: '打开导航' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('mobile-build-workspace.png'), fullPage: true })
  await page.getByRole('button', { name: '打开导航' }).click()
  const mobileNavigation = page.getByRole('navigation', { name: '移动端主导航' })
  await expect(mobileNavigation).toBeVisible()
  await page.getByRole('button', { name: '构建任务' }).last().click()
  await expect(page.getByRole('heading', { name: '构建任务' })).toBeVisible()
  await expect(mobileNavigation).toBeHidden()
  await assertNoOverflow(page)
  const screenshot = await page.screenshot({ path: testInfo.outputPath('mobile-tasks.png'), fullPage: true })
  expect(screenshot.byteLength).toBeGreaterThan(8_000)
})
