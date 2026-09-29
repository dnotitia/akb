import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useSettingsDialog } from "@/contexts/settings-dialog-context";
import HomePage from "@/pages/home";

/** Compatibility entry for saved /settings?tab=… links. There is no settings page. */
export default function SettingsPage() {
  const settings = useSettingsDialog();
  const [params] = useSearchParams();
  const tab = params.get("tab") ?? "profile";
  const openSettings = settings?.openSettings;
  useEffect(() => { openSettings?.(tab); }, [openSettings, tab]);
  return <HomePage />;
}
