"""GitHub のトラフィックを日次で収集して DynamoDB に貯め、ダッシュボード用の集計 JSON を S3 に書き出す。

GitHub の Traffic API は直近 14 日分しか返さないため、毎日取得して日別の値を積み上げる。
追跡対象は「実行時点で public な自分のリポジトリ」で、毎回一覧を取り直すので
新規作成や private からの公開切り替えも次回の実行から自動で対象になる。
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from urllib.request import Request, urlopen

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET_NAME = os.environ["BUCKET_NAME"]
TOKEN_PARAMETER_NAME = os.environ["TOKEN_PARAMETER_NAME"]
SUMMARY_KEY = "data/summary.json"
GITHUB_API = "https://api.github.com"

table = boto3.resource("dynamodb").Table(TABLE_NAME)
s3 = boto3.client("s3")
ssm = boto3.client("ssm")


def get_token():
    res = ssm.get_parameter(Name=TOKEN_PARAMETER_NAME, WithDecryption=True)
    return res["Parameter"]["Value"]


def github_get(path, token):
    """GET してボディと次ページの URL を返す。"""
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-traffic-dashboard",
        },
    )
    with urlopen(req, timeout=30) as res:
        body = json.loads(res.read())
        next_url = None
        for part in (res.headers.get("Link") or "").split(","):
            if 'rel="next"' in part:
                next_url = part[part.index("<") + 1 : part.index(">")]
        return body, next_url


def list_public_repos(token):
    """認証ユーザーがオーナーの public リポジトリを全件返す。"""
    repos = []
    url = "/user/repos?visibility=public&affiliation=owner&per_page=100"
    while url:
        page, url = github_get(url, token)
        repos.extend(page)
    return repos


def fetch_traffic(repo, token):
    name = repo["full_name"]
    views, _ = github_get(f"/repos/{name}/traffic/views?per=day", token)
    clones, _ = github_get(f"/repos/{name}/traffic/clones?per=day", token)
    referrers, _ = github_get(f"/repos/{name}/traffic/popular/referrers", token)
    paths, _ = github_get(f"/repos/{name}/traffic/popular/paths", token)
    return {"views": views, "clones": clones, "referrers": referrers, "paths": paths}


def to_days(traffic):
    """views / clones の配列を {日付: 値} にまとめる。"""
    days = {}
    for item in traffic["views"].get("views", []):
        d = days.setdefault(item["timestamp"][:10], {})
        d["views"] = item["count"]
        d["viewsUniques"] = item["uniques"]
    for item in traffic["clones"].get("clones", []):
        d = days.setdefault(item["timestamp"][:10], {})
        d["clones"] = item["count"]
        d["clonesUniques"] = item["uniques"]
    return days


def save_repo(repo, traffic, today, now):
    pk = f"REPO#{repo['id']}"

    # 既存の日別値と比べて大きい方を残す。当日分は途中経過なので後の実行で増えていく
    existing = {}
    res = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("DAY#"))
    for item in res["Items"]:
        existing[item["sk"][4:]] = item

    with table.batch_writer() as batch:
        for date, values in to_days(traffic).items():
            old = existing.get(date, {})
            item = {"pk": pk, "sk": f"DAY#{date}", "updatedAt": now}
            for field in ("views", "viewsUniques", "clones", "clonesUniques"):
                item[field] = max(int(old.get(field, 0)), values.get(field, 0))
            batch.put_item(Item=item)

        batch.put_item(
            Item={
                "pk": pk,
                "sk": f"SNAP#{today}",
                "referrers": json.dumps(traffic["referrers"]),
                "paths": json.dumps(traffic["paths"]),
                "updatedAt": now,
            }
        )

    # firstSeen は初回だけ入れる
    table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression=(
            "SET fullName = :n, htmlUrl = :u, description = :d, stars = :s, forks = :f, "
            "isFork = :fk, archived = :a, lastSeen = :now, firstSeen = if_not_exists(firstSeen, :now)"
        ),
        ExpressionAttributeValues={
            ":n": repo["full_name"],
            ":u": repo["html_url"],
            ":d": repo.get("description") or "",
            ":s": repo["stargazers_count"],
            ":f": repo["forks_count"],
            ":fk": repo["fork"],
            ":a": repo["archived"],
            ":now": now,
        },
    )


def scan_all():
    items, kwargs = [], {}
    while True:
        res = table.scan(**kwargs)
        items.extend(res["Items"])
        if "LastEvaluatedKey" not in res:
            return items
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def build_summary(active_ids, now):
    repos = {}
    for item in scan_all():
        repo = repos.setdefault(item["pk"], {"daily": [], "snapshot": None})
        sk = item["sk"]
        if sk == "META":
            repo["meta"] = item
        elif sk.startswith("DAY#"):
            repo["daily"].append(
                {
                    "date": sk[4:],
                    "views": int(item.get("views", 0)),
                    "viewsUniques": int(item.get("viewsUniques", 0)),
                    "clones": int(item.get("clones", 0)),
                    "clonesUniques": int(item.get("clonesUniques", 0)),
                }
            )
        elif sk.startswith("SNAP#"):
            # 最新のスナップショットだけ使う
            if repo["snapshot"] is None or sk > repo["snapshot"]["sk"]:
                repo["snapshot"] = item

    result = []
    for pk, repo in repos.items():
        meta = repo.get("meta")
        if not meta:
            continue
        daily = sorted(repo["daily"], key=lambda d: d["date"])
        snap = repo["snapshot"]
        result.append(
            {
                "id": pk[5:],
                "fullName": meta["fullName"],
                "htmlUrl": meta["htmlUrl"],
                "description": meta.get("description", ""),
                "stars": int(meta.get("stars", 0)),
                "forks": int(meta.get("forks", 0)),
                "isFork": bool(meta.get("isFork", False)),
                "archived": bool(meta.get("archived", False)),
                "active": pk[5:] in active_ids,
                "firstSeen": meta.get("firstSeen"),
                "lastSeen": meta.get("lastSeen"),
                "totals": {
                    field: sum(d[field] for d in daily)
                    for field in ("views", "viewsUniques", "clones", "clonesUniques")
                },
                "daily": daily,
                "referrers": json.loads(snap["referrers"]) if snap else [],
                "paths": json.loads(snap["paths"]) if snap else [],
                "snapshotDate": snap["sk"][5:] if snap else None,
            }
        )
    result.sort(key=lambda r: r["totals"]["views"], reverse=True)
    return {"generatedAt": now, "repos": result}


def handler(event, context):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]
    token = get_token()

    repos = list_public_repos(token)
    logger.info("public repos: %d", len(repos))

    def fetch(repo):
        try:
            return fetch_traffic(repo, token)
        except Exception:
            logger.exception("fetch failed: %s", repo["full_name"])
            return None

    # GitHub への取得だけ並列にする。boto3 の resource はスレッドセーフではないので書き込みは逐次
    with ThreadPoolExecutor(max_workers=8) as pool:
        traffics = list(pool.map(fetch, repos))

    failed = []
    for repo, traffic in zip(repos, traffics):
        if traffic is None:
            failed.append(repo["full_name"])
            continue
        save_repo(repo, traffic, today, now)

    if repos and len(failed) == len(repos):
        raise RuntimeError("all repositories failed")

    summary = build_summary({str(r["id"]) for r in repos}, now)
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=SUMMARY_KEY,
        Body=json.dumps(summary, ensure_ascii=False, default=lambda o: int(o) if isinstance(o, Decimal) else str(o)),
        ContentType="application/json",
        CacheControl="max-age=300",
    )
    return {"repos": len(repos), "failed": failed}
