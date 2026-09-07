import { expect, test } from '@playwright/test'

test('supports Korean text input, selection, paste, undo, and slash keyboard input', async ({ page }) => {
  await page.goto('/browser/')

  const editor = page.locator('[data-markdown-surface="editor"] .ProseMirror')
  await expect(editor).toBeVisible()

  await editor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.insertText(' 한글')
  await expect(page.getByTestId('markdown-output')).toContainText('한글')

  await editor.selectText()
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toContain('시작')

  await editor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.evaluate(() => {
    const target = document.querySelector('[data-markdown-surface="editor"] .ProseMirror')
    const data = new DataTransfer()
    data.setData('text/plain', ' 붙여넣기')
    target?.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData: data }))
  })
  await expect(page.getByTestId('markdown-output')).toContainText('붙여넣기')

  await page.keyboard.press('ControlOrMeta+Z')
  await expect(page.getByTestId('markdown-output')).not.toContainText('붙여넣기')

  await editor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('/heading')
  await expect(page.getByTestId('slash-count')).toHaveText('slash:1')
  await expect(page.getByTestId('markdown-output')).toContainText('/heading')
})

test('renders viewer and editor from the same semantic document', async ({ page }) => {
  await page.goto('/browser/')

  const editorHTML = await page.locator('[data-markdown-surface="editor"] .ProseMirror').innerHTML()
  const viewerHTML = await page.locator('[data-markdown-surface="viewer"] .ProseMirror').innerHTML()

  expect(viewerHTML).toBe(editorHTML)
  await expect(page.locator('[data-markdown-surface="viewer"] .ProseMirror')).not.toHaveAttribute(
    'contenteditable',
    'true',
  )
})

test('composition events do not lose Korean text', async ({ page }) => {
  await page.goto('/browser/')

  const editor = page.locator('[data-markdown-surface="editor"] .ProseMirror')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.evaluate(() => {
    const target = document.querySelector('[data-markdown-surface="editor"] .ProseMirror')
    if (!target) return
    target.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }))
  })
  await page.keyboard.insertText('조합')
  await page.evaluate(() => {
    const target = document.querySelector('[data-markdown-surface="editor"] .ProseMirror')
    target?.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '조합' }))
  })

  await expect(page.getByTestId('markdown-output')).toContainText('조합')
})
