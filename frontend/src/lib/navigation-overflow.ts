/** Keep the current destination visible without wrapping or reordering links. */
export function visibleNavigationItems(widths: number[], available: number, moreWidth: number, gap: number, active = -1): number[] {
  const all = widths.map((_, index) => index);
  // Layout-less environments retain ordinary links; the browser measures before paint.
  if (available <= 0 || widths.some(width => width <= 0)) return all;
  const used = (indices: number[], overflow: boolean) => indices.reduce((sum, index) => sum + widths[index], 0)
    + (overflow ? moreWidth : 0) + Math.max(0, indices.length - (overflow ? 0 : 1)) * gap;
  if (used(all, false) <= available) return all;

  const visible = active > 0 ? [0, active] : [0];
  // At very narrow widths, the current location takes priority over Overview.
  if (visible.length > 1 && used(visible, true) > available) visible.shift();
  for (const index of all) {
    if (visible.includes(index)) continue;
    const candidate = [...visible, index];
    if (used(candidate, true) > available) break;
    visible.push(index);
  }
  return visible.sort((a, b) => a - b);
}
