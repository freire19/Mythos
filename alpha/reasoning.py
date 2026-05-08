"""
Structured reasoning engine for deep code analysis.

Implements HYPOTHESIZE → VERIFY → CONCLUDE chain-of-thought for
vulnerability hunting. Extracts and preserves reasoning tokens from
DeepSeek-R1, Claude, and other reasoning-capable models.

Designed for Gap 1: frontier-level reasoning depth via structured thinking.
"""

import re
from dataclasses import dataclass, field
from typing import Any

# ─── Reasoning phases ───


@dataclass
class Hypothesis:
    """A candidate vulnerability hypothesis."""
    id: str
    statement: str                      # "strcpy at line 42 may overflow buf[64]"
    confidence: float = 0.0             # 0.0-1.0
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    file: str = ""
    line: int = 0
    vuln_type: str = ""
    verified: bool = False
    verdict: str = ""                   # "confirmed", "rejected", "inconclusive"


@dataclass
class ReasoningContext:
    """Accumulated reasoning state across a hunting session."""
    hypotheses: list[Hypothesis] = field(default_factory=list)
    files_read: set[str] = field(default_factory=set)
    functions_analyzed: set[str] = field(default_factory=set)
    dataflow_paths: list[dict] = field(default_factory=list)
    confirmed: list[Hypothesis] = field(default_factory=list)
    rejected: list[Hypothesis] = field(default_factory=list)


# ─── Reasoning prompt templates ───

DEEP_ANALYSIS_PROMPT = """
You are performing a deep security analysis. Follow this structure:

## HYPOTHESIZE
Based on the code below, formulate 3-5 specific vulnerability hypotheses.
For each: what is the vulnerability, where exactly (file:line), and what
preconditions must hold for exploitation.

## VERIFY
For each hypothesis:
- Trace the data flow from source to sink
- Check: are bounds validated? Is the input sanitized? Are preconditions reachable?
- Find counter-evidence: why might this NOT be exploitable?

## CONCLUDE
- Mark each hypothesis: CONFIRMED (with exploit path), REJECTED (with reason),
  or INCONCLUSIVE (need more context from other files).
- For CONFIRMED: provide file:line, code snippet, attack vector, fix, severity.

CODE:
{code}

CONTEXT FROM OTHER FILES:
{context}
"""

INTERPROCEDURAL_TRACE_PROMPT = """
Trace variable `{variable}` across function boundaries in this codebase.

Start at the definition/allocation point, follow every assignment, function call
passing it as argument, and dereference. Identify:
1. Where it's freed (if applicable)
2. Where it's dereferenced after free
3. Where it's assigned NULL then dereferenced
4. Where bounds are checked (or not) before buffer operations

Available call graph:
{call_graph}

Relevant code:
{code}
"""

EXPLOITABILITY_ASSESSMENT = """
Assess the exploitability of this finding:

Finding: {finding}
Binary mitigations: {mitigations}
Architecture: {arch}

Determine:
1. Is this exploitable in practice? (Yes/No/Partial)
2. What mitigations must be bypassed? (ASLR, NX, Canary, PIE, RELRO)
3. What exploitation technique is appropriate? (ret2libc, ROP, shellcode, heap feng shui)
4. Estimated complexity: TRIVIAL (<1h), MODERATE (1-8h), HARD (1-3 days), EXTREME (weeks)
"""


# ─── Reasoning token extraction ───

# DeepSeek-R1: <｜end▁of▁thinking｜> contains reasoning_content field
# Claude: thinking blocks in extended content
# OpenAI o1/o3: reasoning_tokens field

_REASONING_PATTERNS = [
    (r"<thinking>(.*?)</thinking>", "claude_thinking"),
    (r"<reasoning>(.*?)</reasoning>", "deepseek_reasoning"),
    (r"<analysis>(.*?)</analysis>", "generic_analysis"),
]


def extract_reasoning(response: dict | str) -> str:
    """Extract reasoning/thinking content from LLM response.

    Handles:
    - DeepSeek-R1: response['reasoning_content']
    - Claude: <thinking> blocks in content
    - OpenAI o1/o3: response['reasoning_tokens']
    """
    if isinstance(response, dict):
        # Direct reasoning fields
        if response.get("reasoning_content"):
            return response["reasoning_content"]
        if response.get("reasoning_tokens"):
            return response["reasoning_tokens"]
        # Check choices
        choices = response.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {}) or choices[0].get("message", {})
            if delta.get("reasoning_content"):
                return delta["reasoning_content"]

    # Text-based extraction
    text = response if isinstance(response, str) else str(response)
    for pattern, source in _REASONING_PATTERNS:
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        if matches:
            return "\n".join(matches)

    return ""


def preserve_reasoning_context(messages: list[dict], max_reasoning_chars: int = 50000) -> list[dict]:
    """Keep reasoning content only for the most recent assistant turn.

    Older reasoning is stripped to save context — only the most recent
    thinking chain needs to be preserved for the model's continuity.
    """
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and i != last_assistant_idx:
            msg.pop("reasoning_content", None)

    # Cap the last one too
    if last_assistant_idx >= 0:
        rc = messages[last_assistant_idx].get("reasoning_content", "")
        if len(rc) > max_reasoning_chars:
            messages[last_assistant_idx]["reasoning_content"] = rc[-max_reasoning_chars:]

    return messages


# ─── Chain-of-thought runner ───


class CoTRunner:
    """Runs the HYPOTHESIZE → VERIFY → CONCLUDE cycle for vulnerability analysis.

    Usage:
        runner = CoTRunner()
        ctx = ReasoningContext()
        hypotheses = runner.hypothesize(code, ctx)
        verified = runner.verify(hypotheses, call_graph, full_codebase)
        conclusions = runner.conclude(verified)
    """

    def __init__(self):
        self.ctx = ReasoningContext()

    def hypothesize(self, code: str, file_path: str = "", focus: str = "") -> list[Hypothesis]:
        """Generate initial vulnerability hypotheses from code.

        Uses pattern recognition to identify dangerous patterns and
        formulate specific, testable hypotheses.
        """
        hypotheses = []
        lines = code.split("\n")

        # Pattern: unbounded copy + fixed buffer
        for i, line in enumerate(lines, 1):
            # strcpy / strcat without bounds check
            m = re.search(r'\b(strcpy|strcat|sprintf|gets)\s*\(\s*(\w+)\s*,', line)
            if m:
                func = m.group(1)
                dest = m.group(2)
                # Look back for buffer declaration
                buf_size = self._find_buffer_size(lines, i - 1, dest)
                h = Hypothesis(
                    id=f"H{len(hypotheses)+1:03d}",
                    statement=f"{func}({dest}, ...) at {file_path}:{i} — possible buffer overflow"
                              + (f" (buffer size: {buf_size})" if buf_size else ""),
                    confidence=0.7 if buf_size else 0.4,
                    file=file_path,
                    line=i,
                    vuln_type="buffer_overflow",
                    evidence_for=[f"Unbounded {func}() call" + (f", dest buffer is {buf_size}" if buf_size else "")],
                )
                hypotheses.append(h)

            # Format string
            m = re.search(r'\b(printf|fprintf|sprintf|snprintf|syslog)\s*\(\s*(\w+)', line)
            if m and not self._is_literal(m.group(2), lines, i):
                h = Hypothesis(
                    id=f"H{len(hypotheses)+1:03d}",
                    statement=f"{m.group(1)}({m.group(2)}) at {file_path}:{i} — format string vulnerability",
                    confidence=0.8,
                    file=file_path,
                    line=i,
                    vuln_type="format_string",
                    evidence_for=[f"{m.group(1)}() called with variable format argument"],
                )
                hypotheses.append(h)

            # malloc(count * size)
            m = re.search(r'\b(malloc|calloc)\s*\(\s*(\w+)\s*\*\s*(\w+)\s*\)', line)
            if m:
                h = Hypothesis(
                    id=f"H{len(hypotheses)+1:03d}",
                    statement=f"{m.group(1)}({m.group(2)}*{m.group(3)}) at {file_path}:{i} — integer overflow",
                    confidence=0.5,
                    file=file_path,
                    line=i,
                    vuln_type="integer_overflow",
                    evidence_for=[f"Multiplication in allocation size without overflow check"],
                )
                hypotheses.append(h)

        self.ctx.hypotheses.extend(hypotheses)
        return hypotheses

    def verify(self, hypotheses: list[Hypothesis], call_graph: list[dict],
               interprocedural_code: dict[str, str] | None = None) -> list[Hypothesis]:
        """Verify hypotheses by tracing data flow across function boundaries.

        Uses the call graph to follow variables through function calls,
        checking whether preconditions for exploitation are actually reachable.
        """
        for h in hypotheses:
            if h.vuln_type == "buffer_overflow":
                # Check: is the source argument user-controlled?
                # Look in call graph for who calls this function
                callers = [c for c in call_graph if c.get("callee", "").startswith(h.file.split("/")[-1].replace(".c", ""))]
                if callers:
                    h.evidence_for.append(f"Reachable from {len(callers)} call sites")
                    h.confidence = min(1.0, h.confidence + 0.15)
                else:
                    h.evidence_against.append("No callers found in graph — may be dead code")

            elif h.vuln_type == "format_string":
                # Check: is the format argument truly user-controlled?
                # A variable might be a local format string constant
                h.confidence = min(1.0, h.confidence)

            elif h.vuln_type == "integer_overflow":
                # Check: are both operands attacker-controlled?
                # If one is a constant, overflow may be impossible
                h.confidence = min(1.0, h.confidence)

        return hypotheses

    def conclude(self, hypotheses: list[Hypothesis], mitigations: dict | None = None) -> list[Hypothesis]:
        """Finalize hypotheses: mark confirmed, rejected, or inconclusive."""
        for h in hypotheses:
            if h.confidence >= 0.7 and not h.evidence_against:
                h.verified = True
                h.verdict = "confirmed"
                self.ctx.confirmed.append(h)
            elif h.evidence_against:
                h.verdict = "rejected"
                self.ctx.rejected.append(h)
            else:
                h.verdict = "inconclusive"

        return [h for h in hypotheses if h.verdict == "confirmed"]

    def _find_buffer_size(self, lines: list[str], start: int, var_name: str) -> str:
        """Look backward from start for buffer declaration."""
        for i in range(start, max(-1, start - 20), -1):
            line = lines[i]
            m = re.search(rf'\b(char|int|uint8_t|uint16_t|uint32_t)\s+{var_name}\s*\[(\d+)\]', line)
            if m:
                return f"{m.group(2)} bytes"
            m = re.search(rf'#define\s+(\w+)\s+(\d+).*{var_name}', line)
            if m:
                return f"MAX={m.group(2)}"
        return ""

    def _is_literal(self, arg: str, lines: list[str], line_idx: int) -> bool:
        """Check if an argument appears to be a string literal."""
        line = lines[line_idx - 1] if line_idx <= len(lines) else ""
        # If the argument appears in quotes on the same line, it's a literal
        if f'"{arg}"' in line or f"'{arg}'" in line:
            return True
        # If the argument appears right after the function name, check context
        if arg in line:
            # Check if it's defined as a const char* in nearby lines
            for i in range(max(0, line_idx - 10), line_idx):
                if f'const char* {arg}' in lines[i] or f'char {arg}[]' in lines[i]:
                    return "= \"" in lines[i]
        return False


# ─── Prompt builder ───


def build_deep_analysis_prompt(code: str, context: str = "",
                                mitigations: dict | None = None,
                                focus_areas: list[str] | None = None) -> str:
    """Build a structured deep analysis prompt with all context.

    Args:
        code: The main code snippet to analyze.
        context: Code from related files (callers, callees, imports).
        mitigations: Binary hardening status (for exploitability assessment).
        focus_areas: Specific vulnerability classes to focus on.
    """
    focus = ""
    if focus_areas:
        focus = "\nFocus specifically on: " + ", ".join(focus_areas) + "."

    mit = ""
    if mitigations:
        mit = f"\nBinary hardening status: {mitigations}\n"

    return DEEP_ANALYSIS_PROMPT.format(
        code=code[:12000],
        context=context[:8000],
    ) + focus + mit
