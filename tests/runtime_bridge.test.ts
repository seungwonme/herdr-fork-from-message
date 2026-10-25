import { afterEach, describe, expect, test } from "bun:test";
import bridge, { questionGuard } from "../scripts/runtime_bridge";

const original = { ...process.env };
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
	for (const key of Object.keys(process.env))
		if (!(key in original)) delete process.env[key];
	Object.assign(process.env, original);
});

describe("question tool boundary", () => {
	for (const name of ["read", "search", "find", "ast_grep", "grep", "glob"]) {
		test(`allows ${name}`, () =>
			expect(questionGuard({ toolName: name })).toBeUndefined());
	}
	for (const name of [
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
			expect(questionGuard({ toolName: name })?.block).toBe(true));
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
	test("GJC refuses a question runtime without authoritative pause control", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_QUESTION_AGENT: "gjc",
			HERDR_QUESTION_FORK: "1",
		});
		const handlers = new Map<string, Handler>();
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		await expect(
			handlers.get("session_start")?.({}, context()),
		).rejects.toThrow("goal pause gate");
	});
	test("in-process GJC navigation stops future registration instead of trusting stale context", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_QUESTION_AGENT: "gjc",
		});
		delete process.env.HERDR_QUESTION_FORK;
		const handlers = new Map<string, Handler>();
		const warnings: string[] = [];
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		await handlers.get("session_switch")?.(
			{},
			{
				...context(),
				ui: { notify: (message: string) => warnings.push(message) },
			},
		);
		expect(warnings[0]).toContain("invalidated");
		await handlers.get("session_shutdown")?.({}, context());
	});
	test("question forks cancel all in-process identity navigation", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_QUESTION_AGENT: "gjc",
			HERDR_QUESTION_FORK: "1",
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
});
