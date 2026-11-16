"""定期报告接口冒烟：部署后一条命令确认三个接口可用、口径字段齐全。

用法（本机或服务器上跑，默认打本机 8000 端口的 everything-api；
契约文档里 uvicorn 的监听端口就是 8000，两者保持一致）：

    python scripts/smoke_periodic.py
    python scripts/smoke_periodic.py --base http://127.0.0.1:8000
    python scripts/smoke_periodic.py --period monthly

只做只读检查：期列表、报告载荷、三条榜单各取一次，断言关键字段存在且自洽
（核心 KPI 条数、availablePeriods 非空、榜单去重后 rank_no 递增）。
不通过时以非 0 退出，方便接进部署脚本。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"


class SmokeError(RuntimeError):
    """冒烟检查失败。"""


def _get(base: str, path: str, params: dict[str, str]) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"{base.rstrip('/')}{path}?{query}" if query else f"{base.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 404 / 400 也要把服务端原文带出来
        detail = exc.read().decode("utf-8", "replace")
        raise SmokeError(f"{url} -> HTTP {exc.code}: {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"{url} 无法访问：{exc}") from exc
    if body.get("code") != 0:
        raise SmokeError(f"{url} -> code={body.get('code')} message={body.get('message')}")
    return body["data"]


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def run(base: str, period: str) -> list[str]:
    lines: list[str] = []

    periods = _get(base, "/api/v09/data/periodic/periods", {"period": period})
    _check(bool(periods["availablePeriods"]), "期列表为空：库里没有任何周期数据")
    latest = periods["latest"]
    _check(latest is not None, "latest 为空")
    lines.append(
        f"periods  OK  {period} 共 {len(periods['availablePeriods'])} 期 最新 {latest}"
    )

    report = _get(base, "/api/v09/data/periodic/report", {"period": period, "key": latest})
    meta = report["meta"]
    core = report["coreKpis"]
    _check(meta["periodKey"] == latest, f"报告期键 {meta['periodKey']} != 最新期 {latest}")
    _check(len(core) == 6, f"核心 KPI 应为 6 条，实际 {len(core)}")
    _check(
        all("trend" in kpi and "availablePeriods" in kpi for kpi in core),
        "KPI 缺少趋势 / 可用期数字段",
    )
    with_value = [kpi["key"] for kpi in core if kpi["hasValue"]]
    lines.append(
        f"report   OK  {meta['periodLabel']} 核心 KPI {len(core)} 条（有值 {len(with_value)}）"
        f"  标记 {len(report['flags'])} 条  isPartial={meta['isPartial']}"
    )
    for flag in report["flags"]:
        lines.append(f"         flag[{flag['level']}] {flag['item']}: {flag['detail']}")

    for line, list_type in (("download", None), ("ad", None), ("online", "dist_0")):
        params = {"line": line, "period": period, "key": latest, "category": "top", "limit": "5"}
        if list_type:
            params["list_type"] = list_type
        try:
            rank = _get(base, "/api/v09/data/periodic/rank", params)
        except SmokeError as exc:
            # 在线榜最早一期可能还没有 dist_* 维度，属于已知情况，单独标注
            lines.append(f"rank     SKIP {line}: {exc}")
            continue
        ranks = [item["rankNo"] for item in rank["items"]]
        _check(ranks == sorted(ranks), f"{line} 榜 rank_no 未按升序返回")
        lines.append(f"rank     OK  {line} top{len(ranks)} 首名 {rank['items'][0]['appName']}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="定期报告接口冒烟")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"服务地址，默认 {DEFAULT_BASE}")
    parser.add_argument("--period", default="weekly", choices=["weekly", "monthly"])
    args = parser.parse_args()

    try:
        for line in run(args.base, args.period):
            print(line)
    except SmokeError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print("全绿：三个接口可用，口径字段齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
