import { useCallback, useEffect, useRef, useState } from "react";

export type SetupStepNumber = 1 | 2 | 3;

function scrollContainer(element: HTMLElement): HTMLElement | null {
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) return parent;
  }
  return null;
}

function alignStickyNavigation(root: HTMLElement, container: HTMLElement | null) {
  // Sticky positioning starts at the scrollport's padding edge. Keep the bar
  // flush with that viewport so earlier form rows cannot peek above it.
  const padding = container ? parseFloat(getComputedStyle(container).paddingTop) || 0 : 0;
  const value = `${-padding}px`;
  if (root.style.getPropertyValue("--connection-sticky-top") !== value) root.style.setProperty("--connection-sticky-top", value);
}

/** Section navigation only: reading a step never completes or resets setup. */
export function useSetupStepNavigation() {
  const rootRef = useRef<HTMLDivElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const stepsRef = useRef<Array<HTMLElement | null>>([null, null, null]);
  const [currentStep, setCurrentStep] = useState<SetupStepNumber>(1);

  const jumpToStep = useCallback((step: SetupStepNumber) => {
    const root = rootRef.current;
    const section = stepsRef.current[step - 1];
    if (!root || !section) return;
    const container = scrollContainer(root);
    alignStickyNavigation(root, container);
    const offset = (navigationRef.current?.offsetHeight ?? 0) + 12;
    if (section.getBoundingClientRect().height > 0) {
      const top = section.getBoundingClientRect().top - (container?.getBoundingClientRect().top ?? 0) - offset;
      if (container) container.scrollTo({ top: container.scrollTop + top, behavior: "instant" });
      else window.scrollTo({ top: window.scrollY + top, behavior: "instant" });
    }
    section.querySelector<HTMLElement>("h3")?.focus({ preventScroll: true });
    setCurrentStep(step);
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const container = scrollContainer(root);
    const target = container ?? window;
    let frame: number | null = null;
    const update = () => {
      frame = null;
      alignStickyNavigation(root, container);
      const bounds = root.getBoundingClientRect();
      if (!root.isConnected || bounds.height <= 0) return;
      const viewport = container?.getBoundingClientRect();
      const top = viewport?.top ?? 0;
      const bottom = viewport?.bottom ?? window.innerHeight;
      if (bounds.bottom <= top || bounds.top >= bottom) return;
      const anchor = Math.max(top, navigationRef.current?.getBoundingClientRect().bottom ?? top) + 12;
      // A short last section cannot always reach the top of its scroll area.
      // Keep the visible focused control/heading as the user's current step.
      const focused = document.activeElement;
      if (focused instanceof HTMLElement) {
        const focusedBounds = focused.getBoundingClientRect();
        const index = stepsRef.current.findIndex(section => section?.contains(focused));
        if (index >= 0 && focusedBounds.bottom > anchor && focusedBounds.top < bottom) {
          setCurrentStep((index + 1) as SetupStepNumber);
          return;
        }
      }
      let step: SetupStepNumber = 1;
      stepsRef.current.forEach((section, index) => {
        if (section && section.getBoundingClientRect().top <= anchor + 1) step = (index + 1) as SetupStepNumber;
      });
      setCurrentStep(step);
    };
    const schedule = () => { if (frame === null) frame = requestAnimationFrame(update); };
    target.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    [root, navigationRef.current, ...stepsRef.current].forEach(element => { if (element) observer?.observe(element); });
    schedule();
    return () => {
      target.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      observer?.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, []);

  return { rootRef, navigationRef, stepsRef, currentStep, setCurrentStep, jumpToStep };
}
