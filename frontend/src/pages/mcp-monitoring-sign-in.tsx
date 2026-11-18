import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { getAuthConfig, loginMcpMonitoring } from "@/lib/api";

export function McpMonitoringSignIn({ onSignIn }: { onSignIn: () => void }) {
  const [params] = useSearchParams();
  const [error, setError] = useState(params.get("auth_error") === "sign_in_failed");
  const [submitting, setSubmitting] = useState(false);
  const config = useQuery({ queryKey: ["monitoring-auth-config"], queryFn: getAuthConfig });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setError(false);
    setSubmitting(true);
    try {
      await loginMcpMonitoring(String(data.get("username")), String(data.get("password")));
      form.reset();
      onSignIn();
    } catch {
      setError(true);
    } finally {
      setSubmitting(false);
    }
  }

  const providers = config.data?.providers.filter(provider => provider.login_url) ?? [];
  return (
    <Panel className="max-w-md">
      <PanelHeader label="Sign in to MCP monitoring" />
      <div className="space-y-4 p-4">
        {config.isPending && <p role="status">Loading sign-in options…</p>}
        {config.isSuccess && (!config.data.available || (config.data.auth_mode === "sso" && providers.length === 0)) &&
          <Alert>Monitoring sign-in is unavailable.</Alert>}
        {error && <Alert variant="destructive">Sign-in failed. Use your dedicated monitoring account.</Alert>}
        {config.data?.local_auth.enabled &&
          <form className="space-y-3" onSubmit={submit}>
            <div className="space-y-1">
              <Label htmlFor="monitoring-username">Username</Label>
              <Input id="monitoring-username" name="username" autoComplete="username" required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="monitoring-password">Password</Label>
              <Input id="monitoring-password" name="password" type="password" autoComplete="current-password" required />
            </div>
            <Button type="submit" loading={submitting}>Sign in</Button>
          </form>}
        {providers.map(provider => provider.login_url &&
          <Button key={provider.alias} asChild variant="outline" className="w-full">
            <a href={`${provider.login_url}?purpose=monitoring`}>
              Sign in with {provider.display_name}
            </a>
          </Button>)}
      </div>
    </Panel>
  );
}
