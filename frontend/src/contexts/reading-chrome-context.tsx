import { createContext, useContext, type Dispatch, type SetStateAction, type ReactNode, type RefObject } from "react";

export interface ReferenceNavigationRequest { id: number; href?: string; referenceKey?: string; scrollTop?: number }

export interface ReadingChrome {
  toolbarHost: HTMLElement | null;
  references: ReactNode;
  referencesRequest?: ReferenceNavigationRequest;
  sideControl?: ReactNode;
  detailsOpen: boolean;
  setDetailsOpen: Dispatch<SetStateAction<boolean>>;
  connectionManagerRequest?: number;
  connectionManagerOpener?: RefObject<HTMLButtonElement | null>;
}

export const ReadingChromeContext = createContext<ReadingChrome | null>(null);

export function useReadingChrome() { return useContext(ReadingChromeContext); }
