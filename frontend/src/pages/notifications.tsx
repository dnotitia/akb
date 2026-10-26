import { useSearchParams } from "react-router-dom";
import { NotificationInbox } from "@/components/notification-inbox";

export default function NotificationsPage() {
  const [params, setParams] = useSearchParams();
  const state = params.get("state") === "unread" ? "unread" : "all";
  const category = params.get("category") === "documents" ? "documents" : params.get("category") === "access" ? "access" : "all";
  return <div className="mx-auto w-full max-w-4xl"><NotificationInbox state={state} category={category} onCategoryChange={value => {
      const next = new URLSearchParams(params); next.set("category", value); setParams(next);
    }} onStateChange={value => {
      const next = new URLSearchParams(params); next.set("state", value); setParams(next);
    }} /></div>;
}
