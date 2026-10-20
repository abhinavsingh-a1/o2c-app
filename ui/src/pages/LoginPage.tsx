import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router";

import { api } from "../api/client";
import { setSession } from "../auth";

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error } = await api.POST("/auth/login", { body: { username, password } });
    setBusy(false);
    if (error || !data) {
      setError("Wrong username or password.");
      return;
    }
    setSession({ token: data.access_token, username: data.user.username, name: data.user.name });
    navigate("/");
  }

  return (
    <form className="panel login" onSubmit={submit}>
      <h1>Sign in</h1>
      <label>
        Username
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
      </label>
      <label>
        Password
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      {error && <p className="error">{error}</p>}
      <button className="primary" disabled={busy || !username || !password}>
        Sign in
      </button>
    </form>
  );
}
