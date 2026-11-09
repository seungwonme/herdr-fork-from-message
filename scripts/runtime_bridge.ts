import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Context = {
	cwd: string;
	model?: { provider: string; id: string };
	sessionManager: {
		getSessionId(): string;
		getSessionFile(): string | undefined;
	};
	ui: { notify(message: string, level?: string): void };
};
type Event = {
	toolName?: string;
	systemPrompt?: string | string[];
	type?: string;
};
type API = {
	on(name: string, handler: (event: Event, ctx: Context) => unknown): void;
	getThinkingLevel(): string | undefined;
	setActiveTools(names: string[]): Promise<void>;
};
type Registration = {
	status: "registered" | "not_ready" | "ignored";
	reason?: string;
	diagnostics?: { candidate_count?: number; candidate_pids?: number[] };
};
type BridgeOptions = {
	register?: (payload: Record<string, unknown>) => Promise<Registration>;
	now?: () => number;
};

const STARTUP_GRACE_MS = 10_000;

const environmentKeys = ["PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"] as const;
const xdgKeys = [
	"XDG_CONFIG_HOME",
	"XDG_DATA_HOME",
	"XDG_STATE_HOME",
	"XDG_CACHE_HOME",
];

export function runtimeAgent(
	environment: Record<string, string | undefined>,
	executable: string,
	argv: readonly string[],
): "omp" | undefined {
	// Only the runtime entrypoint is evidence, never prompts or extension arguments.
	const explicit = environment.HERDR_PANEL_FORK_AGENT;
	if (explicit !== undefined) return explicit === "omp" ? "omp" : undefined;
	for (const path of [executable, argv[0]]) {
		if (path && basename(path).replace(/\.exe$/, "") === "omp") return "omp";
	}
}

const root = dirname(fileURLToPath(import.meta.url));
const reads = new Set(["read", "grep", "glob"]);
const policy =
	"Independent read-only fork: inherited work is background. Do not resume prior tasks, change files/memory/settings, create agents/jobs, or contact the parent. The checkout is shared. Only read tools are permitted.";

export function readOnlyGuard(event: Event) {
	if (!reads.has(event.toolName ?? "")) {
		return {
			block: true,
			reason:
				"Read-only fork: this tool can mutate state or escape the read-only boundary.",
		};
	}
}

export default function runtimeBridge(api: API, options: BridgeOptions = {}) {
	if (process.env.HERDR_ENV !== "1" || !process.env.HERDR_PANE_ID) return;
	if (!runtimeAgent(process.env, process.execPath, process.argv)) {
		api.on("session_start", (_event, ctx) => {
			ctx.ui.notify(
				"Panel Fork registration unavailable: the OMP runtime could not be identified. Set HERDR_PANEL_FORK_AGENT to the verified product.",
				"warning",
			);
		});
		return;
	}
	const readOnly = process.env.HERDR_PANEL_FORK_READ_ONLY === "1";
	const on = api.on.bind(api);
	let current: Context | undefined;
	let busy = false;
	let ready = !readOnly;
	let timer: ReturnType<typeof setInterval> | undefined;
	let warned = false;
	let nonOwner = false;
	let stopped = false;
	let notReadySince: number | undefined;
	const now = options.now ?? Date.now;
	async function guard() {
		if (!readOnly) return;
		await api.setActiveTools(["read", "grep", "glob"]);
		ready = true;
	}

	async function publish(): Promise<boolean> {
		if (busy || !current || nonOwner || stopped) return false;
		busy = true;
		try {
			const ctx = current;
			const environment: Record<string, string> = {};
			for (const key of [...environmentKeys, ...xdgKeys]) {
				if (process.env[key]) environment[key] = process.env[key] as string;
			}
			const payload = {
				agent: "omp",
				pid: process.pid,
				cwd: ctx.cwd,
				session_id: ctx.sessionManager.getSessionId(),
				session_file: ctx.sessionManager.getSessionFile(),
				model:
					ctx.model &&
					ctx.model.provider !== "unknown" &&
					ctx.model.id !== "unknown"
						? `${ctx.model.provider}/${ctx.model.id}`
						: undefined,
				thinking: api.getThinkingLevel(),
				environment,
				bridge_version: 1,
				read_only_ready: ready,
				registration_valid: true,
			};
			const installed = join(
				homedir(),
				".agents/plugins/herdr-fork-from-message/scripts/fork_from_message.py",
			);
			const helper =
				process.env.HERDR_PANEL_FORK_HELPER ??
				(existsSync(join(root, "fork_from_message.py"))
					? join(root, "fork_from_message.py")
					: installed);
			const helperEnvironment = { ...process.env };
			if (helperEnvironment.HERDR_PANEL_FORK_STATE_DIR)
				helperEnvironment.HERDR_PLUGIN_STATE_DIR =
					helperEnvironment.HERDR_PANEL_FORK_STATE_DIR;
			const receipt = options.register
				? await options.register(payload)
				: await new Promise<Registration>((resolve, reject) => {
						const child = spawn("python3", [helper, "--register"], {
							env: helperEnvironment,
							stdio: ["pipe", "pipe", "pipe"],
							timeout: 4000,
						});
						let detail = "";
						let output = "";
						child.stdout.on("data", (data) => {
							output = (output + data.toString()).slice(0, 8192);
						});
						child.stderr.on("data", (data) => {
							detail = (detail + data.toString()).slice(0, 2048);
						});
						child.on("error", reject);
						child.on("close", (code) => {
							if (code !== 0) {
								reject(new Error(detail || "owner registration failed"));
								return;
							}
							try {
								const result = JSON.parse(output);
								if (
									!result ||
									!["registered", "not_ready", "ignored"].includes(
										result.status,
									)
								)
									throw new Error("Invalid registration receipt");
								resolve(result);
							} catch {
								reject(new Error("Invalid registration receipt"));
							}
						});
						child.stdin.on("error", () => {});
						child.stdin.end(JSON.stringify(payload));
					});
			if (stopped) return false;
			if (
				receipt.status === "ignored" &&
				receipt.reason === "nested_non_owner"
			) {
				nonOwner = true;
				if (timer) clearInterval(timer);
				return false;
			}
			if (
				receipt.status === "not_ready" &&
				["foreground_not_visible", "pane_owner_not_visible"].includes(
					receipt.reason ?? "",
				)
			) {
				notReadySince ??= now();
				if (now() - notReadySince < STARTUP_GRACE_MS) return false;
				throw new Error(
					`${receipt.reason} after startup grace (candidate_count=${receipt.diagnostics?.candidate_count ?? 0})`,
				);
			}
			if (receipt.status !== "registered")
				throw new Error("Invalid registration receipt");
			notReadySince = undefined;
			warned = false;
			return true;
		} catch (error) {
			if (!warned && !stopped) {
				current?.ui.notify(
					`Panel Fork registration unavailable: ${String(error)}`,
					"warning",
				);
				warned = true;
			}
			return false;
		} finally {
			busy = false;
		}
	}

	const update = async (_event: Event, ctx: Context) => {
		if (stopped || nonOwner) return;
		current = ctx;
		await guard();
		await publish();
		if (!timer && !nonOwner && !stopped) {
			timer = setInterval(() => {
				void publish();
			}, 5000);
			timer.unref();
		}
	};
	on("session_start", update);
	on("agent_end", update);
	for (const event of [
		"session_before_switch",
		"session_before_branch",
		"session_before_tree",
	]) {
		on(event, () => (readOnly ? { cancel: true } : undefined));
	}
	for (const event of ["session_switch", "session_branch", "session_tree"])
		on(event, update);
	on("session_shutdown", () => {
		stopped = true;
		if (timer) clearInterval(timer);
		current = undefined;
	});
	on("before_agent_start", async (event, ctx) => {
		await update(event, ctx);
		if (!readOnly) return;
		const original = event.systemPrompt ?? [];
		return {
			systemPrompt: Array.isArray(original)
				? [...original, policy]
				: `${original}\n\n${policy}`,
		};
	});
	api.on("tool_call", (event) => (readOnly ? readOnlyGuard(event) : undefined));
}
