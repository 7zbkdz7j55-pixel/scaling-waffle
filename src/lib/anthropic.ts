import Anthropic from "@anthropic-ai/sdk";
import type { Env } from "../env";
import { ulid } from "./ids";

/**
 * Per-1M-token USD rates, used only for the model_calls cost ledger so unit
 * margin per bid is visible from day one. Keep in sync with pricing; this is
 * an estimate for observability, not billing.
 */
const RATES: Record<string, { input: number; output: number; cacheRead: number }> = {
  "claude-opus-5": { input: 5, output: 25, cacheRead: 0.5 },
  "claude-sonnet-5": { input: 2, output: 10, cacheRead: 0.2 },
};

function estimateCostUsd(model: string, u: Anthropic.Usage): number {
  const rate = RATES[model] ?? RATES["claude-sonnet-5"];
  const input = u.input_tokens ?? 0;
  const output = u.output_tokens ?? 0;
  const cacheRead = u.cache_read_input_tokens ?? 0;
  const cacheWrite = u.cache_creation_input_tokens ?? 0;
  return (
    (input / 1e6) * rate.input +
    (output / 1e6) * rate.output +
    (cacheRead / 1e6) * rate.cacheRead +
    (cacheWrite / 1e6) * rate.input * 1.25
  );
}

/** A single tool call the agent emitted, with its already-parsed input. */
export interface EmittedCall {
  name: string;
  input: Record<string, unknown>;
}

export interface RunAgentOptions {
  env: Env;
  oppId: string | null;
  /** Label for the model_calls ledger, e.g. 'shredder'. */
  agent: string;
  model: string;
  system: Anthropic.TextBlockParam[] | string;
  tools: Anthropic.Tool[];
  userContent: string;
  /** Adaptive thinking + effort. Judgment agents want high effort. */
  effort?: "low" | "medium" | "high" | "xhigh" | "max";
  /** Safety valve on loop turns. Emit-style agents rarely need more than 3. */
  maxTurns?: number;
}

export interface RunAgentResult {
  calls: EmittedCall[];
  stopReason: string | null;
  turns: number;
}

/**
 * Runs an "emit"-style agent: the model reports structured data by calling its
 * emit_* tools (possibly many times, in parallel), and we acknowledge each call
 * and loop until it stops. We never execute anything on the model's behalf here
 * — the tool calls ARE the output. Deterministic work and persistence happen in
 * the caller (the workflow step), which is where durability lives.
 */
export async function runAgent(opts: RunAgentOptions): Promise<RunAgentResult> {
  const client = new Anthropic({ apiKey: opts.env.ANTHROPIC_API_KEY });
  const messages: Anthropic.MessageParam[] = [
    { role: "user", content: opts.userContent },
  ];

  const calls: EmittedCall[] = [];
  const maxTurns = opts.maxTurns ?? 6;
  let stopReason: string | null = null;
  let turns = 0;

  while (turns < maxTurns) {
    turns++;
    const response = await client.messages.create({
      model: opts.model,
      max_tokens: 16000,
      thinking: { type: "adaptive" },
      output_config: { effort: opts.effort ?? "high" },
      system: opts.system,
      tools: opts.tools,
      messages,
    });

    await logModelCall(opts.env, opts.oppId, opts.agent, opts.model, response.usage);

    stopReason = response.stop_reason;

    if (response.stop_reason === "pause_turn") {
      messages.push({ role: "assistant", content: response.content });
      continue;
    }

    const toolUses = response.content.filter(
      (b): b is Anthropic.ToolUseBlock => b.type === "tool_use",
    );

    if (toolUses.length === 0) break; // end_turn, refusal, max_tokens — nothing more to collect

    messages.push({ role: "assistant", content: response.content });

    const results: Anthropic.ToolResultBlockParam[] = [];
    for (const tu of toolUses) {
      calls.push({ name: tu.name, input: (tu.input ?? {}) as Record<string, unknown> });
      results.push({
        type: "tool_result",
        tool_use_id: tu.id,
        content: "recorded",
      });
    }
    messages.push({ role: "user", content: results });

    if (response.stop_reason !== "tool_use") break;
  }

  return { calls, stopReason, turns };
}

async function logModelCall(
  env: Env,
  oppId: string | null,
  agent: string,
  model: string,
  usage: Anthropic.Usage,
): Promise<void> {
  try {
    await env.DB.prepare(
      `INSERT INTO model_calls
         (id, opp_id, agent, model, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, est_cost_usd, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?)`,
    )
      .bind(
        ulid(),
        oppId,
        agent,
        model,
        usage.input_tokens ?? 0,
        usage.output_tokens ?? 0,
        usage.cache_read_input_tokens ?? 0,
        usage.cache_creation_input_tokens ?? 0,
        estimateCostUsd(model, usage),
        Date.now(),
      )
      .run();
  } catch {
    // Ledger is observability, never on the critical path. Swallow failures.
  }
}
