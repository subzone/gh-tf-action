#!/usr/bin/env python3
"""
gh-tf-action — GitHub Action for Terraform
Runs Terraform commands and generates rich plan visualization in GitHub Step Summary,
posts PR comments, and uploads plan artifacts. Ported from ado-tf-agent.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── Input helpers ─────────────────────────────────────────────────────────────

def _inp(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", default).strip()

def _bool(name: str, default: bool = False) -> bool:
    v = _inp(name).lower()
    if not v:
        return default
    return v in ("true", "1", "yes")

COMMAND         = _inp("command", "plan")
WORKING_DIR     = _inp("working_directory", ".")
BACKEND_TYPE    = _inp("backend_type", "local")
PLAN_FILE       = _inp("plan_file", "tfplan") or "tfplan"
EXTRA_ARGS      = _inp("additional_args")
AUTO_APPROVE    = _bool("auto_approve")
POST_SUMMARY    = _bool("post_step_summary", True)
POST_COMMENT    = _bool("post_pr_comment", True)
UPLOAD_ARTIFACT = _bool("upload_plan_artifact", True)
ARTIFACT_NAME   = _inp("artifact_name", "terraform-plan") or "terraform-plan"
GITHUB_TOKEN    = os.environ.get("INPUT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

# GitHub context
GITHUB_OUTPUT       = os.environ.get("GITHUB_OUTPUT", "")
GITHUB_STEP_SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "")
GITHUB_WORKSPACE    = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
GITHUB_REPOSITORY   = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME     = os.environ.get("GITHUB_REF_NAME", "")
GITHUB_SHA          = os.environ.get("GITHUB_SHA", "")
GITHUB_RUN_ID       = os.environ.get("GITHUB_RUN_ID", "")
GITHUB_SERVER_URL   = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
GITHUB_API_URL      = os.environ.get("GITHUB_API_URL", "https://api.github.com")
PR_NUMBER           = os.environ.get("GITHUB_EVENT_PULL_REQUEST_NUMBER", "") or \
                      os.environ.get("PR_NUMBER", "")

# ── Colours ───────────────────────────────────────────────────────────────────

RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
GREEN  = "\033[0;32m"
CYAN   = "\033[0;36m"
RESET  = "\033[0m"


def log(msg: str) -> None:
    print(f"{CYAN}[tf-action]{RESET} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{GREEN}[tf-action] \u2713{RESET} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{YELLOW}[tf-action] \u26a0{RESET} {msg}", flush=True)


def err(msg: str) -> None:
    print(f"{RED}[tf-action] \u2717{RESET} {msg}", file=sys.stderr, flush=True)

# ── Working directory ─────────────────────────────────────────────────────────

def resolve_working_dir() -> Path:
    wd = WORKING_DIR if WORKING_DIR else "."
    base = Path(GITHUB_WORKSPACE)
    resolved = (base / wd).resolve()
    # path-traversal guard
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise SystemExit(f"Path traversal detected: working-directory '{wd}' escapes workspace.")
    if not resolved.exists():
        raise SystemExit(f"Working directory does not exist: {resolved}")
    return resolved

# ── Arg parsing ───────────────────────────────────────────────────────────────

def parse_extra_args(raw: str) -> list[str]:
    if not raw.strip():
        return []
    parts: list[str] = []
    current = ""
    in_quote: str | None = None
    for c in raw:
        if in_quote:
            if c == in_quote:
                in_quote = None
            else:
                current += c
            continue
        if c in ('"', "'"):
            in_quote = c
            continue
        if c.isspace():
            if current:
                parts.append(current)
                current = ""
            continue
        current += c
    if current:
        parts.append(current)
    return parts

# ── Backend config args ───────────────────────────────────────────────────────

def backend_args(cwd: Path) -> list[str]:
    bt = BACKEND_TYPE.lower()
    if bt in ("", "local"):
        return []

    if bt == "custom":
        bcf = _inp("backend_config_file")
        if bcf:
            resolved = (cwd / bcf).resolve()
            if not str(resolved).startswith(str(cwd.resolve())):
                raise SystemExit(f"Path traversal in backend-config-file: '{bcf}'")
            return ["-backend-config", str(resolved)]
        return []

    args: list[str] = []
    if bt == "azurerm":
        for key, flag in [
            ("azure_resource_group",  "resource_group_name"),
            ("azure_storage_account", "storage_account_name"),
            ("azure_container",       "container_name"),
            ("azure_state_key",       "key"),
        ]:
            val = _inp(key)
            if val:
                args += ["-backend-config", f"{flag}={val}"]
    elif bt == "s3":
        for key, flag in [
            ("aws_bucket",         "bucket"),
            ("aws_key",            "key"),
            ("aws_region",         "region"),
            ("aws_dynamodb_table", "dynamodb_table"),
        ]:
            val = _inp(key)
            if val:
                args += ["-backend-config", f"{flag}={val}"]
    elif bt == "gcs":
        for key, flag in [
            ("gcp_bucket", "bucket"),
            ("gcp_prefix", "prefix"),
        ]:
            val = _inp(key)
            if val:
                args += ["-backend-config", f"{flag}={val}"]
    return args

# ── Terraform runner ──────────────────────────────────────────────────────────

def tf(args: list[str], cwd: Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["terraform"] + args
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, cwd=str(cwd),
        capture_output=capture,
        text=True,
    )
    return result

def tf_check(args: list[str], cwd: Path) -> None:
    r = tf(args, cwd)
    if r.returncode != 0:
        raise SystemExit(f"terraform {args[0]} failed (exit {r.returncode})")

# ── Plan JSON generation ──────────────────────────────────────────────────────

def generate_plan_json(cwd: Path, plan_file: str) -> Path:
    json_path = cwd / "plan.json"
    log("Generating plan.json via terraform show -json…")
    r = tf(["show", "-json", plan_file], cwd, capture=True)
    if r.returncode != 0:
        raise SystemExit(f"terraform show -json failed: {r.stderr[:2000]}")
    if not r.stdout.strip():
        raise SystemExit("plan.json is empty after terraform show -json.")
    json_path.write_text(r.stdout, encoding="utf-8")
    ok(f"plan.json written ({json_path.stat().st_size // 1024} KB)")
    return json_path

# ── Plan parsing ──────────────────────────────────────────────────────────────

ActionKind = str  # "create" | "delete" | "update" | "replace" | "no-op" | "read"

def classify(actions: list[str]) -> ActionKind:
    s = set(actions)
    if "create" in s and "delete" in s:
        return "replace"
    if "create" in s:
        return "create"
    if "delete" in s:
        return "delete"
    if "update" in s:
        return "update"
    if "read" in s:
        return "read"
    return "no-op"

def parse_plan(plan: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {"create": 0, "delete": 0, "update": 0, "replace": 0, "read": 0, "no-op": 0}
    resources: list[dict[str, str]] = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        kind = classify(actions)
        counts[kind] += 1
        if kind != "no-op":
            resources.append({"address": rc.get("address", ""), "type": rc.get("type", ""), "action": kind})
    return {"counts": counts, "resources": resources}

# ── Policy checks (ported from ADO planTab.ts) ────────────────────────────────

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

def policy_checks(plan: dict[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    for rc in plan.get("resource_changes", []):
        addr    = rc.get("address", "")
        rtype   = rc.get("type", "")
        change  = rc.get("change", {})
        actions = change.get("actions", [])
        after   = change.get("after") or {}

        if not (set(actions) & {"create", "update"}):
            continue

        # S3 public access
        if rtype == "aws_s3_bucket_public_access_block":
            for k in ["block_public_acls", "block_public_policy", "restrict_public_buckets", "ignore_public_acls"]:
                if after.get(k) is False:
                    warnings.append({"severity": "high", "address": addr, "rule": "S3 Public Access",
                                     "detail": f"{k} is false — bucket may be publicly accessible."})

        # S3 versioning
        if rtype == "aws_s3_bucket_versioning":
            cfg = after.get("versioning_configuration") or {}
            if cfg.get("status") != "Enabled":
                warnings.append({"severity": "low", "address": addr, "rule": "S3 Versioning Disabled",
                                  "detail": "Versioning is not enabled — accidental deletions cannot be recovered."})

        # S3 encryption
        if rtype == "aws_s3_bucket":
            bucket_name = addr.split(".")[-1] if "." in addr else addr
            has_enc = any(
                r.get("type") == "aws_s3_bucket_server_side_encryption_configuration"
                and bucket_name in (r.get("address") or "")
                for r in plan.get("resource_changes", [])
            )
            if not has_enc:
                warnings.append({"severity": "medium", "address": addr, "rule": "S3 Encryption",
                                  "detail": "No aws_s3_bucket_server_side_encryption_configuration found for this bucket."})

        # Security group open ingress
        if rtype == "aws_security_group":
            for rule in after.get("ingress", []) or []:
                cidrs = rule.get("cidr_blocks", []) or []
                ipv6  = rule.get("ipv6_cidr_blocks", []) or []
                if "0.0.0.0/0" in cidrs or "::/0" in ipv6:
                    from_port = rule.get("from_port", "")
                    to_port   = rule.get("to_port", "")
                    port_str  = f"port {from_port}" if from_port == to_port else f"ports {from_port}–{to_port}"
                    sev = "high" if from_port in (22, 3389) else "medium"
                    warnings.append({"severity": sev, "address": addr, "rule": "Open Ingress",
                                     "detail": f"Ingress on {port_str} is open to the world (0.0.0.0/0)."})

        # IAM wildcard
        if rtype in ("aws_iam_role_policy", "aws_iam_policy"):
            doc = after.get("policy", "") or ""
            if '"Action": "*"' in doc or '"Action":"*"' in doc:
                warnings.append({"severity": "high", "address": addr, "rule": "IAM Wildcard Action",
                                  "detail": 'Policy contains "Action": "*" — overly permissive.'})
            if '"Resource": "*"' in doc or '"Resource":"*"' in doc:
                warnings.append({"severity": "medium", "address": addr, "rule": "IAM Wildcard Resource",
                                  "detail": 'Policy contains "Resource": "*" — consider scoping to specific resources.'})

        # RDS encryption
        if rtype == "aws_db_instance" and after.get("storage_encrypted") is not True:
            warnings.append({"severity": "high", "address": addr, "rule": "RDS Encryption",
                              "detail": "storage_encrypted is not true — database storage is unencrypted."})

        # RDS public
        if rtype == "aws_db_instance" and after.get("publicly_accessible") is True:
            warnings.append({"severity": "high", "address": addr, "rule": "RDS Public Access",
                              "detail": "publicly_accessible is true — database is reachable from the internet."})

        # EBS encryption
        if rtype == "aws_ebs_volume" and after.get("encrypted") is not True:
            warnings.append({"severity": "medium", "address": addr, "rule": "EBS Encryption",
                              "detail": "EBS volume is not encrypted."})

        # EC2 IMDSv2
        if rtype == "aws_launch_template":
            meta = after.get("metadata_options") or {}
            if meta.get("http_tokens") != "required":
                warnings.append({"severity": "medium", "address": addr, "rule": "IMDSv2 Not Enforced",
                                  "detail": "metadata_options.http_tokens is not 'required' — IMDSv2 not enforced."})

        # Azure storage public
        if rtype == "azurerm_storage_account" and after.get("allow_blob_public_access") is True:
            warnings.append({"severity": "high", "address": addr, "rule": "Azure Blob Public Access",
                              "detail": "allow_blob_public_access is true."})

        # Azure HTTPS only
        if rtype == "azurerm_storage_account" and after.get("enable_https_traffic_only") is False:
            warnings.append({"severity": "high", "address": addr, "rule": "Azure HTTPS Only",
                              "detail": "enable_https_traffic_only is false — HTTP traffic is allowed."})

    warnings.sort(key=lambda w: SEVERITY_ORDER.get(w["severity"], 99))
    return warnings

# ── Mermaid dependency graph ──────────────────────────────────────────────────

def build_mermaid(plan: dict[str, Any]) -> str:
    changes  = plan.get("resource_changes", [])
    cfg_res  = (plan.get("configuration") or {}).get("root_module", {}).get("resources", [])

    action_map: dict[str, str] = {}
    for rc in changes:
        addr = rc.get("address")
        if addr:
            action_map[addr] = classify(rc.get("change", {}).get("actions", []))

    node_set = set(action_map.keys())
    edges: set[tuple[str, str]] = set()

    def collect_refs(val: Any) -> list[str]:
        found: list[str] = []
        if not val or not isinstance(val, (dict, list)):
            return found
        if isinstance(val, list):
            for item in val:
                found.extend(collect_refs(item))
        else:
            refs = val.get("references", [])
            if isinstance(refs, list):
                for r in refs:
                    parts = r.split(".")
                    if len(parts) >= 2:
                        addr = ".".join(parts[:2])
                        if addr in node_set:
                            found.append(addr)
            for v in val.values():
                found.extend(collect_refs(v))
        return found

    for cr in cfg_res:
        src = cr.get("address")
        if not src or src not in node_set:
            continue
        for dep in set(collect_refs(cr.get("expressions", {}))):
            if dep != src:
                edges.add((src, dep))

    def nid(a: str) -> str:
        return "n_" + re.sub(r"[^a-zA-Z0-9]", "_", a)

    def elabel(s: str) -> str:
        return s.replace('"', "'")[:60]

    style_map = {
        "create":  ":::create",
        "delete":  ":::delete",
        "update":  ":::update",
        "replace": ":::replace",
        "read":    ":::read",
        "no-op":   ":::noop",
    }

    lines = [
        "flowchart LR",
        "  classDef create  fill:#dff6dd,stroke:#107c10,color:#107c10",
        "  classDef delete  fill:#fde7e9,stroke:#a4262c,color:#a4262c",
        "  classDef update  fill:#fff4ce,stroke:#c8a400,color:#7a5c00",
        "  classDef replace fill:#fff4ce,stroke:#c8a400,color:#7a5c00",
        "  classDef read    fill:#f0f0f0,stroke:#bbb,color:#444",
        "  classDef noop    fill:#f8f8f8,stroke:#ddd,color:#aaa",
    ]
    for addr, kind in action_map.items():
        label = ".".join(addr.split(".")[-2:]) if "." in addr else addr
        lines.append(f'  {nid(addr)}["{elabel(label)}"]' + style_map.get(kind, ""))
    for src, dep in edges:
        lines.append(f"  {nid(src)} --> {nid(dep)}")
    return "\n".join(lines)

# ── Step Summary renderer ─────────────────────────────────────────────────────

BADGE_COLORS = {
    "create":  ("🟢", "#dff6dd", "#107c10"),
    "delete":  ("🔴", "#fde7e9", "#a4262c"),
    "update":  ("🟡", "#fff4ce", "#7a5c00"),
    "replace": ("🟠", "#fff4ce", "#c8a400"),
    "read":    ("⚪", "#f0f0f0", "#444"),
    "no-op":   ("➖", "#f8f8f8", "#aaa"),
}

def _e(s: str) -> str:
    return html.escape(str(s))

def render_step_summary(plan: dict[str, Any], summary: dict[str, Any],
                        warnings: list[dict[str, str]], json_path: Path,
                        run_url: str) -> str:
    counts  = summary["counts"]
    resources = summary["resources"]
    tf_ver  = plan.get("terraform_version", "")
    fmt_ver = plan.get("format_version", "")

    meta_parts = []
    if tf_ver:
        meta_parts.append(f"Terraform {_e(tf_ver)}")
    if fmt_ver:
        meta_parts.append(f"format {_e(fmt_ver)}")
    meta = " · ".join(meta_parts)

    # Summary badges
    badge_html = ""
    for action, (icon, bg, fg) in BADGE_COLORS.items():
        n = counts.get(action, 0)
        if n and action not in ("no-op",):
            label_map = {"create": "add", "delete": "destroy", "update": "change",
                         "replace": "replace", "read": "read"}
            badge_html += (
                f'<span style="background:{bg};color:{fg};border:1px solid {fg};'
                f'border-radius:4px;padding:2px 8px;margin-right:6px;font-weight:600;">'
                f'{icon} {n} {label_map.get(action, action)}</span>'
            )
    if not badge_html:
        badge_html = '<span style="color:#107c10;font-weight:600;">✅ No changes</span>'

    # Warnings section
    warn_html = ""
    if warnings:
        high   = sum(1 for w in warnings if w["severity"] == "high")
        medium = sum(1 for w in warnings if w["severity"] == "medium")
        low    = sum(1 for w in warnings if w["severity"] == "low")
        sev_counts = " · ".join(filter(None, [
            f'<span style="color:#a4262c;font-weight:600;">{high} high</span>'   if high   else "",
            f'<span style="color:#c8a400;font-weight:600;">{medium} medium</span>' if medium else "",
            f'<span style="color:#0078d4;font-weight:600;">{low} low</span>'     if low    else "",
        ]))
        rows = "".join(
            f"<tr><td>{'🔴' if w['severity']=='high' else '🟡' if w['severity']=='medium' else '🔵'}</td>"
            f"<td><code>{_e(w['address'])}</code></td>"
            f"<td><strong>{_e(w['rule'])}</strong></td>"
            f"<td>{_e(w['detail'])}</td></tr>"
            for w in warnings
        )
        warn_html = f"""
<details open>
<summary>⚠️ <strong>Policy warnings</strong> &nbsp; {sev_counts}</summary>

<table>
<thead><tr><th></th><th>Resource</th><th>Rule</th><th>Detail</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</details>
"""

    # Resource changes table (expandable via <details>)
    if resources:
        rows = ""
        for rc in resources:
            icon = BADGE_COLORS.get(rc["action"], ("", "", ""))[0]
            rows += (
                f"<tr><td>{icon}</td>"
                f"<td><code>{_e(rc['address'])}</code></td>"
                f"<td><code>{_e(rc['type'])}</code></td>"
                f"<td><strong>{_e(rc['action'])}</strong></td></tr>"
            )
        table_html = f"""
<details open>
<summary><strong>Resource changes</strong> ({len(resources)} resources)</summary>

<table>
<thead><tr><th></th><th>Address</th><th>Type</th><th>Action</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</details>
"""
    else:
        table_html = "<p>No resource changes.</p>"

    # Mermaid diagram
    mermaid_src = build_mermaid(plan)
    diagram_html = f"""
<details>
<summary><strong>Dependency graph</strong></summary>

```mermaid
{mermaid_src}
```

</details>
"""

    sha_short = GITHUB_SHA[:7] if GITHUB_SHA else ""
    ref_info  = f"`{_e(GITHUB_REF_NAME)}`" if GITHUB_REF_NAME else ""
    sha_info  = f"@ `{sha_short}`" if sha_short else ""

    return f"""## 🏗️ Terraform Plan

<p>{meta}</p>

{badge_html}

{warn_html}

{table_html}

{diagram_html}

---
<sub>
Run <a href="{_e(run_url)}">#{_e(GITHUB_RUN_ID)}</a>
{ref_info} {sha_info} · <a href="{_e(run_url)}">View workflow</a>
</sub>
"""

# ── PR comment ────────────────────────────────────────────────────────────────

PR_COMMENT_MARKER = "<!-- gh-tf-action-plan-comment -->"

def build_pr_comment(summary: dict[str, Any], warnings: list[dict[str, str]], run_url: str) -> str:
    counts    = summary["counts"]
    resources = summary["resources"]

    def badge(n: int, label: str, icon: str) -> str | None:
        return f"{icon} **{n} {label}**" if n else None

    counts_line = " · ".join(filter(None, [
        badge(counts.get("create", 0),  "to add",     "🟢"),
        badge(counts.get("update", 0),  "to change",  "🟡"),
        badge(counts.get("replace", 0), "to replace", "🟠"),
        badge(counts.get("delete", 0),  "to destroy", "🔴"),
    ])) or "✅ No changes"

    rows = "\n".join(
        f"| {'🟢' if r['action']=='create' else '🔴' if r['action']=='delete' else '🟡'} "
        f"| `{r['address'][:100]}` | {r['action']} |"
        for r in resources[:30]
    )
    overflow = f"\n_…and {len(resources)-30} more resources_" if len(resources) > 30 else ""

    warn_section = ""
    if warnings:
        w_counts = " · ".join(filter(None, [
            f"🔴 {sum(1 for w in warnings if w['severity']=='high')} high"   if any(w['severity']=='high'   for w in warnings) else "",
            f"🟡 {sum(1 for w in warnings if w['severity']=='medium')} medium" if any(w['severity']=='medium' for w in warnings) else "",
            f"🔵 {sum(1 for w in warnings if w['severity']=='low')} low"     if any(w['severity']=='low'    for w in warnings) else "",
        ]))
        warn_rows = "\n".join(
            f"| {'🔴' if w['severity']=='high' else '🟡' if w['severity']=='medium' else '🔵'} "
            f"| `{w['address'][:80]}` | {w['rule']} | {w['detail'][:120]} |"
            for w in warnings[:20]
        )
        warn_section = f"""
<details>
<summary>⚠️ Policy warnings — {w_counts}</summary>

| | Resource | Rule | Detail |
|---|---|---|---|
{warn_rows}

</details>
"""

    ref_info = f"`{GITHUB_REF_NAME}`" if GITHUB_REF_NAME else ""
    sha_info = f"@ `{GITHUB_SHA[:7]}`" if GITHUB_SHA else ""

    return f"""{PR_COMMENT_MARKER}
## 🏗️ Terraform Plan {ref_info} {sha_info}

{counts_line}

| | Resource | Action |
|---|---|---|
{rows}
{overflow}
{warn_section}
<sub>Posted by [gh-tf-action]({run_url}) · [View run]({run_url})</sub>
"""

def get_pr_number() -> str | None:
    if PR_NUMBER:
        return PR_NUMBER
    # Try reading from GITHUB_EVENT_PATH
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if event_path and Path(event_path).exists():
        try:
            event = json.loads(Path(event_path).read_text())
            pr = event.get("pull_request", {}).get("number")
            if pr:
                return str(pr)
        except Exception:
            pass
    return None

def _gh_api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not GITHUB_TOKEN:
        warn("No GITHUB_TOKEN — skipping GitHub API call.")
        return None
    url = f"{GITHUB_API_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":    "gh-tf-action/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()) if resp.status in (200, 201) else None
    except urllib.error.HTTPError as e:
        warn(f"GitHub API {method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")
        return None

def post_or_update_pr_comment(comment_body: str, pr_number: str) -> None:
    repo = GITHUB_REPOSITORY
    if not repo:
        warn("GITHUB_REPOSITORY not set — skipping PR comment.")
        return

    # Find existing comment from this action to update rather than spam
    comments = _gh_api("GET", f"/repos/{repo}/issues/{pr_number}/comments?per_page=100")
    existing_id: int | None = None
    if isinstance(comments, list):
        for c in comments:
            if isinstance(c, dict) and PR_COMMENT_MARKER in (c.get("body") or ""):
                existing_id = c.get("id")
                break

    if existing_id:
        result = _gh_api("PATCH", f"/repos/{repo}/issues/comments/{existing_id}", {"body": comment_body})
        if result:
            ok(f"Updated PR comment #{existing_id} on PR #{pr_number}")
    else:
        result = _gh_api("POST", f"/repos/{repo}/issues/{pr_number}/comments", {"body": comment_body})
        if result:
            ok(f"Posted PR comment on PR #{pr_number}")

# ── GitHub outputs ────────────────────────────────────────────────────────────

def set_outputs(counts: dict[str, int], json_path: Path, exit_code: int) -> None:
    if not GITHUB_OUTPUT:
        return
    total_changes = sum(counts.get(k, 0) for k in ("create", "delete", "update", "replace"))
    lines = [
        f"add-count={counts.get('create', 0)}",
        f"change-count={counts.get('update', 0)}",
        f"destroy-count={counts.get('delete', 0)}",
        f"replace-count={counts.get('replace', 0)}",
        f"has-changes={'true' if total_changes > 0 else 'false'}",
        f"plan-json-path={json_path}",
        f"exit-code={exit_code}",
    ]
    with open(GITHUB_OUTPUT, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def write_step_summary(content: str) -> None:
    if not GITHUB_STEP_SUMMARY:
        return
    with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as f:
        f.write(content + "\n")

# ── Upload artifact ───────────────────────────────────────────────────────────

def upload_artifact(json_path: Path) -> None:
    # GitHub Actions artifacts are uploaded via the toolkit runtime API
    # inside a Docker action we use the actions/upload-artifact action pattern
    # via the ACTIONS_RUNTIME_TOKEN + ACTIONS_RUNTIME_URL env vars.
    runtime_url   = os.environ.get("ACTIONS_RUNTIME_URL", "")
    runtime_token = os.environ.get("ACTIONS_RUNTIME_TOKEN", "")
    run_id        = os.environ.get("GITHUB_RUN_ID", "")

    if not runtime_url or not runtime_token:
        warn("ACTIONS_RUNTIME_URL/TOKEN not available — artifact upload skipped. "
             "Add 'actions/upload-artifact' as a separate step if needed.")
        return

    import urllib.parse
    # Create artifact container
    create_url = f"{runtime_url}_apis/pipelines/workflows/{run_id}/artifacts?api-version=6.0-preview"
    body = json.dumps({"Type": "actions_storage", "Name": ARTIFACT_NAME}).encode()
    req = urllib.request.Request(
        create_url, data=body, method="POST",
        headers={
            "Authorization":  f"Bearer {runtime_token}",
            "Content-Type":   "application/json",
            "Accept":         "application/json;api-version=6.0-preview",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            artifact_info = json.loads(resp.read().decode())
            file_container_resource_url = artifact_info.get("fileContainerResourceUrl", "")
    except Exception as exc:
        warn(f"Could not create artifact container: {exc}")
        return

    if not file_container_resource_url:
        warn("No fileContainerResourceUrl in artifact response.")
        return

    # Upload file
    upload_url = f"{file_container_resource_url}?itemPath={urllib.parse.quote(ARTIFACT_NAME + '/plan.json')}"
    file_data  = json_path.read_bytes()
    req2 = urllib.request.Request(
        upload_url, data=file_data, method="PUT",
        headers={
            "Authorization":  f"Bearer {runtime_token}",
            "Content-Type":   "application/octet-stream",
            "Accept":         "application/json;api-version=6.0-preview",
            "Content-Length": str(len(file_data)),
        },
    )
    try:
        with urllib.request.urlopen(req2, timeout=30) as resp:
            if resp.status in (200, 201):
                ok(f"Artifact '{ARTIFACT_NAME}/plan.json' uploaded.")
            else:
                warn(f"Artifact upload returned HTTP {resp.status}.")
    except Exception as exc:
        warn(f"Artifact upload failed: {exc}")

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_init(cwd: Path, extra: list[str]) -> None:
    args = ["init", "-input=false"] + backend_args(cwd) + extra
    tf_check(args, cwd)
    ok("init complete.")

def cmd_validate(cwd: Path, extra: list[str]) -> None:
    cmd_init(cwd, [])
    tf_check(["validate"] + extra, cwd)
    ok("validate complete.")

def cmd_plan(cwd: Path, extra: list[str]) -> tuple[Path, dict[str, Any]]:
    tf_check(["plan", "-input=false", "-out", PLAN_FILE] + extra, cwd)
    json_path = generate_plan_json(cwd, PLAN_FILE)
    plan = json.loads(json_path.read_text())
    return json_path, plan

def cmd_apply(cwd: Path, extra: list[str], plan_file: str | None = None) -> None:
    args = ["apply", "-input=false"]
    if AUTO_APPROVE:
        args.append("-auto-approve")
    args += extra
    if plan_file:
        args.append(plan_file)
    tf_check(args, cwd)
    ok("apply complete.")

def cmd_destroy(cwd: Path, extra: list[str]) -> None:
    args = ["plan", "-destroy", "-input=false", "-out", PLAN_FILE] + extra
    tf_check(args, cwd)
    apply_args = ["apply", "-input=false"]
    if AUTO_APPROVE:
        apply_args.append("-auto-approve")
    apply_args.append(PLAN_FILE)
    tf_check(apply_args, cwd)
    ok("destroy complete.")

# ── Main ──────────────────────────────────────────────────────────────────────

def run_url() -> str:
    if GITHUB_SERVER_URL and GITHUB_REPOSITORY and GITHUB_RUN_ID:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}"
    return ""

def main() -> None:
    cwd   = resolve_working_dir()
    extra = parse_extra_args(EXTRA_ARGS)
    url   = run_url()

    log(f"Command: {COMMAND}  |  Working dir: {cwd}")
    log(f"Backend: {BACKEND_TYPE}  |  Plan file: {PLAN_FILE}")

    json_path: Path | None = None
    plan:      dict[str, Any] = {}
    exit_code = 0

    try:
        if COMMAND == "init":
            cmd_init(cwd, extra)

        elif COMMAND == "validate":
            cmd_validate(cwd, extra)

        elif COMMAND == "plan":
            cmd_init(cwd, [])
            json_path, plan = cmd_plan(cwd, extra)

        elif COMMAND == "apply":
            # Expects plan file to exist (from prior plan step)
            cmd_apply(cwd, extra, PLAN_FILE)

        elif COMMAND == "plan-and-apply":
            cmd_init(cwd, [])
            json_path, plan = cmd_plan(cwd, extra)
            cmd_apply(cwd, [], PLAN_FILE)

        elif COMMAND == "destroy":
            cmd_init(cwd, [])
            cmd_destroy(cwd, extra)

        else:
            raise SystemExit(f"Unknown command: '{COMMAND}'. Valid: init | validate | plan | apply | plan-and-apply | destroy")

    except SystemExit as e:
        err(str(e))
        exit_code = 1

    # ── Visualization (plan / plan-and-apply) ─────────────────────────────────
    if json_path and plan and exit_code == 0:
        summary  = parse_plan(plan)
        warnings = policy_checks(plan)
        counts   = summary["counts"]

        total = sum(counts.get(k, 0) for k in ("create", "delete", "update", "replace"))
        log(f"Plan: +{counts['create']} to add, ~{counts['update']} to change, "
            f"-{counts['delete']} to destroy, ±{counts['replace']} to replace")

        if warnings:
            warn(f"{len(warnings)} policy warning(s): "
                 f"{sum(1 for w in warnings if w['severity']=='high')} high, "
                 f"{sum(1 for w in warnings if w['severity']=='medium')} medium, "
                 f"{sum(1 for w in warnings if w['severity']=='low')} low")

        # Step Summary
        if POST_SUMMARY:
            rendered = render_step_summary(plan, summary, warnings, json_path, url)
            write_step_summary(rendered)
            ok("Step Summary written.")

        # PR Comment
        if POST_COMMENT:
            pr_num = get_pr_number()
            if pr_num:
                comment = build_pr_comment(summary, warnings, url)
                post_or_update_pr_comment(comment, pr_num)
            else:
                log("Not a PR build — skipping PR comment.")

        # Artifact upload
        if UPLOAD_ARTIFACT and json_path:
            upload_artifact(json_path)

        # Outputs
        set_outputs(counts, json_path, 2 if total > 0 else 0)
    elif exit_code == 0:
        # Non-plan commands still set a basic output
        if GITHUB_OUTPUT:
            with open(GITHUB_OUTPUT, "a") as f:
                f.write("exit-code=0\n")

    if exit_code != 0:
        sys.exit(exit_code)
    ok(f"Command '{COMMAND}' finished successfully.")


if __name__ == "__main__":
    main()
