from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlparse


DEFAULT_BASE_URL = "https://app.pixmax.cn/openapi"
_RUNNING = {"QUEUE", "QUEUED", "WAITING", "WAITING_CONCURRENCY", "PENDING", "RUNNING", "PROCESSING"}
_SUCCESS = {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED", "DONE"}
_FAILED = {"FAILED", "FAIL", "ERROR", "CANCEL", "CANCELED", "CANCELLED", "ABORTED"}


class PixmaxApiError(RuntimeError):
    """Pixmax API 错误，可直接展示给用户。"""

def normalize_base_url(value: Any = "") -> str:
    text = str(value or os.getenv("PIXMAX_OPENAPI_BASE") or DEFAULT_BASE_URL).strip()
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        raise PixmaxApiError("Pixmax  API 地址无效。")
    path = parsed.path.rstrip("/") or "/openapi"
    if not path.endswith("/openapi"):
        path = f"{path}/openapi"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(value: Any, keys: tuple[str, ...]) -> str:
    for item in _walk(value):
        for key in keys:
            candidate = item.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
    return ""


def _status(value: Any) -> str:
    return _first(value, ("status", "state", "taskStatus", "taskState")).upper()


def _message(value: Any, fallback: str = "Pixmax  API 返回未知错误") -> str:
    for item in _walk(value):
        candidate = (
            item.get("errMessage")
            or item.get("errorMessage")
            or item.get("message")
            or item.get("providerErrorMsgZh")
            or item.get("providerErrorMsg")
            or item.get("replaceProviderErrorMsg")
        )
        if isinstance(candidate, str) and candidate.strip():
            return str(candidate)
        code = item.get("errCode") or item.get("errorCode")
        if code:
            return str(code)
    return fallback


def _redact(value: Any, secret: str) -> str:
    text = str(value or "")
    return text.replace(secret, "[已隐藏]") if secret else text


def _param_name(row: dict[str, Any]) -> str:
    return str(
        row.get("name")
        or row.get("paramName")
        or row.get("param_name")
        or row.get("key")
        or row.get("field")
        or ""
    ).strip()


def _model_aliases(row: dict[str, Any]) -> list[str]:
    raw = row.get("modelCodeAlias") or row.get("model_code_alias") or row.get("aliases") or []
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = [raw]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _param_map(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {_param_name(row): row for row in value if isinstance(row, dict) and _param_name(row)}
    return {}


def _model_rows(response: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _walk(response):
        code = str(
            item.get("modelCode")
            or item.get("model_code")
            or item.get("modelId")
            or item.get("code")
            or ""
        ).strip()
        if not code or code in seen:
            continue
        item_type = str(item.get("nodeType") or item.get("canvasNodeType") or item.get("modelType") or "").strip()
        if item_type and item_type.upper() not in {node_type.upper(), node_type.replace("GENERATE_", "").upper()}:
            continue
        seen.add(code)
        params = _param_map(
            item.get("params")
            or item.get("parameters")
            or item.get("parameterSchema")
            or item.get("schema")
        )
        if not params:
            config = item.get("modelConfig") if isinstance(item.get("modelConfig"), dict) else {}
            params = _param_map(
                config.get("params")
                or config.get("parameters")
                or config.get("parameterSchema")
                or config.get("schema")
            )
        rows.append({
            "code": code,
            "name": str(item.get("modelName") or item.get("name") or code).strip(),
            "node_type": item_type or node_type,
            "params": params or {},
            "aliases": _model_aliases(item),
        })
    return rows


def _project_rows(response: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _walk(response):
        project_uuid = str(item.get("projectUuid") or item.get("projectUUID") or item.get("uuid") or "").strip()
        if not project_uuid or project_uuid in seen:
            continue
        if not any(key in item for key in ("projectUuid", "projectUUID", "projectName", "project_name", "projectId")) and not item.get("name"):
            continue
        seen.add(project_uuid)
        rows.append({
            "uuid": project_uuid,
            "name": str(item.get("projectName") or item.get("name") or project_uuid).strip(),
        })
    return rows


def _asset_urls(value: Any, base_url: str) -> list[str]:
    keys = ("fullUrl", "fileUrl", "webUrl", "downloadUrl", "assetUrl", "imageUrl", "videoUrl", "url")
    found: list[str] = []
    seen: set[str] = set()
    for item in _walk(value):
        for key in keys:
            raw = item.get(key)
            if not raw or not isinstance(raw, str):
                continue
            raw = raw.strip()
            if raw.startswith("data:") or raw.startswith("http://") or raw.startswith("https://"):
                url = raw
            else:
                parsed_base = urlparse(base_url)
                origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.netloc else base_url.rstrip("/")
                url = f"{origin}/{raw.lstrip('/')}"
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def find_asset_uuids(value: Any) -> list[str]:

    found: list[str] = []
    seen: set[str] = set()

    def add_from_asset(asset: Any) -> None:
        if not isinstance(asset, dict):
            return
        asset_uuid = _first(asset, ("assetsUuid", "assetUuid", "assetUUID"))
        if asset_uuid and asset_uuid not in seen:
            seen.add(asset_uuid)
            found.append(asset_uuid)

    def walk_results(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in {"resultassets", "resultassetlist"}:
                    if isinstance(child, list):
                        for asset in child:
                            add_from_asset(asset)
                    else:
                        add_from_asset(child)
                walk_results(child)
        elif isinstance(node, list):
            for child in node:
                walk_results(child)

    walk_results(value)
    return found


class PixmaxApiClient:

    def __init__(self, api_key: str, base_url: str = "", timeout: int = 90, opener: Callable[..., Any] | None = None):
        key = str(api_key or os.getenv("PIXMAX_OPENAPI_KEY") or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise PixmaxApiError("请先填写 Pixmax  API Key。")
        self.api_key = key
        self.base_url = normalize_base_url(base_url)
        self.timeout = max(1, int(timeout))
        self.opener = opener or urllib.request.urlopen

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            data=body,
            method=method.upper(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "typetale-pixmax-openapi/2.0",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                status_code = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise PixmaxApiError(f"Pixmax  API HTTP {exc.code}: {_redact(detail, self.api_key)[:400]}") from exc
        except urllib.error.URLError as exc:
            raise PixmaxApiError(f"Pixmax  API 网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise PixmaxApiError("Pixmax  API 请求超时。") from exc
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PixmaxApiError(f"Pixmax  API 返回的不是 JSON（HTTP {status_code}）。") from exc
        if not isinstance(result, dict):
            raise PixmaxApiError("Pixmax  API 返回了无法识别的数据。")
        if status_code >= 400 or result.get("success") is False:
            request_id = _first(result, ("requestId", "requestID", "traceId"))
            suffix = f"，请求 ID：{request_id}" if request_id else ""
            raise PixmaxApiError(f"Pixmax  API 错误：{_redact(_message(result), self.api_key)}{suffix}")
        return result

    def available_models(self, node_type: str) -> list[dict[str, Any]]:
        models = _model_rows(self._request("POST", "/model/available", {}), node_type)
        try:
            config_rows = _model_rows(self._request("POST", "/model/config", {}), node_type)
        except PixmaxApiError:
            config_rows = []
        by_code: dict[str, dict[str, Any]] = {}
        for row in config_rows:
            for key in [row.get("code"), *(row.get("aliases") or [])]:
                if key:
                    by_code[str(key)] = row
        for row in models:
            config = by_code.get(row["code"])
            if config and config.get("params"):
                row["params"] = config["params"]
                row["param_source"] = "official"
            else:
                row["param_source"] = "unavailable"
        return models

    def list_projects(self) -> list[dict[str, str]]:
        return _project_rows(self._request("POST", "/project/list", {}))

    def ensure_project(self, project_uuid: str = "") -> str:
        selected = str(project_uuid or "").strip()
        if selected:
            return selected
        projects = self.list_projects()
        if projects:
            return projects[0]["uuid"]
        for payload in ({"projectName": "字字动画 Pixmax API 项目"}, {"name": "字字动画 Pixmax API 项目"}):
            try:
                response = self._request("POST", "/project/createOrUpdate", payload)
                selected = _first(response, ("projectUuid", "projectUUID", "projectId", "uuid"))
                if selected:
                    return selected
            except PixmaxApiError:
                continue
        raise PixmaxApiError(" API 无法自动创建项目，请检查账号权限。")

    def connect(self, node_type: str, project_uuid: str = "") -> tuple[str, list[dict[str, Any]]]:
        selected = self.ensure_project(project_uuid)
        models = self.available_models(node_type)
        if not models:
            kind = "图片" if node_type == "GENERATE_IMAGE" else "视频"
            raise PixmaxApiError(f" API 没有返回可用的{kind}模型。")
        return selected, models

    def _multipart_request(self, endpoint: str, project_uuid: str, file_path: str) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise PixmaxApiError(f"参考素材不存在：{path}")
        boundary = f"----TypetalePixmax{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            data=b"".join(chunks),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                status_code = getattr(response, "status", 200)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise PixmaxApiError(f"Pixmax 素材上传失败：{exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PixmaxApiError(f"素材上传返回的不是 JSON（HTTP {status_code}）。") from exc
        if not isinstance(result, dict) or status_code >= 400 or result.get("success") is False:
            raise PixmaxApiError(f"Pixmax 素材上传失败：{_redact(_message(result), self.api_key)}")
        return result

    def upload_asset(self, project_uuid: str, file_path: str) -> str:
        response = self._multipart_request("/assets/upload", project_uuid, file_path)
        asset_uuid = _first(response, ("assetsUuid", "assetUuid", "assetUUID", "uuid"))
        if not asset_uuid:
            raise PixmaxApiError("素材上传成功，但没有返回资产 UUID。")
        return asset_uuid

    def check_asset_compliance(self, asset_uuids: list[str]) -> None:
        seen: set[str] = set()
        for raw_asset_uuid in asset_uuids or []:
            asset_uuid = str(raw_asset_uuid or "").strip()
            if not asset_uuid or asset_uuid in seen:
                continue
            seen.add(asset_uuid)
            self._request("POST", "/assetLibrary/compliance/check", {"assetUuid": asset_uuid})

    def submit_task(self, project_uuid: str, node_type: str, model: str, prompt: str, params: dict[str, Any], input_asset_uuids: list[str] | None = None) -> str:
        reserved = {"prompt", "model", "nodeType"}
        safe_params = {key: value for key, value in params.items() if str(key) not in reserved}
        payload = {
            "projectUuid": project_uuid,
            "inputAssetUuids": list(input_asset_uuids or []),
            "inputTexts": [],
            "params": {"prompt": prompt, "model": model, "nodeType": node_type, **safe_params},
        }
        response = self._request("POST", "/task/submit", payload)
        task_uuid = _first(response, ("taskUuid", "taskUUID", "taskId", "uuid"))
        if not task_uuid:
            raise PixmaxApiError(" API 已接受任务，但没有返回 taskUuid。")
        return task_uuid

    def task_detail(self, task_uuid: str) -> dict[str, Any]:
        return self._request("POST", "/task/detail", {"taskUuid": task_uuid})

    def generate(self, project_uuid: str, node_type: str, model: str, prompt: str, params: dict[str, Any], input_asset_uuids: list[str] | None = None, timeout: int = 1800, interval: float = 5.0, progress_callback: Callable[..., Any] | None = None) -> tuple[dict[str, Any], list[str]]:
        task_uuid = self.submit_task(project_uuid, node_type, model, prompt, params, input_asset_uuids)
        _report(progress_callback, "Pixmax  API 已提交任务", 10)
        deadline = time.monotonic() + max(1, int(timeout))
        while True:
            response = self.task_detail(task_uuid)
            status = _status(response) or "PENDING"
            if status in _SUCCESS:
                _report(progress_callback, "Pixmax  API 任务已完成", 100)
                return response, _asset_urls(response, self.base_url)
            if status in _FAILED:
                raise PixmaxApiError(f"Pixmax  API 生成失败：{_message(response, status)}")
            _report(progress_callback, "Pixmax  API 排队中" if status in _RUNNING - {"RUNNING", "PROCESSING"} else "Pixmax  API 生成中", None)
            if time.monotonic() >= deadline:
                raise PixmaxApiError(f"Pixmax  API 任务超时（{timeout} 秒）。")
            time.sleep(max(0.2, float(interval)))


def _report(callback: Callable[..., Any] | None, message: str, percent: int | None) -> None:
    if not callable(callback):
        return
    try:
        callback(message, percent)
    except TypeError:
        callback(message)
    except Exception:
        pass


def find_asset_urls(value: Any, base_url: str = "") -> list[str]:
    return _asset_urls(value, normalize_base_url(base_url))


def save_asset(source: str, output_path: str, timeout: int, api_key: str = "", base_url: str = "") -> None:
    if source.startswith("data:"):
        header, encoded = source.split(",", 1)
        Path(output_path).write_bytes(base64.b64decode(encoded))
    elif source.startswith(("http://", "https://")):
        source_host = (urlparse(source).hostname or "").lower()
        api_host = (urlparse(normalize_base_url(base_url)).hostname or "").lower()
        pixmax_host = source_host == "pixmax.cn" or source_host.endswith(".pixmax.cn")
        same_host = bool(source_host and source_host == api_host)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key and (same_host or pixmax_host) else {}
        request = urllib.request.Request(source, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout))) as response:
                Path(output_path).write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            suffix = f"：{detail[:400]}" if detail else f"（{exc.reason}）"
            raise PixmaxApiError(f"结果素材下载失败：HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PixmaxApiError(f"结果素材下载失败：{exc}") from exc
    else:
        try:
            Path(output_path).write_bytes(base64.b64decode(source, validate=True))
        except Exception as exc:
            raise PixmaxApiError(" API 返回的素材地址无法识别。") from exc
    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise PixmaxApiError(" API 返回了空素材。")
