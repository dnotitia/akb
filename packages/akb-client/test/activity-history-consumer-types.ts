import {
  createClient,
  type AkbActivityEnvelope,
  type AkbActivityListOptions,
  type AkbActivityRecentOptions,
  type AkbDocumentDiffEnvelope,
  type AkbDocumentDiffOptions,
  type AkbDocumentHistoryEnvelope,
  type AkbDocumentHistoryOptions,
  type AkbOperationResponse,
  type AkbRecentChangesEnvelope,
  type AkbSuccessEnvelope,
  type operations,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1" }).vault("proof");
const historyOptions: AkbDocumentHistoryOptions = { limit: 20 };
const diffOptions: AkbDocumentDiffOptions = { vault: "other", commit: "abc1234" };
const listOptions: AkbActivityListOptions = { collection: null, author: null, since: null, limit: 20 };
const recentOptions: AkbActivityRecentOptions = { vault: "proof", limit: 10 };

const history: AkbDocumentHistoryEnvelope = (await client.docs.history("guides/readme.md", historyOptions)).throwOnError().data;
const diff: AkbDocumentDiffEnvelope = (await client.docs.diff("guides/readme.md", diffOptions)).throwOnError().data;
const activity: AkbActivityEnvelope = (await client.activity.list(listOptions)).throwOnError().data;
const recent: AkbRecentChangesEnvelope = (await client.activity.recent(recentOptions)).throwOnError().data;

history.kind satisfies "document_history";
history.history.at(0)?.author_name satisfies string | null | undefined;
diff.kind satisfies "document_diff";
diff.type satisfies "added" | "deleted" | "modified" | "unknown" | "unchanged";
diff.error satisfies string | null | undefined;
activity.kind satisfies "activity";
activity.activity.at(0)?.files.at(0)?.change satisfies "added" | "deleted" | "modified" | undefined;
activity.activity.at(0)?.author_name satisfies string | null | undefined;
recent.kind satisfies "recent_changes";
recent.changes.at(0)?.commit satisfies string | null | undefined;
recent.changes.at(0)?.changed_at satisfies string | null | undefined;

history satisfies AkbOperationResponse<operations["documentsHistory"]>;
diff satisfies AkbOperationResponse<operations["documentsDiff"]>;
activity satisfies AkbOperationResponse<operations["activityList"]>;
recent satisfies AkbOperationResponse<operations["activityRecent"]>;

declare const success: AkbSuccessEnvelope;
if (success.kind === "document_history") {
  success.history.at(0)?.message satisfies string | undefined;
} else if (success.kind === "document_diff") {
  success.diff satisfies string;
} else if (success.kind === "activity") {
  success.total satisfies number;
} else if (success.kind === "recent_changes") {
  success.changes.at(0)?.doc_id satisfies string | undefined;
}

// @ts-expect-error commit is required.
client.docs.diff("guides/readme.md", {});
// @ts-expect-error history has no topK option.
client.docs.history("guides/readme.md", { topK: 20 });
// @ts-expect-error list has no topK option.
client.activity.list({ topK: 20 });
// @ts-expect-error recent does not accept activity filters.
client.activity.recent({ author: "u1" });
