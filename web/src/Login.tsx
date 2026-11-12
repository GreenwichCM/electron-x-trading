import { useEffect, useState } from "react";
import { api, setToken, type User } from "./auth";

/** Development login: pick a user from electronx.users. Replaced by Google sign-in later. */
export default function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<User[]>("/api/dev/users")
      .then(setUsers)
      .catch(() => setError("Can't reach the api. Is it running with API_DEV_LOGIN=Y?"));
  }, []);

  const signIn = async (id: string) => {
    try {
      const res = await api<{ token: string; user: User }>("/api/dev/login", {
        method: "POST",
        body: JSON.stringify({ user_id: id }),
      });
      setToken(res.token);
      onLogin(res.user);
    } catch {
      setError("Sign-in failed.");
    }
  };

  return (
    <main className="login">
      <h1>Sign in</h1>
      <p className="hint">Development login: choose a user.</p>
      <div className="users">
        {users.map((u) => (
          <button key={u.id} onClick={() => signIn(u.id)}>
            <strong>{u.name}</strong>
            <span>{u.roles.join(", ")}</span>
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </main>
  );
}
