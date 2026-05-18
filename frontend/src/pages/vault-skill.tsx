import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { getDocument } from "@/lib/api";
import { DocumentView } from "@/components/document-view";
import { SkillCreateButton } from "@/components/skill/skill-create-button";
import { Skeleton } from "@/components/ui/skeleton";

const SKILL_PATH = "overview/vault-skill.md";

export default function VaultSkillPage() {
  const { name: vault } = useParams<{ name: string }>();

  const docQuery = useQuery({
    queryKey: ["document", vault, SKILL_PATH],
    queryFn: () => getDocument(vault!, SKILL_PATH),
    retry: false,
    enabled: !!vault,
  });

  if (!vault) return null;

  if (docQuery.isLoading) {
    return (
      <div className="max-w-[1020px] mx-auto w-full px-4 py-8">
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (docQuery.isError) {
    const errStatus = (docQuery.error as any)?.status;
    const errMessage = String((docQuery.error as any)?.message ?? "");
    // Backend NotFoundError serializes as {"error": "Document not found: ..."}
    // without a structured detail, so api() throws a plain Error with no .status
    // attached. Match the message instead — both "404" and "not found" cover
    // the canonical paths.
    const isNotFound =
      errStatus === 404 || /404|not found/i.test(errMessage);
    if (!isNotFound) {
      return (
        <div className="max-w-[1020px] mx-auto w-full px-4 py-8 text-sm text-foreground-muted">
          Failed to load vault skill: {errMessage || "unknown error"}
        </div>
      );
    }
    return (
      <div className="max-w-[1020px] mx-auto w-full px-4 py-12 flex flex-col items-start gap-4">
        <div className="flex items-center gap-2">
          <Sparkles className="h-5 w-5 text-foreground-muted" />
          <h2 className="font-serif text-2xl">No vault skill yet</h2>
        </div>
        <p className="text-[14px] text-foreground-muted leading-relaxed max-w-prose">
          Define agent conventions for this vault — what types, tags, and relations to use.
          Agents read this via{" "}
          <code className="coord">
            akb_help(topic=&quot;vault-skill&quot;, vault=&quot;{vault}&quot;)
          </code>
          .
        </p>
        <SkillCreateButton vault={vault} />
      </div>
    );
  }

  return (
    <div className="max-w-[1020px] mx-auto w-full px-4 py-8">
      <DocumentView vault={vault} docId={SKILL_PATH} />
    </div>
  );
}
