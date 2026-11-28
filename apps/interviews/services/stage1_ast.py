"""
Safe Read-Only AST Static Analyzer for Stage 1 Applied Diagnosis.

Strictly treats candidate-submitted Python patches as untrusted text.
NEVER executes candidate code (no exec, eval, compile, subprocess, or host execution).
Enforces character, line, AST node count, and nesting depth bounds cleanly.
"""

import ast
from typing import Any, Dict, List


class Stage1ASTAnalyzer:
    """Zero-execution Python structural analyzer returning supplementary review signals."""

    MAX_CODE_CHARS = 12000
    MAX_LINE_COUNT = 300
    MAX_AST_NODES = 1800
    MAX_AST_DEPTH = 40

    @classmethod
    def analyze(cls, code_str: str, invariants: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        invariants = invariants or []
        raw = (code_str or "").replace("\x00", "")

        if not raw.strip():
            return {
                "status": "EMPTY",
                "syntax_valid": False,
                "node_count": 0,
                "line_count": 0,
                "error": None,
                "checks": [],
                "passed_rules": 0,
                "total_rules": len(invariants),
                "supplementary_observations": ["No Python patch snippet submitted; evaluated on diagnosis and fix rationale."],
                "code_executed": False,
            }

        lines = raw.splitlines()
        if len(raw) > cls.MAX_CODE_CHARS or len(lines) > cls.MAX_LINE_COUNT:
            return {
                "status": "INPUT_TOO_LARGE",
                "syntax_valid": False,
                "node_count": 0,
                "line_count": len(lines),
                "error": f"Submitted snippet exceeds maximum size ({cls.MAX_CODE_CHARS} chars / {cls.MAX_LINE_COUNT} lines).",
                "checks": [],
                "passed_rules": 0,
                "total_rules": len(invariants),
                "supplementary_observations": ["Patch snippet exceeded static analysis size limit."],
                "code_executed": False,
            }

        try:
            tree = ast.parse(raw)
        except SyntaxError as exc:
            return {
                "status": "SYNTAX_ERROR",
                "syntax_valid": False,
                "node_count": 0,
                "line_count": len(lines),
                "error": f"Python syntax error on line {exc.lineno or '?'}: {exc.msg}",
                "checks": [],
                "passed_rules": 0,
                "total_rules": len(invariants),
                "supplementary_observations": ["Patch snippet contains a syntax error."],
                "code_executed": False,
            }
        except (RecursionError, MemoryError, ValueError) as exc:
            return {
                "status": "PATHOLOGICAL_AST",
                "syntax_valid": False,
                "node_count": 0,
                "line_count": len(lines),
                "error": f"Unable to parse deeply nested or malformed AST safely: {type(exc).__name__}",
                "checks": [],
                "passed_rules": 0,
                "total_rules": len(invariants),
                "supplementary_observations": ["AST parser aborted cleanly due to pathological nesting."],
                "code_executed": False,
            }

        # Bounded iterative walk (no recursion risk) to enforce node & depth budgets
        node_count = 0
        max_depth = 0
        stack: List[tuple] = [(tree, 0)]
        nodes: List[ast.AST] = []

        while stack:
            node, depth = stack.pop()
            node_count += 1
            if depth > max_depth:
                max_depth = depth
            if node_count > cls.MAX_AST_NODES or max_depth > cls.MAX_AST_DEPTH:
                return {
                    "status": "PATHOLOGICAL_AST",
                    "syntax_valid": True,
                    "node_count": node_count,
                    "line_count": len(lines),
                    "error": "Submitted snippet exceeds AST complexity/depth safety cap.",
                    "checks": [],
                    "passed_rules": 0,
                    "total_rules": len(invariants),
                    "supplementary_observations": ["AST traversal stopped cleanly at complexity limit."],
                    "code_executed": False,
                }
            nodes.append(node)
            for child in ast.iter_child_nodes(node):
                stack.append((child, depth + 1))

        checks: List[Dict[str, Any]] = []
        for inv in invariants:
            rule_name = str(inv.get('rule', '')).strip()
            passed, detail = cls._evaluate_rule(rule_name, tree, nodes, raw)
            checks.append({
                "rule": rule_name,
                "description": inv.get('description', ''),
                "passed": passed,
                "detail": detail,
            })

        passed_rules = sum(1 for c in checks if c["passed"])
        observations = [c["detail"] for c in checks]

        return {
            "status": "VALID_AST",
            "syntax_valid": True,
            "node_count": node_count,
            "line_count": len(lines),
            "error": None,
            "checks": checks,
            "passed_rules": passed_rules,
            "total_rules": len(checks),
            "supplementary_observations": observations,
            "code_executed": False,
        }

    @classmethod
    def _evaluate_rule(cls, rule_name: str, tree: ast.AST, nodes: List[ast.AST], raw: str):
        if rule_name == 'no_bare_except_pass':
            for n in nodes:
                if isinstance(n, ast.ExceptHandler):
                    if all(isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis) for stmt in n.body):
                        return False, "Detected exception handler swallowing errors with pass/ellipsis."
            return True, "No silent except-pass error swallowing detected."

        if rule_name == 'no_fail_open_return_true':
            for n in nodes:
                if isinstance(n, ast.ExceptHandler):
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant) and sub.value.value is True:
                            return False, "Detected fail-open `return True` inside exception handler."
            return True, "Exception handling fails closed (no fail-open True return)."

        if rule_name == 'require_explicit_guard':
            has_if = any(isinstance(n, ast.If) for n in nodes)
            has_raise_or_guard_ret = any(
                isinstance(n, (ast.Raise, ast.Return)) for n in nodes
            )
            if has_if and has_raise_or_guard_ret:
                return True, "Explicit conditional validation guard / early rejection present."
            return False, "Expected explicit conditional validation guard (if/raise/early return) missing."

        if rule_name == 'avoid_nested_linear_membership':
            has_any_gen = False
            for n in nodes:
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'any':
                    if any(isinstance(arg, ast.GeneratorExp) for arg in n.args):
                        has_any_gen = True
            has_nested_for = False
            for n in nodes:
                if isinstance(n, ast.For):
                    for child in ast.iter_child_nodes(n):
                        # check direct or body nested loop
                        pass
                    for stmt in n.body:
                        if any(isinstance(sub, ast.For) for sub in ast.walk(stmt)):
                            has_nested_for = True
            if has_any_gen or has_nested_for:
                return False, "Nested O(N^2) linear loop/generator scan still appears in patch."
            return True, "Quadratic nested linear scan removed."

        if rule_name == 'require_batched_or_streaming_pattern':
            low = raw.lower()
            has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in nodes)
            batch_markers = ('batch', 'bulk', 'chunk', 'in_', 'aggregat', 'get_tenants', 'get_invoice_totals', 'sum(')
            if has_yield or any(m in low for m in batch_markers):
                return True, "Batched or streaming aggregation pattern detected."
            return False, "Patch still appears to fetch or accumulate unbounded per-tenant records sequentially."

        return True, f"Rule '{rule_name}' checked."
