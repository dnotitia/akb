import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/ui/page-header";
import { PageShell } from "@/components/ui/page-shell";
import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { NotificationInbox } from "@/components/notification-inbox";

export default function NotificationsPage() {
  const [params, setParams] = useSearchParams();
  const state = params.get("state") === "unread" ? "unread" : "all";
  const category = params.get("category") === "documents" ? "documents" : params.get("category") === "access" ? "access" : "all";
  return <PageShell contentWidth="compact" header={<PageHeader title="Notifications" subtitle="Personal access updates and changes to documents you watch." actions={<Button variant="outline" asChild><Link to="/settings?tab=notifications">Manage watched documents</Link></Button>} />}>
    <Panel variant="workspace"><NotificationInbox state={state} category={category} onCategoryChange={value => {
      const next = new URLSearchParams(params); next.set("category", value); setParams(next);
    }} onStateChange={value => {
      const next = new URLSearchParams(params); next.set("state", value); setParams(next);
    }} /></Panel>
  </PageShell>;
}
