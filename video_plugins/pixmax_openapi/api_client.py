"""字字动画专用的 Pixmax OpenAPI 客户端。

这个文件只处理API，不依赖旧版网页登录、画布 UUID 或 CLI。
"""

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
    """API 可直接展示给用户的错误。"""


def normalize_base_url(value: Any = "") -> str:
    text = str(value or os.getenv("PIXMAX_OPENAPI_BASE") or DEFAULT_BASE_URL).strip()
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        raise PixmaxApiError("Pixmax API 地址无效。")
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


def _message(value: Any, fallback: str = "Pixmax API 返回未知错误") -> str:
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


def _asset_urls(
    value: Any,
    base_url: str,
    media_type: str = "",
    excluded_asset_uuids: set[str] | None = None,
) -> list[str]:
    keys = (
        "downloadUrl", "downloadURL", "fullUrl", "fullURL", "fileUrl", "fileURL", "uri",
        "assetUrl", "resultUrl", "webUrl", "videoUrl", "imageUrl", "url", "src", "ossUrl",
        "previewWebUrl", "thumbnailWebUrl", "thumbnailVideoWebUrl",
    )
    wanted = str(media_type or "").strip().lower()
    found: list[str] = []
    seen: set[str] = set()
    for item in _walk(value):
        if excluded_asset_uuids:
            item_asset_uuid = _first(item, ("assetsUuid", "assetUuid", "assetUUID"))
            if item_asset_uuid in excluded_asset_uuids:
                continue
        if wanted:
            type_text = " ".join(
                str(item.get(key) or "")
                for key in ("type", "assetType", "assetsType", "resultAssetType", "mediaType", "mimeType", "contentType", "fileType", "resourceType")
            ).lower()
        for key in keys:
            raw = item.get(key)
            if not raw or not isinstance(raw, str):
                continue
            raw = raw.strip()
            lower = raw.lower()
            is_video_url = lower.startswith("data:video/") or bool(re.search(r"\.(?:mp4|mov|m4v|webm|avi|mkv)(?:[?#].*)?$", lower))
            is_image_url = lower.startswith("data:image/") or bool(re.search(r"\.(?:png|jpe?g|webp|gif|bmp|avif)(?:[?#].*)?$", lower))
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            explicit_video_key = "video" in normalized_key
            ambiguous_url_key = normalized_key in {
                "url", "weburl", "src", "previewweburl", "thumbnailweburl", "thumbnailvideoweburl"
            }
            if wanted == "video" and (("image" in type_text and "video" not in type_text) or (is_image_url and not is_video_url)):
                continue
            if wanted == "image" and ("video" in type_text or (is_video_url and not is_image_url)):
                continue
            if wanted == "video" and ambiguous_url_key and not (explicit_video_key or "video" in type_text or is_video_url):
                # 通用 url/webUrl 在没有媒体类型时可能只是参考图预览，不能作为视频兜底。
                continue
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


def _asset_url_from_item(
    item: Any,
    base_url: str,
    media_type: str = "",
    excluded_asset_uuids: set[str] | None = None,
) -> list[str]:
    """从单个输出资产对象提取地址，并按媒体类型过滤。"""

    if not isinstance(item, dict):
        return []
    if excluded_asset_uuids:
        item_asset_uuid = _first(item, ("assetsUuid", "assetUuid", "assetUUID"))
        if item_asset_uuid in excluded_asset_uuids:
            return []
    wanted = str(media_type or "").strip().lower()
    type_text = " ".join(
        str(item.get(key) or "")
        for key in ("type", "assetType", "assetsType", "resultAssetType", "mediaType", "mimeType", "contentType", "fileType", "resourceType")
    ).lower()
    keys = (
        (
            "videoUrl", "videoURL", "video_url", "downloadUrl", "downloadURL", "fullUrl", "fullURL",
            "fileUrl", "fileURL", "uri", "resultUrl", "webUrl", "assetUrl", "url", "src", "ossUrl",
            "previewWebUrl", "thumbnailVideoWebUrl",
        )
        if wanted == "video"
        else (
            "imageUrl", "imageURL", "image_url", "downloadUrl", "downloadURL", "fullUrl", "fullURL",
            "fileUrl", "fileURL", "uri", "resultUrl", "webUrl", "assetUrl", "url", "src", "ossUrl",
            "previewWebUrl", "thumbnailWebUrl",
        )
        if wanted == "image"
        else (
            "downloadUrl", "downloadURL", "fullUrl", "fullURL", "fileUrl", "fileURL", "uri",
            "assetUrl", "resultUrl", "webUrl", "videoUrl", "imageUrl", "url", "src", "ossUrl",
            "previewWebUrl", "thumbnailWebUrl", "thumbnailVideoWebUrl",
        )
    )
    found: list[str] = []
    for key in keys:
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        lower = raw.lower()
        is_video_url = lower.startswith("data:video/") or bool(re.search(r"\.(?:mp4|mov|m4v|webm|avi|mkv)(?:[?#].*)?$", lower))
        is_image_url = lower.startswith("data:image/") or bool(re.search(r"\.(?:png|jpe?g|webp|gif|bmp|avif)(?:[?#].*)?$", lower))
        if wanted == "video" and (("image" in type_text and "video" not in type_text) or (is_image_url and not is_video_url)):
            continue
        if wanted == "image" and ("video" in type_text or (is_video_url and not is_image_url)):
            continue
        if raw.startswith(("data:", "http://", "https://")):
            found.append(raw)
        else:
            parsed_base = urlparse(base_url)
            origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.netloc else base_url.rstrip("/")
            found.append(f"{origin}/{raw.lstrip('/')}")
    return found


def _result_asset_urls(
    value: Any,
    base_url: str,
    media_type: str = "",
    excluded_asset_uuids: set[str] | None = None,
) -> list[str]:
    """只从任务输出资产节点提取地址，避免误拿输入参考素材。"""

    result_keys = {
        "resultassets",
        "resultassetlist",
        "outputassets",
        "outputassetlist",
        "generatedassets",
        "generatedassetlist",
    }
    containers: list[Any] = []

    def walk_results(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in result_keys:
                    containers.append(child)
                walk_results(child)
        elif isinstance(node, list):
            for child in node:
                walk_results(child)

    walk_results(value)
    if not containers:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                add(child)
            return
        for url in _asset_url_from_item(item, base_url, media_type, excluded_asset_uuids):
            if url not in seen:
                seen.add(url)
                found.append(url)

    for container in containers:
        add(container)
    return found


def _has_result_asset_container(value: Any) -> bool:
    """判断响应是否明确包含输出资产容器。"""

    result_keys = {
        "resultassets",
        "resultassetlist",
        "outputassets",
        "outputassetlist",
        "generatedassets",
        "generatedassetlist",
    }

    def walk_results(node: Any) -> bool:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in result_keys:
                    return True
                if walk_results(child):
                    return True
        elif isinstance(node, list):
            return any(walk_results(child) for child in node)
        return False

    return walk_results(value)


def find_asset_uuids(value: Any) -> list[str]:
    """只从任务结果的 resultAssets 提取资产 UUID，不误拿 task/project UUID。"""

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
    """只使用 API Key 的 Pixmax 客户端。"""

    def __init__(self, api_key: str, base_url: str = "", timeout: int = 90, opener: Callable[..., Any] | None = None):
        key = str(api_key or os.getenv("PIXMAX_OPENAPI_KEY") or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise PixmaxApiError("请先填写 Pixmax API Key。")
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
            raise PixmaxApiError(f"Pixmax API HTTP {exc.code}: {_redact(detail, self.api_key)[:400]}") from exc
        except urllib.error.URLError as exc:
            raise PixmaxApiError(f"Pixmax API 网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise PixmaxApiError("Pixmax API 请求超时。") from exc
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PixmaxApiError(f"Pixmax API 返回的不是 JSON（HTTP {status_code}）。") from exc
        if not isinstance(result, dict):
            raise PixmaxApiError("Pixmax API 返回了无法识别的数据。")
        if status_code >= 400 or result.get("success") is False:
            request_id = _first(result, ("requestId", "requestID", "traceId"))
            suffix = f"，请求 ID：{request_id}" if request_id else ""
            raise PixmaxApiError(f"Pixmax API 错误：{_redact(_message(result), self.api_key)}{suffix}")
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
        raise PixmaxApiError("API 无法自动创建项目，请检查账号权限。")

    def connect(self, node_type: str, project_uuid: str = "") -> tuple[str, list[dict[str, Any]]]:
        selected = self.ensure_project(project_uuid)
        models = self.available_models(node_type)
        if not models:
            kind = "图片" if node_type == "GENERATE_IMAGE" else "视频"
            raise PixmaxApiError(f"API 没有返回可用的{kind}模型。")
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
            raise PixmaxApiError("API 已接受任务，但没有返回 taskUuid。")
        return task_uuid

    def task_detail(self, task_uuid: str) -> dict[str, Any]:
        return self._request("POST", "/task/detail", {"taskUuid": task_uuid})

    def generate(self, project_uuid: str, node_type: str, model: str, prompt: str, params: dict[str, Any], input_asset_uuids: list[str] | None = None, timeout: int = 1800, interval: float = 5.0, progress_callback: Callable[..., Any] | None = None) -> tuple[dict[str, Any], list[str]]:
        task_uuid = self.submit_task(project_uuid, node_type, model, prompt, params, input_asset_uuids)
        _report(progress_callback, "提交任务", 5)
        deadline = time.monotonic() + max(1, int(timeout))
        poll_count = 0
        last_percent = 5
        while True:
            response = self.task_detail(task_uuid)
            status = _status(response) or "PENDING"
            if status in _SUCCESS:
                _report(progress_callback, "已完成", 100)
                # 任务详情可能同时带有 inputAssets 和 resultAssets；只读取输出资产。
                excluded = {str(value).strip() for value in (input_asset_uuids or []) if str(value or "").strip()}
                result_urls = _result_asset_urls(response, self.base_url, "video", excluded)
                if result_urls:
                    return response, result_urls
                # 有输出容器但没有可识别视频地址时不要退回扫描输入资产，避免把参考图当成结果。
                if _has_result_asset_container(response):
                    return response, []
                return response, _asset_urls(response, self.base_url, "video", excluded)
            if status in _FAILED:
                raise PixmaxApiError(f"生成失败：{_message(response, status)}")
            if status in _RUNNING - {"RUNNING", "PROCESSING"}:
                _report(progress_callback, "排队中", None)
            else:
                poll_count += 1
                reported = _progress_percent(response)
                if reported is None:
                    reported = min(95, 10 + poll_count * 5)
                last_percent = max(last_percent, min(95, reported))
                _report(progress_callback, "生成中", last_percent)
            if time.monotonic() >= deadline:
                raise PixmaxApiError(f"任务超时（{timeout} 秒）。")
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


def _progress_percent(value: Any) -> int | None:
    """从任务详情中读取服务端进度，缺失时由轮询进度兜底。"""

    for item in _walk(value):
        for key in ("progress", "progressPercent", "percentage", "percent", "completedPercent", "completePercent"):
            raw = item.get(key)
            if isinstance(raw, dict):
                raw = raw.get("value") or raw.get("percent") or raw.get("percentage")
            if isinstance(raw, str):
                raw = raw.strip().rstrip("%")
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 100:
                return int(round(number))
    return None


def find_asset_urls(
    value: Any,
    base_url: str = "",
    input_asset_uuids: list[str] | None = None,
) -> list[str]:
    normalized_base = normalize_base_url(base_url)
    excluded = {str(item).strip() for item in (input_asset_uuids or []) if str(item or "").strip()}
    result_urls = _result_asset_urls(value, normalized_base, "video", excluded)
    if result_urls:
        return result_urls
    if _has_result_asset_container(value):
        return []
    return _asset_urls(value, normalized_base, "video", excluded)


def _looks_like_image_bytes(data: bytes) -> bool:
    header = data[:16]
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or header.startswith(b"RIFF") and data[8:12] == b"WEBP"
        or header.startswith(b"BM")
    )


def save_asset(
    source: str,
    output_path: str,
    timeout: int,
    api_key: str = "",
    base_url: str = "",
    expected_media_type: str = "",
) -> None:
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
            raise PixmaxApiError("API 返回的素材地址无法识别。") from exc
    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise PixmaxApiError("API 返回了空素材。")
    if str(expected_media_type or "").strip().lower() == "video":
        try:
            data = Path(output_path).read_bytes()
        except OSError as exc:
            raise PixmaxApiError("结果视频文件无法读取。") from exc
        if _looks_like_image_bytes(data):
            try:
                Path(output_path).unlink()
            except OSError:
                pass
            raise PixmaxApiError("Pixmax 返回的是参考图片，不是生成的视频。")
