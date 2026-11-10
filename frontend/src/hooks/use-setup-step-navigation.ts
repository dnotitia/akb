import { useCallback, useLayoutEffect, useRef, useState } from "react";

export type SetupStepNumber = 1 | 2 | 3;

/** Navigation is explicit. Scrolling never advances setup or implies success. */
export function useSetupStepNavigation() {
  const rootRef = useRef<HTMLDivElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const stepsRef = useRef<Array<HTMLElement | null>>([null, null, null]);
  const [currentStep, setCurrentStep] = useState<SetupStepNumber>(1);
  const pendingFocus = useRef(false);
  const [focusRequest, setFocusRequest] = useState(0);
  const jumpToStep = useCallback((step: SetupStepNumber) => {
    pendingFocus.current = true;
    setCurrentStep(step);
    setFocusRequest(value => value + 1);
  }, []);
  useLayoutEffect(() => {
    if (!pendingFocus.current) return;
    pendingFocus.current = false;
    const root = rootRef.current;
    const section = stepsRef.current[currentStep - 1];
    if (!root || !section) return;
    let container = root.parentElement;
    while (container && !/(auto|scroll)/.test(getComputedStyle(container).overflowY)) container = container.parentElement;
    const top = root.getBoundingClientRect().top - (container?.getBoundingClientRect().top ?? 0);
    if (root.getBoundingClientRect().height > 0) {
      if (container) container.scrollTo({ top: container.scrollTop + top, behavior: "instant" });
      else window.scrollTo({ top: window.scrollY + top, behavior: "instant" });
    }
    section.querySelector<HTMLElement>("h3")?.focus({ preventScroll: true });
  }, [currentStep, focusRequest]);
  return { rootRef, navigationRef, stepsRef, currentStep, jumpToStep };
}
