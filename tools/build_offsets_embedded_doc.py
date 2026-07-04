#!/usr/bin/env python3
"""Build a self-contained HTML document with the offset output embedded."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT.parent / "output"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "offsets-embedded.html"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def count_client_schema(client_json: Any) -> dict[str, int]:
    if not isinstance(client_json, dict):
        return {"classes": 0, "fields": 0, "enums": 0}
    root = client_json.get("client.dll", client_json)
    if not isinstance(root, dict):
        return {"classes": 0, "fields": 0, "enums": 0}
    classes = root.get("classes", {})
    enums = root.get("enums", {})
    field_count = 0
    if isinstance(classes, dict):
        for value in classes.values():
            if isinstance(value, dict) and isinstance(value.get("fields"), dict):
                field_count += len(value["fields"])
    return {
        "classes": len(classes) if isinstance(classes, dict) else 0,
        "fields": field_count,
        "enums": len(enums) if isinstance(enums, dict) else 0,
    }


def detect_generator_meta(files: list[Path]) -> dict[str, str]:
    meta = {"source_url": "", "generated_comment": ""}
    for path in files:
        if path.suffix.lower() not in {".cs", ".hpp", ".rs", ".zig"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:3]
        except Exception:
            continue
        joined = "\n".join(lines)
        url_match = re.search(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", joined)
        if not url_match:
            continue
        meta["source_url"] = url_match.group(0)
        if len(lines) > 1:
            meta["generated_comment"] = lines[1].lstrip("/# ").strip()
        return meta
    return meta


def build_zip(source: Path, files: list[Path]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source)
            archive_name = Path("output") / relative
            info = zipfile.ZipInfo(str(archive_name).replace(os.sep, "/"))
            info.date_time = (2026, 7, 2, 15, 23, 25)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def format_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:.2f} KiB"
    return f"{value} B"


def hex_value(value: Any) -> str:
    if isinstance(value, int):
        return f"0x{value:X}"
    return "-"


def table_rows(rows: list[list[str]]) -> str:
    return "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Offset output directory")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="HTML output path")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    out_path = args.out.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")

    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"source directory contains no files: {source}")

    manifest_files = []
    extension_counts: Counter[str] = Counter()
    module_counts: defaultdict[str, int] = defaultdict(int)
    total_bytes = 0
    for path in files:
        relative = path.relative_to(source).as_posix()
        size = path.stat().st_size
        total_bytes += size
        extension_counts[path.suffix.lower() or "(none)"] += 1
        module_counts[path.stem.rsplit(".", 1)[0]] += 1
        manifest_files.append(
            {
                "path": f"output/{relative}",
                "size": size,
                "sha256": sha256_file(path),
                "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            }
        )

    zip_bytes = build_zip(source, files)
    zip_b64 = base64.b64encode(zip_bytes).decode("ascii")
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    generator_meta = detect_generator_meta(files)

    info_json = load_json(source / "info.json") or {}
    offsets_json = load_json(source / "offsets.json") or {}
    buttons_json = load_json(source / "buttons.json") or {}
    client_json = load_json(source / "client_dll.json")
    client_counts = count_client_schema(client_json)

    required_names = ["info.json", "offsets.json", "buttons.json", "client_dll.json"]
    required_rows = []
    for name in required_names:
        item = next((entry for entry in manifest_files if entry["path"] == f"output/{name}"), None)
        if item:
            required_rows.append([name, format_size(item["size"]), item["sha256"]])

    key_offsets = []
    client_offsets = offsets_json.get("client.dll", {}) if isinstance(offsets_json, dict) else {}
    engine_offsets = offsets_json.get("engine2.dll", {}) if isinstance(offsets_json, dict) else {}
    for key in [
        "dwEntityList",
        "dwLocalPlayerController",
        "dwLocalPlayerPawn",
        "dwViewMatrix",
        "dwViewAngles",
        "dwGlobalVars",
        "dwPlantedC4",
    ]:
        key_offsets.append(["client.dll", key, str(client_offsets.get(key, "-")), hex_value(client_offsets.get(key))])
    for key in ["dwBuildNumber", "dwWindowWidth", "dwWindowHeight"]:
        key_offsets.append(["engine2.dll", key, str(engine_offsets.get(key, "-")), hex_value(engine_offsets.get(key))])

    button_rows = []
    button_root = buttons_json.get("client.dll", {}) if isinstance(buttons_json, dict) else {}
    for key in sorted(button_root):
        button_rows.append([key, str(button_root[key]), hex_value(button_root[key])])

    inventory_rows = [
        [entry["path"], format_size(entry["size"]), entry["sha256"]]
        for entry in manifest_files
    ]
    extension_rows = [[ext, str(count)] for ext, count in sorted(extension_counts.items())]
    upstream_base = ""
    upstream_rows = []
    if generator_meta["source_url"]:
        upstream_base = generator_meta["source_url"].replace("github.com", "raw.githubusercontent.com") + "/main/output"
        upstream_rows = [[name, f"{upstream_base}/{name}"] for name in required_names]

    manifest = {
        "generated_utc": generated_utc,
        "source_directory": str(source),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "zip_bytes": len(zip_bytes),
        "zip_sha256": sha256_bytes(zip_bytes),
        "build_number": info_json.get("build_number"),
        "offset_timestamp": info_json.get("timestamp"),
        "detected_generator": generator_meta,
        "upstream_raw_base": upstream_base,
        "git": {
            "remote": git_value(["remote", "get-url", "origin"]),
            "branch": git_value(["branch", "--show-current"]),
            "commit": git_value(["rev-parse", "HEAD"]),
        },
        "files": manifest_files,
    }

    html_text = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Offset Embedded Document - build {html.escape(str(info_json.get("build_number", "unknown")))}</title>
  <style>
    :root {{
      --bg: #f7f8fb;
      --paper: #ffffff;
      --ink: #18202f;
      --muted: #5c6878;
      --line: #d9e0ea;
      --accent: #176b87;
      --accent-soft: #e3f3f7;
      --warn: #8a5a00;
      --warn-soft: #fff4d7;
      --code: #101827;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: "Segoe UI", Arial, sans-serif;
      line-height: 1.55;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 24px 56px;
    }}
    header {{
      padding: 28px 0 22px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 24px;
    }}
    h1 {{
      font-size: 32px;
      line-height: 1.16;
      margin: 0 0 10px;
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 21px;
      margin: 30px 0 12px;
      letter-spacing: 0;
    }}
    h3 {{
      font-size: 16px;
      margin: 18px 0 8px;
      letter-spacing: 0;
    }}
    p {{ margin: 8px 0 12px; }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin: 18px 0 8px;
    }}
    .metric {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .metric strong {{
      display: block;
      font-size: 20px;
      margin-top: 3px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin: 16px 0;
    }}
    .note {{
      background: var(--warn-soft);
      border-color: #efd489;
      color: #3c2b07;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }}
    button {{
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: white;
      font-weight: 650;
      padding: 10px 14px;
      cursor: pointer;
    }}
    button.secondary {{
      background: #eef3f8;
      color: var(--ink);
      border: 1px solid var(--line);
    }}
    code, pre {{
      font-family: Consolas, "Cascadia Mono", monospace;
    }}
    pre {{
      overflow: auto;
      color: #eef4ff;
      background: var(--code);
      border-radius: 8px;
      padding: 14px;
      font-size: 12px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      margin: 10px 0 18px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{
      background: var(--accent-soft);
      color: #103e51;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    td:nth-child(3), td.hash {{
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 11px;
      word-break: break-all;
    }}
    .scroll {{
      max-height: 420px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .scroll table {{ margin: 0; border: 0; }}
    .ok {{ color: #146c43; font-weight: 650; }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
<main>
  <header>
    <p class="label">CTF / reverse-flow evidence bundle</p>
    <h1>Offset Embedded Document</h1>
    <p>這是一份自包含文件：完整 <code>output/</code> offset 匯出已壓縮成 ZIP 並以 base64 內嵌在本 HTML 中，可離線驗證與下載還原。</p>
  </header>

  <section class="meta-grid" aria-label="summary">
    <div class="metric"><span class="label">Build number</span><strong>{html.escape(str(info_json.get("build_number", "-")))}</strong></div>
    <div class="metric"><span class="label">Offset timestamp</span><strong>{html.escape(str(info_json.get("timestamp", "-")))}</strong></div>
    <div class="metric"><span class="label">Embedded files</span><strong>{len(files)}</strong></div>
    <div class="metric"><span class="label">Original size</span><strong>{format_size(total_bytes)}</strong></div>
    <div class="metric"><span class="label">Embedded ZIP size</span><strong>{format_size(len(zip_bytes))}</strong></div>
    <div class="metric"><span class="label">Generated UTC</span><strong>{html.escape(generated_utc)}</strong></div>
  </section>

  <section class="panel">
    <h2>雲端下載 / Cloud Download</h2>
    <p>按下按鈕會從本文件內嵌資料還原 <code>offset-output-build-{html.escape(str(info_json.get("build_number", "unknown")))}.zip</code>。若 GitHub 以原始碼方式顯示，請先下載此 HTML 後用瀏覽器開啟。</p>
    <div class="actions">
      <button id="downloadZip" type="button">Download embedded ZIP</button>
      <button class="secondary" id="copyManifest" type="button">Copy manifest JSON</button>
    </div>
    <p id="status" class="muted">Embedded ZIP SHA-256: <code>{sha256_bytes(zip_bytes)}</code></p>
  </section>

  <section class="panel note">
    <h2>範圍 / Scope</h2>
    <p>本文件只封裝與盤點本機 offset 匯出資料；未執行任何二進位檔，也未修改原始 <code>D:\\11\\output</code> 內容。完整檔案以 <code>output/&lt;name&gt;</code> 路徑保存在內嵌 ZIP 中。</p>
  </section>

  <section class="panel">
    <h2>公開 offset 倉庫 / Public Offset Source</h2>
    <p>本機輸出檔頭偵測到產生來源：<a href="{html.escape(generator_meta["source_url"])}">{html.escape(generator_meta["source_url"] or "-")}</a>。下列 raw 連結指向上游公開 repo 的 <code>main/output</code>；上游會隨時間變動，本文件內嵌 ZIP 則是固定 snapshot。</p>
    <table>
      <thead><tr><th>File</th><th>Public raw URL</th></tr></thead>
      <tbody>
{table_rows(upstream_rows)}
      </tbody>
    </table>
  </section>

  <h2>必要檔案 / Required Files</h2>
  <table>
    <thead><tr><th>File</th><th>Size</th><th>SHA-256</th></tr></thead>
    <tbody>
{table_rows(required_rows)}
    </tbody>
  </table>

  <h2>Offset 摘要 / Key Offsets</h2>
  <table>
    <thead><tr><th>Module</th><th>Name</th><th>Decimal</th><th>Hex</th></tr></thead>
    <tbody>
{table_rows(key_offsets)}
    </tbody>
  </table>

  <h2>Buttons 摘要 / Button Offsets</h2>
  <div class="scroll">
    <table>
      <thead><tr><th>Name</th><th>Decimal</th><th>Hex</th></tr></thead>
      <tbody>
{table_rows(button_rows)}
      </tbody>
    </table>
  </div>

  <h2>Schema 摘要 / Schema Summary</h2>
  <table>
    <thead><tr><th>Area</th><th>Count</th></tr></thead>
    <tbody>
      <tr><td>client.dll classes</td><td>{client_counts["classes"]}</td></tr>
      <tr><td>client.dll fields</td><td>{client_counts["fields"]}</td></tr>
      <tr><td>client.dll enums</td><td>{client_counts["enums"]}</td></tr>
    </tbody>
  </table>

  <h2>檔案類型統計 / File Type Counts</h2>
  <table>
    <thead><tr><th>Extension</th><th>Count</th></tr></thead>
    <tbody>
{table_rows(extension_rows)}
    </tbody>
  </table>

  <h2>完整清單 / Embedded Inventory</h2>
  <div class="scroll">
    <table>
      <thead><tr><th>Path</th><th>Size</th><th>SHA-256</th></tr></thead>
      <tbody>
{table_rows(inventory_rows)}
      </tbody>
    </table>
  </div>

  <h2>GitHub 發布資訊 / Repository Metadata</h2>
  <pre>{html.escape(json.dumps(manifest["git"], indent=2, ensure_ascii=False))}</pre>

  <h2>Manifest JSON</h2>
  <pre id="manifestJson">{html.escape(json.dumps(manifest, indent=2, ensure_ascii=False))}</pre>

  <script id="embeddedZipBase64" type="application/octet-stream">
{zip_b64}
  </script>
  <script>
    const statusEl = document.getElementById("status");
    const fileName = "offset-output-build-{html.escape(str(info_json.get("build_number", "unknown")))}.zip";
    const zipHash = "{sha256_bytes(zip_bytes)}";

    function base64ToBlob(base64, mimeType) {{
      const clean = base64.replace(/\\s+/g, "");
      const sliceSize = 1024 * 512;
      const byteCharacters = atob(clean);
      const byteArrays = [];
      for (let offset = 0; offset < byteCharacters.length; offset += sliceSize) {{
        const slice = byteCharacters.slice(offset, offset + sliceSize);
        const byteNumbers = new Array(slice.length);
        for (let i = 0; i < slice.length; i += 1) {{
          byteNumbers[i] = slice.charCodeAt(i);
        }}
        byteArrays.push(new Uint8Array(byteNumbers));
      }}
      return new Blob(byteArrays, {{ type: mimeType }});
    }}

    document.getElementById("downloadZip").addEventListener("click", () => {{
      const base64 = document.getElementById("embeddedZipBase64").textContent;
      const blob = base64ToBlob(base64, "application/zip");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      statusEl.innerHTML = '<span class="ok">ZIP generated from embedded data.</span> SHA-256: <code>' + zipHash + '</code>';
    }});

    document.getElementById("copyManifest").addEventListener("click", async () => {{
      const text = document.getElementById("manifestJson").textContent;
      await navigator.clipboard.writeText(text);
      statusEl.innerHTML = '<span class="ok">Manifest copied.</span> Embedded ZIP SHA-256: <code>' + zipHash + '</code>';
    }});
  </script>
</main>
</body>
</html>
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8", newline="\n")
    print(out_path)
    print(json.dumps({
        "file_count": len(files),
        "total_bytes": total_bytes,
        "zip_bytes": len(zip_bytes),
        "zip_sha256": sha256_bytes(zip_bytes),
        "html_sha256": sha256_file(out_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
