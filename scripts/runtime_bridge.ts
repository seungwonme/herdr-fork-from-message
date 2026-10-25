import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Context = {
	cwd: string;
	model?: { provider: string; id: string };
	sessionManager: {
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

const root = dirname(fileURLToPath(import.meta.url));
const reads = new Set(["read", "search", "find", "ast_grep", "grep", "glob"]);
const policy =
	"Independent question-only fork: inherited work is background. Wait for new questions; do not resume prior tasks, change files/memory/settings, create agents/jobs, or contact the parent. The checkout is shared. Only read tools are permitted.";

export function questionGuard(event: Event) {
	if (!reads.has(event.toolName ?? "")) {
		return {
			block: true,
			reason:
				"Question-only fork: this tool can mutate state or escape the read-only boundary.",
		};
	}
}

export default function runtimeBridge(api: API) {
	if (process.env.HERDR_ENV !== "1" || !process.env.HERDR_PANE_ID) return;
	const agent =
		process.env.HERDR_QUESTION_AGENT ??
		(process.execPath.split("/").pop() === "gjc" ? "gjc" : "omp");
	const question = process.env.HERDR_QUESTION_FORK === "1";
	let current: Context | undefined;
	let busy = false;
	let ready = !question;
	let timer: ReturnType<typeof setInterval> | undefined;
	let warned = false;
	let invalidated = false;

	async function guard(ctx: Context) {
		if (!question) return;
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

	async function publish(invalidate = false) {
		if (busy || !current || (invalidated && !invalidate)) return;
		busy = true;
		try {
			const ctx = current;
			const environment: Record<string, string> = {};
			for (const key of agent === "gjc"
				? [
						"GJC_CONFIG_DIR",
						"GJC_CODING_AGENT_DIR",
						"XDG_CONFIG_HOME",
						"XDG_DATA_HOME",
						"XDG_STATE_HOME",
						"XDG_CACHE_HOME",
					]
				: [
						"PI_CONFIG_DIR",
						"PI_CODING_AGENT_DIR",
						"XDG_CONFIG_HOME",
						"XDG_DATA_HOME",
						"XDG_STATE_HOME",
						"XDG_CACHE_HOME",
					]) {
				if (process.env[key]) environment[key] = process.env[key] as string;
			}
			const payload = {
				agent,
				pid: process.pid,
				cwd: ctx.cwd,
				session_id: ctx.sessionManager.getSessionId(),
				session_file: ctx.sessionManager.getSessionFile(),
				model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
				profile: ctx.getActiveModelProfile?.(),
				thinking: api.getThinkingLevel(),
				environment,
				bridge_version: 1,
				question_ready: ready,
				registration_valid: !invalidated,
			};
			const installed = join(
				homedir(),
				".agents/plugins/herdr-fork-from-message/scripts/fork_from_message.py",
			);
			const helper =
				process.env.HERDR_QUESTION_HELPER ??
				(existsSync(join(root, "fork_from_message.py"))
					? join(root, "fork_from_message.py")
					: installed);
			await new Promise<void>((resolve, reject) => {
				const child = spawn("python3", [helper, "--register"], {
					stdio: ["pipe", "ignore", "pipe"],
					timeout: 4000,
				});
				let detail = "";
				child.stderr.on("data", (data) => {
					detail += data.toString();
				});
				child.on("error", reject);
				child.on("close", (code) =>
					code === 0
						? resolve()
						: reject(new Error(detail || "owner registration failed")),
				);
				child.stdin.on("error", () => {});
				child.stdin.end(JSON.stringify(payload));
			});
		} catch (error) {
			if (!warned) {
				current?.ui.notify(
					`Question fork registration unavailable: ${String(error)}`,
					"warning",
				);
				warned = true;
			}
		} finally {
			busy = false;
		}
	}

	const update = async (_event: Event, ctx: Context) => {
		current = ctx;
		await guard(ctx);
		if (invalidated) return;
		await publish();
		if (!timer) {
			timer = setInterval(() => {
				void publish();
			}, 5000);
			timer.unref();
		}
	};
	api.on("session_start", update);
	api.on("agent_end", update);
	for (const event of [
		"session_before_switch",
		"session_before_branch",
		"session_before_tree",
	]) {
		api.on(event, () => (question ? { cancel: true } : undefined));
	}
	for (const event of ["session_switch", "session_branch", "session_tree"])
		api.on(event, async (value, ctx) => {
			if (agent === "gjc") {
				// GJC 0.19 may retain the startup manager in extension contexts after a
				// cold-sidecar transition. Refuse stale identity rather than re-publish it.
				invalidated = true;
				ready = false;
				if (timer) clearInterval(timer);
				// Let any preceding heartbeat finish, then overwrite it with a tombstone.
				while (busy) await new Promise((resolve) => setTimeout(resolve, 10));
				await publish(true);
				ctx.ui.notify(
					"Question fork registration invalidated by session navigation. Restart GJC to register its current session safely.",
					"warning",
				);
				return;
			}
			await update(value, ctx);
		});
	api.on("session_shutdown", () => {
		if (timer) clearInterval(timer);
		current = undefined;
	});
	api.on("before_agent_start", async (event, ctx) => {
		await update(event, ctx);
		if (!question) return;
		const original = event.systemPrompt ?? [];
		return {
			systemPrompt: Array.isArray(original)
				? [...original, policy]
				: `${original}\n\n${policy}`,
		};
	});
	api.on("tool_call", (event) => (question ? questionGuard(event) : undefined));
}
