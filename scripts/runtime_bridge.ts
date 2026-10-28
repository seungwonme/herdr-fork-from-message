import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Context = {
	cwd: string;
	sessionMetadata?: { kind: "main" | "sub"; taskDepth: number };
	model?: { provider: string; id: string };
	sessionManager: {
		getCwd?(): string;
		getSessionId(): string;
		getSessionFile(): string | undefined;
	};
	getActiveModelProfile?: () => string | undefined;
	getGoalState?: () => unknown;
	operateGoal?: (op: "pause") => Promise<unknown>;
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
type PiAgent = "gjc" | "omp" | "pi" | "omo";
type Registration = {
	status: "registered" | "not_ready" | "ignored";
	reason?: string;
	diagnostics?: { candidate_count?: number; candidate_pids?: number[] };
};
type BridgeOptions = {
	register?: (payload: Record<string, unknown>) => Promise<Registration>;
	now?: () => number;
	wait?: (milliseconds: number) => Promise<void>;
};

const STARTUP_GRACE_MS = 10_000;
const INVALIDATION_TIMEOUT_MS = 4000;

const environmentKeys: Record<PiAgent, readonly string[]> = {
	gjc: ["GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR"],
	omp: ["PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"],
	pi: ["PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"],
	omo: [
		"OMO_CODING_AGENT_DIR",
		"SENPI_CODING_AGENT_DIR",
		"PI_CODING_AGENT_DIR",
		"OMO_CODING_AGENT_SESSION_DIR",
	],
};
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
): PiAgent | undefined {
	let product: PiAgent | undefined;
	// Product executables take precedence over script paths and user arguments.
	for (const path of [executable, argv[0]]) {
		if (!path) continue;
		const name = basename(path).replace(/\.exe$/, "");
		if (Object.hasOwn(environmentKeys, name)) {
			product = name as PiAgent;
			break;
		}
	}
	// Only inspect the runtime entrypoint, never prompts or extension arguments.
	if (!product) {
		for (const path of argv.slice(0, 2)) {
			if (
				/\/node_modules\/(?:omo-ai\/bin\/omo\.js|@code-yeongyu\/senpi\/dist\/(?:bundle\/)?cli\.js)$/.test(
					path,
				)
			) {
				product = "omo";
				break;
			}
			if (
				/\/node_modules\/@earendil-works\/pi-coding-agent\/dist\/(?:bundle\/)?cli\.js$/.test(
					path,
				)
			) {
				product = "pi";
				break;
			}
		}
	}
	const explicit = environment.HERDR_PANEL_FORK_AGENT;
	if (explicit !== undefined) {
		if (
			!Object.hasOwn(environmentKeys, explicit) ||
			(product && product !== explicit)
		)
			return undefined;
		return explicit as PiAgent;
	}
	if (product) return product;
	if (environment.OMO_NATIVE === "1") return "omo";
	try {
		if (JSON.parse(environment.SENPI_BRAND ?? "null")?.command === "omo")
			return "omo";
	} catch {
		// An invalid brand marker is not evidence of product identity.
	}
}

const root = dirname(fileURLToPath(import.meta.url));
const reads = new Set(["read", "search", "find", "ast_grep", "grep", "glob"]);
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
	const agent = runtimeAgent(process.env, process.execPath, process.argv);
	if (!agent) {
		api.on("session_start", (_event, ctx) => {
			ctx.ui.notify(
				"Panel Fork registration unavailable: the Pi-family product could not be identified. Set HERDR_PANEL_FORK_AGENT to the verified product.",
				"warning",
			);
		});
		return;
	}
	const readOnly = process.env.HERDR_PANEL_FORK_READ_ONLY === "1";
	const on: API["on"] = (name, handler) => {
		api.on(name, (event, ctx) => {
			// In-process GJC tasks share the owner's PID and inherited pane variables.
			if (agent === "gjc" && ctx.sessionMetadata?.kind !== "main") return;
			return handler(event, ctx);
		});
	};
	let current: Context | undefined;
	let busy = false;
	let ready = !readOnly;
	let timer: ReturnType<typeof setInterval> | undefined;
	let warned = false;
	let invalidated = false;
	let nonOwner = false;
	let stopped = false;
	let notReadySince: number | undefined;
	let transitionIdentity: string | undefined;
	let tombstoneConfirmed = false;
	let navigationCompleted = false;
	const now = options.now ?? Date.now;
	const wait =
		options.wait ??
		((milliseconds: number) =>
			new Promise<void>((resolve) => setTimeout(resolve, milliseconds)));

	function conversationIdentity(ctx: Context) {
		return JSON.stringify([
			ctx.sessionManager.getSessionId(),
			ctx.sessionManager.getSessionFile() ?? null,
			sessionCwd(ctx),
		]);
	}

	function sessionCwd(ctx: Context): string {
		if (agent !== "gjc") return ctx.cwd;
		if (!ctx.sessionManager.getCwd)
			throw new Error("GJC runtime does not expose its current session cwd");
		return ctx.sessionManager.getCwd();
	}

	async function guard(ctx: Context) {
		if (!readOnly) return;
		if (agent !== "gjc" && agent !== "omp")
			throw new Error(
				`Read-only mode is not supported for ${agent}: native guard support has not been verified`,
			);
		// Pause through the owning session API, never rewrite its transcript.
		if (agent === "gjc") {
			if (!ctx.operateGoal)
				throw new Error(
					"GJC runtime does not expose the required goal pause gate",
				);
			await ctx.operateGoal("pause");
		}
		await api.setActiveTools(
			agent === "gjc"
				? ["read", "search", "find", "ast_grep"]
				: ["read", "grep", "glob"],
		);
		ready = true;
	}

	async function publish(invalidate = false, timeout = 4000): Promise<boolean> {
		if (busy || !current || nonOwner || stopped || (invalidated && !invalidate))
			return false;
		busy = true;
		try {
			const ctx = current;
			const environment: Record<string, string> = {};
			for (const key of [...environmentKeys[agent], ...xdgKeys]) {
				if (process.env[key]) environment[key] = process.env[key] as string;
			}
			const payload = {
				agent,
				pid: process.pid,
				cwd: sessionCwd(ctx),
				session_id: ctx.sessionManager.getSessionId(),
				session_file: ctx.sessionManager.getSessionFile(),
				model:
					ctx.model &&
					ctx.model.provider !== "unknown" &&
					ctx.model.id !== "unknown"
						? `${ctx.model.provider}/${ctx.model.id}`
						: undefined,
				profile: ctx.getActiveModelProfile?.(),
				thinking: api.getThinkingLevel(),
				environment,
				bridge_version: 1,
				read_only_ready: ready,
				registration_valid: !invalidated,
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
							timeout,
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
			if (stopped || (invalidated && !invalidate)) return false;
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
				if (invalidate) return false;
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
			if (!invalidate && !warned && !stopped && !invalidated) {
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

	async function invalidateOwner(ctx: Context): Promise<boolean> {
		const deadline = now() + INVALIDATION_TIMEOUT_MS;
		invalidated = true;
		ready = false;
		if (timer) clearInterval(timer);
		timer = undefined;
		while (busy && !stopped && now() < deadline) await wait(10);
		if (nonOwner) return true;
		for (
			let attempt = 0;
			attempt < 3 && !stopped && !busy && now() < deadline;
			attempt++
		) {
			if (tombstoneConfirmed) return true;
			if (await publish(true, Math.max(1, deadline - now()))) {
				tombstoneConfirmed = true;
				return true;
			}
			if (attempt < 2) await wait(100);
		}
		if (!stopped)
			ctx.ui.notify(
				"Panel Fork invalidation pending: the previous owner record could not be revoked. Session navigation was not confirmed; keep the current conversation until registration recovers.",
				"warning",
			);
		return false;
	}

	const update = async (_event: Event, ctx: Context) => {
		if (stopped || nonOwner || invalidated) return;
		current = ctx;
		await guard(ctx);
		if (invalidated) return;
		await publish();
		if (!timer && !nonOwner && !stopped && !invalidated) {
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
		on(event, async (_value, ctx) => {
			if (readOnly) return { cancel: true };
			if (
				agent !== "gjc" ||
				stopped ||
				nonOwner ||
				navigationCompleted ||
				!current
			)
				return;
			transitionIdentity = conversationIdentity(ctx);
			if (!(await invalidateOwner(ctx))) return { cancel: true };
		});
	}
	for (const event of ["session_switch", "session_branch", "session_tree"])
		on(event, async (value, ctx) => {
			if (agent === "gjc") {
				// GJC 0.19 may retain the startup manager in extension contexts after a
				// cold-sidecar transition. Refuse stale identity rather than re-publish it.
				transitionIdentity = undefined;
				navigationCompleted = true;
				current ??= ctx;
				if (await invalidateOwner(ctx))
					ctx.ui.notify(
						"Panel Fork registration invalidated by session navigation. Restart GJC to register its current session safely.",
						"warning",
					);
				return;
			}
			await update(value, ctx);
		});
	on("session_shutdown", () => {
		stopped = true;
		transitionIdentity = undefined;
		if (timer) clearInterval(timer);
		current = undefined;
	});
	on("before_agent_start", async (event, ctx) => {
		// A cancelled transition has no switch event. Only the unchanged native
		// conversation may recover, and only after a confirmed tombstone.
		if (
			invalidated &&
			!navigationCompleted &&
			transitionIdentity === conversationIdentity(ctx)
		) {
			if (!tombstoneConfirmed && !(await invalidateOwner(ctx))) return;
			invalidated = false;
			tombstoneConfirmed = false;
			transitionIdentity = undefined;
			ready = !readOnly;
		}
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
