import { type Environment, LABELS } from "./environment";

/** Which exchange this is, always in view: grey for the simulator, amber for beta, red for production. */
export default function EnvironmentBadge({ env }: { env: Environment | null }) {
  if (!env) return null;
  return (
    <span className={`env-badge ${env.name}`} title={env.order_entry ? "" : "Record-only: no orders are sent from here"}>
      {LABELS[env.name]}
      {!env.order_entry && " · record-only"}
    </span>
  );
}
