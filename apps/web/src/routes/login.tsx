import { FormEvent, useState } from "react";
import { LockKeyhole, LogIn } from "lucide-react";

import { Button } from "../components/ui/button";
import { API_BASE_URL, apiErrorMessage, apiErrorStatus, login } from "../lib/api";

type LoginRouteProps = {
  onNavigate: (path: string) => void;
};

export function LoginRoute({ onNavigate }: LoginRouteProps) {
  const [email, setEmail] = useState("admin@teplo.local");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await login(email, password);
      onNavigate("/settings");
    } catch (loginError) {
      const status = apiErrorStatus(loginError);
      setError(
        status === 401
          ? "Неверная почта или пароль"
          : status === undefined
            ? // Адрес берём фактический (VITE_API_BASE_URL): на превью-стендах порт свой, и
              // зашитый 8000 отправлял проверять не тот backend.
              `API недоступен: проверьте backend на ${API_BASE_URL}`
            : apiErrorMessage(loginError, "Не удалось войти"),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="grid min-h-screen place-items-center px-5 py-8">
      <section className="w-full max-w-sm rounded-lg border border-border bg-white p-5 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <LockKeyhole size={18} aria-hidden="true" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-normal">Тепло</h1>
            <div className="text-sm text-muted-foreground">Вход в приложение</div>
          </div>
        </div>

        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <label className="grid gap-1.5 text-sm font-medium">
            Почта
            <input
              autoComplete="email"
              className="h-10 rounded-md border border-border bg-white px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Пароль
            <input
              autoComplete="current-password"
              className="h-10 rounded-md border border-border bg-white px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>

          {error ? <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div> : null}

          <Button disabled={isSubmitting} type="submit">
            <LogIn size={16} aria-hidden="true" />
            {isSubmitting ? "Вход..." : "Войти"}
          </Button>
        </form>
      </section>
    </main>
  );
}
