import { cn } from "@/lib/utils";

// Brand lockup (design-system): a rounded teal mark with an orange spark + the
// gradient "akb" wordmark. One source of truth so every surface renders the
// brand identically. CSS-only mark (no image asset) so it themes cleanly.
export function Logo({
  size = 28,
  wordmark = true,
  subtitle = false,
  className,
}: {
  size?: number;
  wordmark?: boolean;
  subtitle?: boolean;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <span
        className="brand-mark relative inline-grid place-items-center rounded-[36%] font-display text-white"
        style={{
          width: size,
          height: size,
          fontSize: size * 0.5,
          background: "linear-gradient(135deg, var(--color-teal), var(--color-teal-2))",
        }}
        aria-hidden
      >
        a
        <span
          className="absolute rounded-full"
          style={{
            width: size * 0.16,
            height: size * 0.16,
            right: size * 0.14,
            bottom: size * 0.16,
            background: "var(--color-orange)",
          }}
        />
      </span>
      {wordmark && (
        <span className="flex flex-col leading-none">
          <span
            className="brand-gradient font-display tracking-tight"
            style={{ fontSize: size * 0.62 }}
          >
            akb
          </span>
          {subtitle && (
            <span className="coord mt-0.5" style={{ fontSize: 9 }}>
              knowledgebase
            </span>
          )}
        </span>
      )}
    </span>
  );
}
