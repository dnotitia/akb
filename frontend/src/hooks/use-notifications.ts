import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useCurrentUser } from "@/contexts/current-user-context";
import { notificationCount, notificationPage, NotificationsUnavailable, type NotificationCategory } from "@/lib/api-notifications";

export function useNotificationCount() {
  const user = useCurrentUser();
  return useQuery({
    queryKey: ["notifications", user?.user_id, "count"],
    queryFn: ({ signal }) => notificationCount(signal),
    enabled: !!user,
    retry: false,
    refetchOnWindowFocus: "always",
    refetchIntervalInBackground: false,
    refetchInterval: query => query.state.error instanceof NotificationsUnavailable
      ? false : query.state.error ? 120_000 : 45_000,
  });
}

export function useNotificationInbox(state: "all" | "unread", category: NotificationCategory = "all") {
  const user = useCurrentUser();
  return useInfiniteQuery({
    queryKey: ["notifications", user?.user_id, "inbox", state, category],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => notificationPage(state, pageParam, signal, category),
    getNextPageParam: page => page.next_cursor || undefined,
    enabled: !!user,
    retry: false,
    refetchOnWindowFocus: "always",
    staleTime: 0,
    refetchIntervalInBackground: false,
    refetchInterval: query => query.state.error instanceof NotificationsUnavailable
      ? false : query.state.error ? 120_000 : 45_000,
  });
}
