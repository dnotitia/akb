import type { ReactNode } from "react";

export function SettingsHeading({ id, title, description, children }: {
  id: string; title: string; description: string; children?: ReactNode;
}) {
  return <header className="settings-section-heading">
    <div><h2 id={id}>{title}</h2><p>{description}</p></div>
    {children}
  </header>;
}

export function SettingsGroup({ id, title, description, children }: {
  id: string; title: string; description?: string; children: ReactNode;
}) {
  return <section className="settings-group" aria-labelledby={id}>
    <header className="settings-group-heading"><h3 id={id}>{title}</h3>{description && <p>{description}</p>}</header>
    <div className="settings-group-content">{children}</div>
  </section>;
}
