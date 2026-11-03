"""함대 워크플로 차례 기다리기 — 먼저 만들어진 실행이 끝날 때까지 기다립니다.

예전에는 함대 실행·관리·사실 갱신·기존 글 정리가 concurrency 그룹 하나(fleet)를 같이 썼습니다.
GitHub 은 그룹마다 '대기 중' 실행을 하나만 두고, 새 실행이 들어오면 대기 중이던 것을 취소합니다.
그래서 정리 작업이 길게 도는 동안 대기하던 관리 에이전트가 뒤에 온 함대 실행에 밀려 취소됐습니다
(2026-10-31 사실 갱신, 2026-11-01 사실 갱신·관리 에이전트).

이제 워크플로마다 그룹을 따로 두고(같은 워크플로끼리만 밀어냄), 서로 겹치지 않게 하는 일은 이 스크립트가 합니다.
네 워크플로 가운데 나보다 먼저 만들어진(run id 가 작은) 실행이 아직 안 끝났으면 끝날 때까지 기다립니다.
먼저 온 순서대로 돌기 때문에 서로 기다리다 멈추는 일은 없습니다.

  python3 scripts/wait_turn.py --max-wait 200 --on-timeout go     # 관리·정리: 늦더라도 돕니다
  python3 scripts/wait_turn.py --max-wait 90 --on-timeout skip    # 함대 실행: 이번엔 건너뜀 (밀린 슬롯은 다음 실행이 따라잡음)

결과는 GITHUB_OUTPUT 의 go=true|false 로 남깁니다. GitHub API 를 못 읽으면 예전처럼 그냥 돕니다(go=true).
setup-python 전에 돌므로 표준 라이브러리만 씁니다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

WATCH = ("fleet.yml", "manager.yml", "refresh.yml", "retrofit.yml")
OPEN_STATUSES = ("in_progress", "queued", "waiting", "pending", "requested")


def blockers(runs: list[dict], own_id: int) -> list[dict]:
    """나보다 먼저 만들어졌고 아직 안 끝난, 함대 워크플로 실행들."""
    seen, out = set(), []
    for r in runs:
        name = str(r.get("path", "")).split("@")[0].rsplit("/", 1)[-1]
        rid = int(r.get("id", 0))
        if name in WATCH and rid < own_id and r.get("status") != "completed" and rid not in seen:
            seen.add(rid)
            out.append(r)
    return out


def _open_runs(repo: str, token: str) -> list[dict]:
    runs = []
    for status in OPEN_STATUSES:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/runs?status={status}&per_page=100",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            runs += json.load(resp).get("workflow_runs", [])
    return runs


def _output(go: bool) -> None:
    print(f"go={'true' if go else 'false'}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wait", type=int, default=200, help="최대 대기 분")
    ap.add_argument("--on-timeout", choices=("go", "skip"), default="go")
    ap.add_argument("--poll", type=int, default=60, help="확인 간격 초")
    args = ap.parse_args()

    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    own = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if not (repo and token and own):
        print("GitHub 실행 정보가 없어 기다리지 않습니다.")
        _output(True)
        return 0

    deadline = time.monotonic() + args.max_wait * 60
    while True:
        try:
            ahead = blockers(_open_runs(repo, token), own)
        except Exception as e:  # API 를 못 읽으면 예전처럼 돕니다 (concurrency 그룹 하나일 때보다 나빠지지 않게).
            print(f"::warning::실행 목록을 읽지 못해 기다리지 않고 진행합니다: {e}")
            _output(True)
            return 0
        if not ahead:
            print("앞선 함대 실행 없음 — 진행합니다.")
            _output(True)
            return 0
        names = ", ".join(f"{r.get('name')}#{r.get('id')}({r.get('status')})" for r in ahead)
        if time.monotonic() >= deadline:
            if args.on_timeout == "skip":
                print(f"{args.max_wait}분 기다려도 앞선 실행이 남아 이번 실행은 건너뜁니다: {names}")
                _output(False)
            else:
                print(f"::warning::{args.max_wait}분 기다려도 앞선 실행이 남았지만 진행합니다: {names}")
                _output(True)
            return 0
        print(f"앞선 실행을 기다립니다: {names}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
