"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");

    if (!username.trim() || !password.trim()) {
      setError("Usuario y contraseña son requeridos");
      return;
    }

    const apiUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!apiUrl) {
      setError("Falta configurar NEXT_PUBLIC_API_URL");
      return;
    }

    setLoading(true);

    try {
      const response = await fetch(`${apiUrl}/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ username: username.trim(), password }),
      });

      if (response.status === 401) {
        setError("Usuario o contraseña incorrectos");
        setPassword("");
        return;
      }

      if (!response.ok) {
        setError("No fue posible iniciar sesion. Intenta nuevamente.");
        return;
      }

      const data = (await response.json()) as { access_token?: string };
      if (!data.access_token) {
        setError("Respuesta invalida del servidor.");
        return;
      }

      localStorage.setItem("token", data.access_token);
      router.push("/");
    } catch (err) {
      console.error("Login error:", err);
      setError("No se pudo conectar con el servidor.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-100 px-4 py-10">
      <div className="mx-auto flex min-h-[80vh] w-full max-w-5xl items-center justify-center rounded-3xl bg-white shadow-xl shadow-slate-200/60">
        <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-8">
          <p className="mb-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">
            TYS OCR
          </p>
          <h1 className="mb-6 text-3xl font-semibold text-slate-900">
            Iniciar sesion
          </h1>

          <form className="space-y-4" onSubmit={handleSubmit}>
            <div>
              <label
                htmlFor="username"
                className="mb-1 block text-sm font-medium text-slate-700"
              >
                Username
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
                className="w-full rounded-xl border border-slate-300 px-3 py-2.5 text-slate-900 outline-none transition focus:border-slate-600"
              />
            </div>

            <div>
              <label
                htmlFor="password"
                className="mb-1 block text-sm font-medium text-slate-700"
              >
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                className="w-full rounded-xl border border-slate-300 px-3 py-2.5 text-slate-900 outline-none transition focus:border-slate-600"
              />
            </div>

            {error && (
              <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {loading ? "Ingresando..." : "Ingresar"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
