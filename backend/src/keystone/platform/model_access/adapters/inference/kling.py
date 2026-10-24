"""Verified OpenLux native image task protocol; no domain state or retries."""

from urllib.parse import urlsplit

from keystone.platform.model_access.adapters.inference.limits import retry_after
from keystone.platform.model_access.schemas.errors import AdapterError, QueryLater
from keystone.platform.model_access.schemas.images import ImageTaskReceipt

ORIGIN = "https://api.openlux.ai"
BASE = ORIGIN + "/v1"
SUBMIT = "/kling/v1/images/generations"
QUERY = SUBMIT + "/{id}"
MODELS = {"kling-v1", "kling-v1-5", "kling-v2", "kling-v2-1"}


def submit_body(selected, data):
    if (
        selected.provider != "openlux"
        or selected.api_base != BASE
        or selected.provider_model not in MODELS
    ):
        raise AdapterError(409, "model_provider_unavailable", "模型不支持原生异步图片协议")
    return {
        "model_name": selected.provider_model,
        "prompt": data.prompt,
        "n": data.n,
        "resolution": data.resolution.lower(),
    }


def task_id(payload):
    try:
        if type(payload["code"]) is not int or payload["code"] != 0:
            raise ValueError()
        value = payload["data"]["task_id"]
        # Use the same bounded path identity as persisted receipts.
        import re

        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError()
        return value
    except (AttributeError, KeyError, TypeError, ValueError):
        raise AdapterError(502, "model_invalid_output", "异步图片提交回执无效") from None


def status(payload, receipt: ImageTaskReceipt):
    try:
        if type(payload["code"]) is not int or payload["code"] != 0:
            raise ValueError()
        data = payload["data"]
        if data.get("task_id", receipt.task_id) != receipt.task_id:
            raise ValueError()
        state = data.get("task_status", data.get("status"))
        if "task_status" in data and "status" in data and data["status"] != state:
            raise ValueError()
        if state in {"submitted", "processing", "failed"}:
            return state, ()
        if state != "succeed":
            raise ValueError()
        items = data["task_result"]["images"]
        if not isinstance(items, list) or not 1 <= len(items) <= receipt.count:
            raise ValueError()
        urls = tuple(item["url"] for item in items)
        for url in urls:
            download_host(url)
        return state, urls
    except (AttributeError, KeyError, TypeError, ValueError):
        raise AdapterError(502, "model_invalid_output", "异步图片查询结果无效") from None


def download_host(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise AdapterError(502, "model_result_source_denied", "图片结果来源不受支持")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not (host.endswith(".klingai.com") or host == "h2.inkwai.com")
        ):
            raise ValueError()
        return host
    except ValueError:
        raise AdapterError(502, "model_result_source_denied", "图片结果来源不受支持") from None


def check_query(response):
    if response.status_code in {404, 429} or response.status_code >= 500:
        raise QueryLater(
            retry_after(response.headers.get("retry-after")),
            rate_limited=response.status_code == 429,
        )
    if response.status_code != 200:
        raise AdapterError(502, "model_task_query_rejected", "原图片任务查询被拒绝")


def check_download(response):
    if response.status_code in {404, 429} or response.status_code >= 500:
        raise QueryLater()
    if response.status_code != 200:
        raise AdapterError(502, "model_result_download_unavailable", "原图片结果无法取回")


async def query(http, receipt, secret):
    import httpx2

    try:
        payload = await http.get_json(
            "openlux",
            ORIGIN,
            SUBMIT + "/" + receipt.task_id,
            headers={"Authorization": "Bearer " + secret},
            check_status=check_query,
        )
        return status(payload, receipt)
    except (TimeoutError, OSError, httpx2.TimeoutException, httpx2.NetworkError):
        raise QueryLater() from None
    except (AttributeError, ValueError, TypeError, RecursionError):
        raise AdapterError(502, "model_invalid_output", "原图片任务返回了无效结果") from None


async def download(http, url):
    import httpx2

    from keystone.platform.model_access.schemas.errors import ResponseTooLarge
    from keystone.platform.model_access.schemas.images import MAX_OUTPUT_IMAGE_BYTES

    try:
        return await http.get_public_bytes(
            "openlux",
            url,
            hostname=download_host(url),
            limit=MAX_OUTPUT_IMAGE_BYTES,
            check_status=check_download,
        )
    except (TimeoutError, OSError, httpx2.TimeoutException, httpx2.NetworkError):
        raise QueryLater() from None
    except ResponseTooLarge:
        raise AdapterError(502, "model_response_too_large", "原图片结果超过上限") from None


class KlingImages:
    operations = frozenset({"image.generate"})
    delivery = "async"
    models = frozenset(MODELS)
    paths = {SUBMIT: "POST", QUERY: "GET"}
    query_path = QUERY
    require_parameters = False

    def request_target(self, selected):
        return ORIGIN, SUBMIT, "model_name"

    async def submit(self, sdk, selected, data):
        response = await sdk.post(SUBMIT, body=submit_body(selected, data), cast_to=dict)
        return task_id(response)

    query = staticmethod(query)
    download = staticmethod(download)
