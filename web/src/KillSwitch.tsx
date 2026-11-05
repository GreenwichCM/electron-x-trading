import { type FormEvent, useState } from "react";
import { api, type User } from "./auth";
import { usePolling } from "./usePolling";

type KillSwitchState = { active: boolean; reason: string; by: string; time: string };

/** A red banner while trading is halted. Risk users get the controls to halt and resume. */
export default function KillSwitch({ user }: { user: User }) {
  const [refresh, setRefresh] = useState(0);
  const state = usePolling<KillSwitchState>("/api/killswitch", 2000, refresh);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const canTrip = user.permissions.includes("killswitch.trip");

  const change = async (active: boolean, event?: FormEvent) => {
    event?.preventDefault();
    setError("");
    if (active && !reason.trim()) {
      setError("Give a reason for halting trading.");
      return;
    }
    try {
      await api("/api/killswitch", { method: "POST", body: JSON.stringify({ active, reason }) });
      setReason("");
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Change failed");
    }
  };

  if (!state) return null;
  if (state.active) {
    return (
      <div className="killswitch on" role="alert">
        <div>
          <strong>Trading halted.</strong> {state.reason}
          <span className="meta">
            {" "}
            by {state.by}
            {state.time && ` at ${new Date(state.time).toLocaleTimeString()}`}
          </span>
        </div>
        {canTrip && (
          <button className="resume" onClick={() => change(false)}>
            Resume trading
          </button>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    );
  }
  if (!canTrip) return null;
  return (
    <form className="killswitch off" onSubmit={(e) => change(true, e)}>
      <span>Kill switch</span>
      <input
        id="killswitch-reason"
        placeholder="Reason for halting all trading"
        value={reason}
        onChange={(e) => {
          setReason(e.target.value);
          setError("");
        }}
      />
      <button type="submit" className="halt">
        Halt trading
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}
