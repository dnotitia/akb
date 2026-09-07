import '@testing-library/jest-dom/vitest'

const rect = {
  bottom: 0,
  height: 0,
  left: 0,
  right: 0,
  top: 0,
  width: 0,
  x: 0,
  y: 0,
  toJSON: () => ({}),
} as DOMRect

Object.defineProperty(document, 'elementFromPoint', {
  configurable: true,
  value: () => document.body,
})
HTMLElement.prototype.getBoundingClientRect = () => rect
HTMLElement.prototype.getClientRects = () => [rect] as unknown as DOMRectList
Range.prototype.getBoundingClientRect = () => rect
Range.prototype.getClientRects = () => [rect] as unknown as DOMRectList
