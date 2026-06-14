#!/usr/bin/env python3
"""推送公开版交易日报 → 飞书群机器人 + 个人微信（PushPlus / Server酱）"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]
JOURNAL_DIR = ROOT / "docs/trading-system/journal"
CONFIG_PATH = ROOT / "docs/trading-system/notify.yaml"
CONFIG_EXAMPLE = ROOT / "docs/trading-system/notify.yaml.example"


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"未找到 {path}，请复制 notify.yaml.example 为 notify.yaml 并填写 webhook/token"
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _section(body: str, header: str) -> str:
    pat = re.compile(rf"^{re.escape(header)}[\s\S]*?(?=^## |\Z)", re.MULTILINE)
    m = pat.search(body)
    return m.group(0).strip() if m else ""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline_md(text: str) -> str:
    text = _escape(text.replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s.replace("|", "").replace(":", "").replace("-", "")) == set()


def _parse_table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    rows: list[list[str]] = []
    for line in lines[2:]:
        if not line.strip().startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return header, rows


def _row_is_buyable(cells: list[str]) -> bool:
    joined = " ".join(cells)
    return "可买" in joined or "【突破" in joined or "【反包" in joined


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    parts = [
        "<table style='width:100%;border-collapse:collapse;margin:8px 0 12px;font-size:13px;'>",
        "<tr>",
    ]
    for cell in header:
        parts.append(
            f"<th style='background:#f5f7fa;padding:6px 8px;border:1px solid #e8e8e8;"
            f"text-align:left;font-weight:600;'>{_inline_md(cell)}</th>"
        )
    parts.append("</tr>")
    for row in rows:
        bg = "#f6ffed" if _row_is_buyable(row) else "#fff"
        parts.append(f"<tr style='background:{bg};'>")
        for cell in row:
            style = "padding:6px 8px;border:1px solid #e8e8e8;vertical-align:top;"
            if "可买" in cell:
                style += "color:#08979c;font-weight:600;"
            parts.append(f"<td style='{style}'>{_inline_md(cell)}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _render_md_block(text: str, *, callout: bool = False) -> str:
    chunks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            table_lines = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            header, rows = _parse_table(table_lines)
            chunks.append(_render_table(header, rows))
            continue

        if stripped.startswith("- "):
            chunks.append("<ul style='margin:6px 0 10px;padding-left:20px;'>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                item = lines[i].strip()[2:]
                chunks.append(f"<li style='margin:4px 0;'>{_inline_md(item)}</li>")
                i += 1
            chunks.append("</ul>")
            continue

        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            chunks.append(
                f"<div style='color:#666;font-size:12px;margin:0 0 12px;'>{_inline_md(quote)}</div>"
            )
            i += 1
            continue

        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith("|")
                or nxt.startswith("- ")
                or nxt.startswith(">")
            ):
                break
            para_lines.append(nxt)
            i += 1
        para = " ".join(para_lines)
        if callout:
            chunks.append(
                "<div style='background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;"
                f"padding:10px 12px;margin:8px 0 14px;font-size:15px;line-height:1.5;'>"
                f"{_inline_md(para)}</div>"
            )
        elif para.startswith("*") and para.endswith("*"):
            chunks.append(
                f"<p style='color:#999;font-size:12px;margin:6px 0;'>{_inline_md(para.strip('*'))}</p>"
            )
        else:
            chunks.append(f"<p style='margin:6px 0 10px;'>{_inline_md(para)}</p>")

    return "".join(chunks)


def _split_sections(body: str) -> list[tuple[str, str]]:
    pat = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pat.finditer(body))
    sections: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        sections.append((title, content))
    return sections


def _plain_from_section(title: str, content: str) -> str:
    lines = [f"【{title}】"]
    for line in content.splitlines():
        s = line.strip()
        if not s or _is_table_sep(s):
            continue
        if s.startswith("|") and s.endswith("|"):
            s = " | ".join(c.strip() for c in s.strip("|").split("|"))
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = s.replace("&lt;", "<").replace("&gt;", ">")
        lines.append(s)
    return "\n".join(lines)


def _short_section_title(full_title: str) -> str:
    m = re.match(r"^[零一二三四五六七八九十百]+、(.+?)（", full_title)
    if m:
        return m.group(1).strip()
    return re.sub(r"^[零一二三四五六七八九十百]+、", "", full_title).split("（")[0].strip()


def _extract_names_from_section(title: str, content: str) -> str:
    """推送用：每节只保留个股名称或一行状态。"""
    buy_tags = ("可买", "突破", "反包", "N-204", "N-303", "N-402")
    buy_only = any(k in title for k in ("反包", "箱体", "科技", "花开"))
    names: list[str] = []

    def add_name(name: str) -> None:
        name = name.strip().strip("★").strip()
        if name and name not in names:
            names.append(name)

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and _is_table_sep(lines[i + 1])
        ):
            if "还差什么" in stripped:
                i += 1
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                continue
            header_cells = [c.strip() for c in stripped.strip("|").split("|")]
            name_idx = next(
                (idx for idx, h in enumerate(header_cells) if h == "名称"),
                None,
            )
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i]
                if buy_only and not any(tag in row for tag in buy_tags):
                    i += 1
                    continue
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                if name_idx is not None and name_idx < len(cells):
                    add_name(cells[name_idx])
                i += 1
            continue
        i += 1

    for line in content.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        text = s[2:].strip()
        if text.startswith("**") and "：" not in text:
            continue
        if text.startswith("**") and "：" in text:
            continue
        if text in ("（无）", "无观察标的"):
            return "（无）"
        if "、" in text or "，" in text:
            for part in re.split(r"[、,，]", text):
                add_name(part)
            continue
        if text and not text.startswith("（"):
            add_name(text)

    for m in re.finditer(
        r"(?:可买|观察)[】\*]*：?\s*(?:★\s*)?(?:sh\.|sz\.)?\d+\s+([^\s；、（]+)",
        content,
    ):
        add_name(m.group(1))

    if names:
        return "、".join(names)

    if title.startswith("三、Dragon") or "明日买入" in title:
        if any(k in content for k in ("不买入", "不新开仓")):
            return "不买入"
    if title.startswith("二、Dragon"):
        if "空仓" in content or "无合格主线" in content or "无主线" in content:
            return "无主线"
    if any(k in content for k in ("（无）", "无观察标的", "无 N-103", "无 N-204")):
        return "（无）"
    if "休眠" in content or "本节跳过" in content:
        return "跳过"
    return "（无）"


def _top_picks_detail_content(content: str) -> str:
    """综合优选：保留评分表 + #1/#2/#3 分项说明。"""
    lines_out: list[str] = []
    in_table = False
    for line in content.splitlines():
        s = line.strip()
        if not s:
            if in_table:
                in_table = False
            continue
        if s.startswith("|"):
            lines_out.append(line.rstrip())
            in_table = True
            continue
        if re.match(r"^- \*\*#\d", s):
            lines_out.append(line.rstrip())
    return "\n".join(lines_out).strip()


def _load_private_section(public_path: Path, title_prefix: str) -> str:
    private_path = public_path.with_name(
        public_path.name.replace("-日报-公开.md", "-日报.md")
    )
    if not private_path.exists():
        return ""
    for sec_title, sec_content in _split_sections(
        private_path.read_text(encoding="utf-8")
    ):
        if sec_title.startswith(title_prefix):
            return sec_content
    return ""


def build_message(public_path: Path, max_chars: int = 6000) -> Tuple[str, str, str]:
    body = public_path.read_text(encoding="utf-8")
    m = re.search(r"公开分享版）\s*(\d{4}-\d{2}-\d{2})", body)
    date = m.group(1) if m else public_path.stem.split("-")[0]
    title = f"交易日报 信号日{date}·次日执行（公开版）"

    meta = re.search(r"> (.+)", body)
    meta_text = meta.group(1) if meta else f"信号日 {date}"

    html_parts = [
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "font-size:14px;color:#1f1f1f;line-height:1.6;max-width:100%;\">",
        f"<div style='color:#666;font-size:12px;margin-bottom:12px;'>{_inline_md(meta_text)}</div>",
    ]
    text_parts = [title, meta_text.replace("**", ""), ""]

    skip_titles = ("换仓建议", "建议加入自选", "当前持仓", "次日执行清单")

    for sec_title, sec_content in _split_sections(body):
        if not sec_title.startswith("零、综合优选"):
            continue
        label = _short_section_title(sec_title)
        html_parts.append(
            f"<div style='margin:12px 0 4px;font-size:13px;color:#666;font-weight:600;'>"
            f"{_escape(label)}</div>"
        )
        private_content = _load_private_section(public_path, "零、综合优选")
        detail = _top_picks_detail_content(private_content) or sec_content
        html_parts.append(
            "<div style='background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;"
            "padding:8px 10px;margin-bottom:10px;'>"
        )
        html_parts.append(_render_md_block(detail))
        html_parts.append("</div>")
        text_parts.append(f"【{label}】")
        text_parts.append(_plain_from_section(sec_title, detail))
        break

    exec_content = _load_private_section(public_path, "十、次日执行清单")
    if exec_content:
        html_parts.append(
            "<div style='margin:14px 0 4px;font-size:13px;color:#666;font-weight:600;'>"
            "次日执行清单</div>"
        )
        html_parts.append(
            "<div style='background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;"
            "padding:8px 10px;margin-bottom:10px;'>"
        )
        html_parts.append(_render_md_block(exec_content))
        html_parts.append("</div>")
        text_parts.append("【次日执行清单】")
        text_parts.append(_plain_from_section("十、次日执行清单", exec_content))

    for sec_title, sec_content in _split_sections(body):
        if any(k in sec_title for k in skip_titles):
            continue
        if sec_title.startswith("零、综合优选"):
            continue
        if "科技" in sec_title and "（无）" in sec_content:
            continue

        label = _short_section_title(sec_title)
        html_parts.append(
            f"<div style='margin:12px 0 4px;font-size:13px;color:#666;font-weight:600;'>"
            f"{_escape(label)}</div>"
        )

        summary = _extract_names_from_section(sec_title, sec_content)
        if summary == "（无）" and sec_title.startswith("三、Dragon"):
            continue
        html_parts.append(
            f"<div style='margin:0 0 10px;font-size:15px;'>{_escape(summary)}</div>"
        )
        text_parts.append(f"【{label}】{summary}")

    html_parts.append(
        "<p style='color:#999;font-size:11px;margin-top:12px;border-top:1px solid #eee;"
        "padding-top:8px;'>公开精简版 · 综合优选与执行清单含详情</p></div>"
    )

    html = "".join(html_parts)
    text = "\n".join(text_parts).strip()
    if len(html) > max_chars:
        html = html[: max_chars - 60] + "<p style='color:#999;'>…（已截断）</p></div>"
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…（已截断）"
    return title, text, html


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def send_feishu(webhook: str, text: str) -> None:
    payload = {"msg_type": "text", "content": {"text": text}}
    result = _post_json(webhook, payload)
    ok = result.get("StatusCode") == 0 or result.get("code") == 0
    if not ok:
        raise RuntimeError(f"飞书推送失败: {result}")


def send_pushplus(token: str, title: str, html: str, topic: str = "") -> None:
    payload: Dict[str, Any] = {
        "token": token,
        "title": title,
        "content": html,
        "template": "html",
    }
    if topic:
        payload["topic"] = topic
    result = _post_json("https://www.pushplus.plus/send", payload)
    if result.get("code") != 200:
        raise RuntimeError(f"PushPlus 推送失败: {result}")


def send_serverchan(sendkey: str, title: str, html: str) -> None:
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {"title": title, "desp": html}
    result = _post_json(url, payload)
    if result.get("code") not in (0, None):
        raise RuntimeError(f"Server酱推送失败: {result}")


def send_briefing(
    signal_date: Optional[str] = None,
    config_path: Path = CONFIG_PATH,
    journal_dir: Path = JOURNAL_DIR,
    dry_run: bool = False,
) -> dict:
    if signal_date:
        public_path = journal_dir / f"{signal_date}-日报-公开.md"
    else:
        candidates = sorted(journal_dir.glob("*-日报-公开.md"), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"未找到公开日报: {journal_dir}")
        public_path = candidates[0]

    if not public_path.exists():
        raise FileNotFoundError(f"公开日报不存在: {public_path}")

    cfg: dict = {}
    if config_path.exists():
        cfg = load_config(config_path)
    elif not dry_run:
        raise FileNotFoundError(
            f"未找到 {config_path}，请复制 notify.yaml.example 为 notify.yaml 并填写 webhook/token"
        )

    if not dry_run and not cfg.get("enabled", True):
        return {"skipped": True, "reason": "notify.enabled=false"}

    max_chars = int(cfg.get("max_content_chars") or 6000)
    title, text, html = build_message(public_path, max_chars=max_chars)
    results: dict = {"file": str(public_path), "title": title}

    if dry_run:
        print(title)
        print("-" * 40)
        print(text)
        print("-" * 40)
        print(f"HTML 长度: {len(html)} 字符")
        return {"dry_run": True, "html": html, **results}

    feishu = (cfg.get("feishu_webhook") or "").strip()
    if feishu:
        send_feishu(feishu, text)
        results["feishu"] = "ok"

    pushplus = (cfg.get("pushplus_token") or "").strip()
    if pushplus:
        send_pushplus(
            pushplus,
            title,
            html,
            topic=str(cfg.get("pushplus_topic") or ""),
        )
        results["pushplus"] = "ok"

    sendkey = (cfg.get("serverchan_sendkey") or "").strip()
    if sendkey:
        send_serverchan(sendkey, title, html)
        results["serverchan"] = "ok"

    if not any(k in results for k in ("feishu", "pushplus", "serverchan")):
        raise ValueError(
            "notify.yaml 未配置 feishu_webhook / pushplus_token / serverchan_sendkey"
        )
    return results


def main():
    parser = argparse.ArgumentParser(description="推送公开版交易日报")
    parser.add_argument("--date", default=None, help="信号日 YYYY-MM-DD")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true", help="仅打印摘要不发送")
    parser.add_argument(
        "--html-preview",
        type=str,
        default="",
        help="将 HTML 写入本地文件预览（可与 --dry-run 同用）",
    )
    args = parser.parse_args()
    try:
        if args.dry_run and args.html_preview:
            public_path = (
                JOURNAL_DIR / f"{args.date}-日报-公开.md"
                if args.date
                else sorted(JOURNAL_DIR.glob("*-日报-公开.md"), reverse=True)[0]
            )
            cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) if Path(
                args.config
            ).exists() else {}
            max_chars = int((cfg or {}).get("max_content_chars") or 6000)
            title, _, html = build_message(public_path, max_chars=max_chars)
            Path(args.html_preview).write_text(html, encoding="utf-8")
            print(f"HTML 预览已写入: {args.html_preview}")

        result = send_briefing(
            signal_date=args.date,
            config_path=Path(args.config),
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        if CONFIG_EXAMPLE.exists():
            print(f"提示: cp {CONFIG_EXAMPLE} {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        print(f"❌ 推送失败: {e}", file=sys.stderr)
        sys.exit(1)

    if result.get("dry_run"):
        print("\n(dry-run，未发送)")
    elif result.get("skipped"):
        print(f"跳过推送: {result['reason']}")
    else:
        channels = [k for k in ("feishu", "pushplus", "serverchan") if k in result]
        print(f"✅ 已推送: {', '.join(channels)} ← {result['file']}")


if __name__ == "__main__":
    main()
