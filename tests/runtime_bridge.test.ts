import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import bridge, { readOnlyGuard, runtimeAgent } from "../scripts/runtime_bridge";

const original = { ...process.env };
const directories: string[] = [];
const shutdowns: (() => unknown)[] = [];
type BridgeAPI = Parameters<typeof bridge>[0];
type Handler = Parameters<BridgeAPI["on"]>[1];
type Context = Parameters<Handler>[1];
const context = (): Context => ({
	cwd: "/fixture",
	sessionManager: {
		getSessionId: () => "fixture",
		getSessionFile: () => undefined,
	},
	ui: { notify: () => {} },
});
afterEach(() => {
	for (const shutdown of shutdowns.splice(0)) shutdown();
	for (const directory of directories.splice(0))
		rmSync(directory, { recursive: true, force: true });
	for (const key of Object.keys(process.env))
		if (!(key in original)) delete process.env[key];
	Object.assign(process.env, original);
});

function registrationFixture(agent: string, readOnly = false) {
	const directory = mkdtempSync(join(tmpdir(), "panel-fork-bridge-"));
	directories.push(directory);
	const helper = join(directory, "register.py");
	writeFileSync(
		helper,
		'import json, os, sys\nfrom pathlib import Path\nPath(__file__).with_name("registration.json").write_text(sys.stdin.read(), encoding="utf-8")\nPath(__file__).with_name("environment.json").write_text(json.dumps({key: os.environ.get(key) for key in ("HERDR_PLUGIN_STATE_DIR", "HERDR_PANEL_FORK_STATE_DIR")}), encoding="utf-8")\nprint(json.dumps({"status":"registered"}))\n',
	);
	Object.assign(process.env, {
		HERDR_ENV: "1",
		HERDR_PANE_ID: "fixture",
		HERDR_PANEL_FORK_AGENT: agent,
		HERDR_PANEL_FORK_READ_ONLY: readOnly ? "1" : "0",
		HERDR_PANEL_FORK_HELPER: helper,
	});
	const handlers = new Map<string, Handler>();
	const tools: string[][] = [];
	const ctx: Context = {
		...context(),
		model: { provider: "fixture-provider", id: "live-model" },
		sessionManager: {
			getSessionId: () => "child-session",
			getSessionFile: () => "/fixture/sessions/child-session.jsonl",
		},
	};
	bridge({
		on: (name, handler) => {
			handlers.set(name, handler);
		},
		getThinkingLevel: () => "high",
		setActiveTools: async (names) => {
			tools.push(names);
		},
	});
	shutdowns.push(() => handlers.get("session_shutdown")?.({}, ctx));
	return {
		handlers,
		ctx,
		tools,
		payload: () =>
			JSON.parse(readFileSync(join(directory, "registration.json"), "utf8")),
		helperEnvironment: () =>
			JSON.parse(readFileSync(join(directory, "environment.json"), "utf8")),
	};
}

describe("registration readiness", () => {
	function fixture(
		run: NonNullable<NonNullable<Parameters<typeof bridge>[1]>["register"]>,
		now = () => 0,
	) {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_PANEL_FORK_AGENT: "omp",
			HERDR_PANEL_FORK_READ_ONLY: "0",
		});
		const handlers = new Map<string, Handler>();
		const warnings: string[] = [];
		const ctx = {
			...context(),
			ui: { notify: (message: string) => warnings.push(message) },
		};
		bridge(
			{
				on: (name, handler) => {
					handlers.set(name, handler);
				},
				getThinkingLevel: () => "high",
				setActiveTools: async () => {},
			},
			{ register: run, now },
		);
		shutdowns.push(() => handlers.get("session_shutdown")?.({}, ctx));
		return { handlers, warnings, ctx };
	}

	test("OMP registers the current context cwd", async () => {
		const f = registrationFixture("omp");
		f.ctx.cwd = "/fixture/omp";
		await f.handlers.get("session_start")?.({}, f.ctx);
		expect(f.payload().cwd).toBe("/fixture/omp");
	});

	test("normal OMP navigation refreshes the owner from the new context", async () => {
		const payloads: Record<string, unknown>[] = [];
		const f = fixture(async (payload) => {
			payloads.push(payload);
			return { status: "registered" };
		});
		await f.handlers.get("session_start")?.({}, f.ctx);
		const next = {
			...f.ctx,
			cwd: "/fixture/next",
			sessionManager: {
				getSessionId: () => "next-session",
				getSessionFile: () => "/fixture/next/session.jsonl",
			},
		};
		await f.handlers.get("session_switch")?.({}, next);
		await f.handlers.get("agent_end")?.({}, next);
		expect(payloads.map((item) => item.session_id)).toEqual([
			"fixture",
			"next-session",
			"next-session",
		]);
		expect(payloads[1]?.cwd).toBe("/fixture/next");
		expect(payloads.every((item) => item.registration_valid === true)).toBe(
			true,
		);
		expect(f.warnings).toEqual([]);
	});

	test("not-ready startup followed by success does not warn", async () => {
		let count = 0;
		const f = fixture(async () =>
			++count === 1
				? { status: "not_ready", reason: "foreground_not_visible" }
				: { status: "registered" },
		);
		await f.handlers.get("session_start")?.({}, f.ctx);
		await f.handlers.get("agent_end")?.({}, f.ctx);
		expect(count).toBe(2);
		expect(f.warnings).toEqual([]);
	});

	test("persistent unready state warns after the bounded grace", async () => {
		let now = 0;
		const f = fixture(
			async () => ({ status: "not_ready", reason: "foreground_not_visible" }),
			() => now,
		);
		await f.handlers.get("session_start")?.({}, f.ctx);
		now = 10_000;
		await f.handlers.get("agent_end")?.({}, f.ctx);
		await f.handlers.get("agent_end")?.({}, f.ctx);
		expect(f.warnings).toHaveLength(1);
		expect(f.warnings[0]).toContain("candidate_count=0");
	});

	test("duplicate ownership is warned immediately, not retried silently", async () => {
		const f = fixture(async () => {
			throw new Error("candidate_count=2 candidate_pids=[100,200]");
		});
		await f.handlers.get("session_start")?.({}, f.ctx);
		expect(f.warnings).toHaveLength(1);
		expect(f.warnings[0]).toContain("candidate_count=2");
	});

	test("a nested non-owner stops reporting without a warning", async () => {
		let calls = 0;
		const f = fixture(async () => {
			calls++;
			return { status: "ignored", reason: "nested_non_owner" };
		});
		await f.handlers.get("session_start")?.({}, f.ctx);
		await f.handlers.get("agent_end")?.({}, f.ctx);
		expect(calls).toBe(1);
		expect(f.warnings).toEqual([]);
	});

	test("shutdown prevents lifecycle events from resurrecting registration", async () => {
		let calls = 0;
		const f = fixture(async () => {
			calls++;
			return { status: "registered" };
		});
		await f.handlers.get("session_start")?.({}, f.ctx);
		await f.handlers.get("session_shutdown")?.({}, f.ctx);
		await f.handlers.get("agent_end")?.({}, f.ctx);
		expect(calls).toBe(1);
	});
});

describe("OMP product identity", () => {
	test("a bare Bun runtime can use an explicit OMP identity", () => {
		expect(
			runtimeAgent({ HERDR_PANEL_FORK_AGENT: "omp" }, "/usr/bin/bun", ["bun"]),
		).toBe("omp");
	});
	test("an OMP executable identifies the product", () => {
		expect(runtimeAgent({}, "/opt/bin/omp", ["omp"])).toBe("omp");
		expect(runtimeAgent({}, "/usr/bin/bun", ["omp"])).toBe("omp");
		expect(runtimeAgent({}, "/opt/bin/omp.exe", ["omp.exe"])).toBe("omp");
	});
	test("an explicit unknown product fails closed", () => {
		for (const explicit of ["unknown", "pi", ""])
			expect(
				runtimeAgent({ HERDR_PANEL_FORK_AGENT: explicit }, "/opt/bin/omp", [
					"omp",
				]),
			).toBeUndefined();
	});
	test("unknown identities never silently register as OMP", () => {
		for (const environment of [{}, { HERDR_AGENT: "pi" }])
			expect(
				runtimeAgent(environment, "/usr/bin/bun", ["bun", "/fixture/cli.js"]),
			).toBeUndefined();
		expect(
			runtimeAgent({}, "/usr/bin/bun", ["bun", "/fixture/cli.js", "omp"]),
		).toBeUndefined();
		expect(runtimeAgent({}, "/usr/bin/bun", ["bun", "omp"])).toBeUndefined();
	});
	test("an unidentified runtime warns without registering or changing tools", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_PANEL_FORK_AGENT: "unknown",
		});
		const handlers = new Map<string, Handler>();
		const warnings: string[] = [];
		let changedTools = false;
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {
				changedTools = true;
			},
		});
		expect([...handlers.keys()]).toEqual(["session_start"]);
		await handlers.get("session_start")?.(
			{},
			{
				...context(),
				ui: {
					notify: (message) => {
						warnings.push(message);
					},
				},
			},
		);
		expect(warnings[0]).toContain("OMP runtime could not be identified");
		expect(changedTools).toBe(false);
	});
});

describe("OMP owner registration", () => {
	test("an unauthenticated placeholder model is not relayed as a real selector", async () => {
		const fixture = registrationFixture("omp");
		await fixture.handlers.get("session_start")?.(
			{},
			{
				...fixture.ctx,
				model: { provider: "unknown", id: "unknown" },
			},
		);
		expect(fixture.payload().model).toBeUndefined();
	});
	test("the helper uses the parent's pinned root over an inherited plugin root", async () => {
		const fixture = registrationFixture("omp");
		process.env.HERDR_PANEL_FORK_STATE_DIR = "/parent/pinned-state";
		process.env.HERDR_PLUGIN_STATE_DIR = "/shell/conflicting-state";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperEnvironment()).toEqual({
			HERDR_PANEL_FORK_STATE_DIR: "/parent/pinned-state",
			HERDR_PLUGIN_STATE_DIR: "/parent/pinned-state",
		});
		expect(process.env.HERDR_PLUGIN_STATE_DIR).toBe("/shell/conflicting-state");
	});
	test("the helper keeps the existing plugin root when no parent root is pinned", async () => {
		const fixture = registrationFixture("omp");
		delete process.env.HERDR_PANEL_FORK_STATE_DIR;
		process.env.HERDR_PLUGIN_STATE_DIR = "/shell/existing-state";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperEnvironment()).toEqual({
			HERDR_PANEL_FORK_STATE_DIR: null,
			HERDR_PLUGIN_STATE_DIR: "/shell/existing-state",
		});
	});
	for (const [agent, keys] of [
		["omp", ["PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"]],
	] as const) {
		test(`${agent} registers live settings and configuration without restricting normal mode`, async () => {
			const fixture = registrationFixture(agent);
			for (const key of [
				...keys,
				"XDG_CONFIG_HOME",
				"XDG_DATA_HOME",
				"XDG_STATE_HOME",
				"XDG_CACHE_HOME",
			])
				process.env[key] = `/source/${key}`;
			process.env.PANEL_FORK_TEST_API_KEY = "not-relayed";
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			const payload = fixture.payload();
			expect(payload.agent).toBe(agent);
			expect(payload.session_id).toBe("child-session");
			expect(payload.session_file).toBe(
				"/fixture/sessions/child-session.jsonl",
			);
			expect(payload.model).toBe("fixture-provider/live-model");
			expect(payload.thinking).toBe("high");
			expect(payload.read_only_ready).toBe(true);
			expect(payload.registration_valid).toBe(true);
			for (const key of keys)
				expect(payload.environment[key]).toBe(`/source/${key}`);
			expect(payload.environment.XDG_STATE_HOME).toBe("/source/XDG_STATE_HOME");
			expect(payload.environment.PANEL_FORK_TEST_API_KEY).toBeUndefined();
			expect(fixture.tools).toEqual([]);
			for (const toolName of ["write", "bash", "goal", "task"])
				expect(
					await fixture.handlers.get("tool_call")?.({ toolName }, fixture.ctx),
				).toBeUndefined();
			for (const name of [
				"session_before_switch",
				"session_before_branch",
				"session_before_tree",
			])
				expect(
					await fixture.handlers.get(name)?.({}, fixture.ctx),
				).toBeUndefined();
			expect(
				await fixture.handlers.get("before_agent_start")?.(
					{ systemPrompt: "Original prompt" },
					fixture.ctx,
				),
			).toBeUndefined();
			process.env[keys[0]] = "/updated/config-root";
			await fixture.handlers.get("agent_end")?.(
				{},
				{
					...fixture.ctx,
					model: { provider: "updated", id: "model" },
				},
			);
			expect(fixture.payload().environment[keys[0]]).toBe(
				"/updated/config-root",
			);
			expect(fixture.payload().model).toBe("updated/model");
		});
	}
	for (const agent of ["omp"]) {
		test(`${agent} retains its verified read-only runtime gates`, async () => {
			const fixture = registrationFixture(agent, true);
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			expect(fixture.payload().read_only_ready).toBe(true);
			expect(fixture.tools).toEqual([["read", "grep", "glob"]]);
			expect(
				await fixture.handlers.get("tool_call")?.(
					{ toolName: "bash" },
					fixture.ctx,
				),
			).toMatchObject({ block: true });
			expect(
				await fixture.handlers.get("tool_call")?.(
					{ toolName: "read" },
					fixture.ctx,
				),
			).toBeUndefined();
			expect(
				await fixture.handlers.get("session_before_switch")?.({}, fixture.ctx),
			).toEqual({ cancel: true });
			const result = (await fixture.handlers.get("before_agent_start")?.(
				{ systemPrompt: "Original prompt" },
				fixture.ctx,
			)) as { systemPrompt: string };
			expect(result.systemPrompt).toStartWith("Original prompt\n\n");
			expect(result.systemPrompt).toContain("Only read tools are permitted");
		});
	}
});

describe("optional read-only tool boundary", () => {
	for (const name of ["read", "grep", "glob"]) {
		test(`allows ${name}`, () =>
			expect(readOnlyGuard({ toolName: name })).toBeUndefined());
	}
	for (const name of [
		"search",
		"find",
		"ast_grep",
		"goal",
		"write",
		"bash",
		"edit",
		"task",
		"irc",
		"mcp__service_send",
		"search_tool_bm25",
		"eval",
		"retain",
		"new_tool",
		"",
	]) {
		test(`blocks ${name || "missing tool name"}`, () =>
			expect(readOnlyGuard({ toolName: name })?.block).toBe(true));
	}
	test("does nothing outside Herdr", () => {
		delete process.env.HERDR_ENV;
		const handlers: string[] = [];
		bridge({
			on: (name) => {
				handlers.push(name);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		expect(handlers).toEqual([]);
	});
	test("read-only forks cancel all in-process identity navigation", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_PANEL_FORK_AGENT: "omp",
			HERDR_PANEL_FORK_READ_ONLY: "1",
		});
		const handlers = new Map<string, Handler>();
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		for (const name of [
			"session_before_switch",
			"session_before_branch",
			"session_before_tree",
		]) {
			expect(await handlers.get(name)?.({}, context())).toEqual({
				cancel: true,
			});
		}
	});
	test("normal Panel Fork allows mutations and session navigation", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_PANEL_FORK_AGENT: "omp",
			HERDR_PANEL_FORK_READ_ONLY: "0",
		});
		const handlers = new Map<string, Handler>();
		let changedTools = false;
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {
				changedTools = true;
			},
		});
		for (const toolName of ["write", "edit", "bash", "goal", "task"]) {
			expect(
				await handlers.get("tool_call")?.({ toolName }, context()),
			).toBeUndefined();
		}
		for (const name of [
			"session_before_switch",
			"session_before_branch",
			"session_before_tree",
		]) {
			expect(await handlers.get(name)?.({}, context())).toBeUndefined();
		}
		expect(changedTools).toBe(false);
	});
});
