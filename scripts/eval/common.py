"""評估共用工具：報告 metadata、deterministic 抽樣、不覆蓋舊 baseline 的寫檔、唯讀 Supabase 包裝。"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

EVALUATION_VERSION = "1.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_DIR = REPO_ROOT / "eval_baselines"

T = TypeVar("T")


# ---------------------------------------------------------------- metadata / 報告


def git_info(root: Path = REPO_ROOT) -> dict[str, Any]:
    """目前 commit 與工作樹是否有未提交變更（失敗時回傳 None，不中斷評估）。"""
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=True)
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": (bool(status) if status is not None else None)}


def build_report(name: str, *, dataset_versions: dict[str, Any], parameters: dict[str, Any],
                 metrics: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
    git = git_info()
    report: dict[str, Any] = {
        "evaluation_version": EVALUATION_VERSION,
        "evaluation": name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "dataset_versions": dataset_versions,
        "parameters": parameters,
        "metrics": metrics,
    }
    if details:
        report["details"] = details
    return report


def write_report(report: dict[str, Any], path: Path, *, overwrite: bool = False) -> Path:
    """寫出 JSON 報告。已存在的檔案（舊 baseline）預設拒絕覆蓋。"""
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} 已存在；baseline 不會被自動覆蓋（請換檔名，或明確指定 --overwrite）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def default_output_path(name: str, root: Path = DEFAULT_BASELINE_DIR) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / f"{name}-{stamp}.json"


def rate(numerator: int, denominator: int) -> float | None:
    """分母為 0 時回傳 None（不是 0，避免把「沒有樣本」誤讀成「沒有錯誤」）。"""
    return round(numerator / denominator, 6) if denominator else None


def sha256_of(items: Sequence[Any]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------- deterministic 抽樣


def deterministic_sample(items: Sequence[T], size: int, seed: int | str, key: Callable[[T], Any]) -> list[T]:
    """先依 key 排序（與輸入順序無關），再以固定 seed 抽樣；結果也依 key 排序。"""
    ordered = sorted(items, key=key)
    if size >= len(ordered):
        return ordered
    picked = random.Random(f"sample:{seed}").sample(range(len(ordered)), size)
    return [ordered[i] for i in sorted(picked)]


# ---------------------------------------------------------------- 唯讀保證


class EvalWriteAttempt(RuntimeError):
    """評估程式嘗試寫入 production 資料。"""


READ_ONLY_RPC = frozenset({"search_products", "search_entity_aliases"})


class _ReadOnlyTable:
    def __init__(self, table: Any):
        self._table = table

    def select(self, *args: Any, **kwargs: Any) -> Any:
        return self._table.select(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        raise EvalWriteAttempt(f"評估為唯讀：不允許對資料表呼叫 {name}()")


class ReadOnlySupabase:
    """包裝 SupabaseService，只放行 select / 白名單 RPC；任何寫入方法一律擲出 EvalWriteAttempt。"""

    def __init__(self, service: Any, rpc_allowlist: frozenset[str] = READ_ONLY_RPC):
        self._service = service
        self._rpc_allowlist = rpc_allowlist

    def table(self, name: str) -> _ReadOnlyTable:
        return _ReadOnlyTable(self._service.table(name))

    def select(self, builder: Any) -> list[dict[str, Any]]:
        return self._service.select(builder)

    def rpc(self, fn: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if fn not in self._rpc_allowlist:
            raise EvalWriteAttempt(f"評估只允許唯讀 RPC {sorted(self._rpc_allowlist)}，拒絕 {fn}")
        return self._service.rpc(fn, params)

    def source_info(self, source_id: str | None) -> dict[str, Any]:
        return self._service.source_info(source_id)

    def is_project_database(self) -> bool:
        return self._service.is_project_database()

    def upsert(self, *args: Any, **kwargs: Any) -> Any:
        raise EvalWriteAttempt("評估為唯讀：不允許 upsert")

    def update(self, *args: Any, **kwargs: Any) -> Any:
        raise EvalWriteAttempt("評估為唯讀：不允許 update")

    def insert(self, *args: Any, **kwargs: Any) -> Any:
        raise EvalWriteAttempt("評估為唯讀：不允許 insert")
