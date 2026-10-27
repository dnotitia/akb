import { createContext, useContext, type Dispatch, type SetStateAction, type ReactNode } from "react";

export interface ReadingChrome {
  toolbarHost: HTMLElement | null;
  referenceControl: ReactNode;
  detailsHost: HTMLElement | null;
  contextHeaderHost: HTMLElement | null;
  narrow: boolean;
  detailsOpen: boolean;
  setDetailsOpen: Dispatch<SetStateAction<boolean>>;
}

export const ReadingChromeContext = createContext<ReadingChrome | null>(null);

export function useReadingChrome() { return useContext(ReadingChromeContext); }
