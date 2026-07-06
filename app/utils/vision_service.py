"""视觉模型调用服务 — 阿里云百炼多模态 LLM 图片描述。"""
import asyncio
import base64
import os
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)

# 并发控制：chroma.yaml → vision_max_concurrent，环境变量 VISION_MAX_CONCURRENT 可覆盖
_MAX_CONCURRENT = int(os.getenv("VISION_MAX_CONCURRENT",
    str(get_config("vision_max_concurrent", 3))))
# 单张图片超时（秒），chroma.yaml → vision_image_timeout，环境变量 VISION_IMAGE_TIMEOUT 可覆盖
_IMAGE_TIMEOUT = int(os.getenv("VISION_IMAGE_TIMEOUT",
    str(get_config("vision_image_timeout", 30))))
# 大图压缩阈值（KB），超过此大小的图片（无论格式）自动缩放 + 转 JPEG 发送
_MAX_IMAGE_KB = int(os.getenv("VISION_MAX_IMAGE_KB",
    str(get_config("vision_max_image_kb", 250))))
# 压缩后最大尺寸（像素）
_MAX_IMAGE_DIM = int(os.getenv("VISION_MAX_IMAGE_DIM",
    str(get_config("vision_max_image_dim", 2048))))


def _get_vision_prompt() -> str:
    loader = PromptLoader()
    prompt = loader.load("vision")
    if prompt:
        return prompt
    return get_config(
        "vision_default_prompt",
        "请详细描述这张图片的内容。如果是图表，请提取其中的数据和趋势；"
        "如果是文档截图，请识别其中的文字；如果是图片，请描述其主要内容。",
    )


class VisionService:
    """多模态视觉模型服务 — 仅使用阿里云百炼。"""

    def __init__(self):
        self._model = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    def _get_model(self):
        if self._model is None:
            try:
                from app.utils.factory import create_vision_model
                self._model = create_vision_model()
                logger.info("【视觉服务】阿里云百炼多模态模型已就绪")
            except Exception as e:
                logger.error(f"【视觉服务】模型初始化失败: {e}")
                self._model = None
        return self._model

    def close(self):
        """释放 Vision 模型的 httpx 连接池。"""
        if self._model is not None:
            try:
                if hasattr(self._model, "root_client"):
                    self._model.root_client.close()
                if hasattr(self._model, "root_async_client"):
                    self._model.root_async_client.close()
                logger.info("【视觉服务】httpx 连接池已关闭")
            except Exception as e:
                logger.warning(f"【视觉服务】关闭连接池失败: {e}")
            self._model = None

    async def _describe_single(self, image_path: str, max_retries: int = 1) -> tuple[str, str]:
        """带超时和重试的单张图片描述。超时/网络错误重试一次，硬错误不重试。"""
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                desc = await asyncio.wait_for(
                    self.describe_image(image_path), timeout=_IMAGE_TIMEOUT
                )
                if attempt > 0:
                    logger.info(f"【视觉服务】重试成功 ({image_path})")
                return image_path, desc
            except asyncio.TimeoutError:
                last_error = f"超时 {_IMAGE_TIMEOUT}s"
                if attempt < max_retries:
                    logger.warning(f"【视觉服务】超时 ({image_path}): {last_error}, 第{attempt+1}次重试...")
                else:
                    logger.warning(f"【视觉服务】超时 ({image_path}): {last_error}, 已达最大重试次数")
            except (OSError, IOError) as e:
                last_error = str(e)
                if attempt < max_retries:
                    logger.warning(f"【视觉服务】网络错误 ({image_path}): {e}, 第{attempt+1}次重试...")
                else:
                    logger.warning(f"【视觉服务】网络错误 ({image_path}): {e}, 已达最大重试次数")
            except Exception as e:
                logger.warning(f"【视觉服务】描述失败 ({image_path}): {e}")
                raise RuntimeError(f"【视觉服务】图片描述失败 ({image_path}): {e}") from e
        raise RuntimeError(f"【视觉服务】图片描述失败 ({image_path}): {last_error}")

    async def describe_image(self, image_path: str) -> str:
        model = self._get_model()
        if model is None:
            raise RuntimeError("【视觉服务】模型未初始化，无法描述图片")

        from langchain_core.messages import HumanMessage

        # image_path 可能是相对于 data/ 的路径（来自 image_extractor），
        # 也可能是绝对路径。用 resolve_path 直接拼接，避免 get_data_path
        # 对文件路径调用 mkdir（Windows 下文件与目录同名会触发 WinError 183）。
        from app.utils.path_tool import resolve_path
        resolved_path = resolve_path(f"data/{image_path}")
        if not os.path.isfile(resolved_path):
            resolved_path = image_path  # 回退到原始路径

        with open(resolved_path, "rb") as f:
            raw = f.read()

        orig_size = len(raw)
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        compressed = False

        # 压缩判定：文件大小 或 像素维度 任一超阈值
        need_check = orig_size > _MAX_IMAGE_KB * 1024
        if not need_check:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                w, h = img.size
                if max(w, h) > _MAX_IMAGE_DIM:
                    need_check = True  # 像素超大但文件小（如高效压缩的 PNG）
            except Exception:
                pass

        if need_check:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                w, h = img.size
                if max(w, h) > _MAX_IMAGE_DIM:
                    ratio = _MAX_IMAGE_DIM / max(w, h)
                    img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=85)
                raw = buf.getvalue()
                compressed = True
                reduction = (1 - len(raw) / orig_size) * 100 if orig_size > 0 else 0
                logger.info(
                    f"【视觉服务】图片压缩: {orig_size/1024:.0f}KB({w}×{h}) → "
                    f"{len(raw)/1024:.0f}KB, base64后约{len(raw)*1.33/1024:.0f}KB "
                    f"({os.path.basename(image_path)})"
                )
            except Exception:
                pass

        image_data = base64.b64encode(raw).decode("utf-8")

        mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
        mime_type = "image/jpeg" if compressed else f"image/{mime_map.get(ext, 'png')}"

        message = HumanMessage(content=[
            {"type": "text", "text": _get_vision_prompt()},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
        ])

        response = await model.ainvoke([message])
        result = response.content if hasattr(response, "content") else str(response)
        return result.strip()

    async def _describe_with_semaphore(self, image_path: str) -> tuple[str, str]:
        """Semaphore 限流的单张图片描述。"""
        async with self._semaphore:
            return await self._describe_single(image_path)

    async def describe_image_batch(self, image_paths: list[str]) -> dict:
        """并发描述一批图片，通过 Semaphore 控制并发数。

        单张图片超时或失败不中断整批——降级跳过，返回空描述。
        返回 {"results": {path: desc}, "degraded": N, "total": N}
        """
        if not image_paths:
            return {"results": {}, "degraded": 0, "total": 0}

        logger.info(f"【视觉服务】批量开始: {len(image_paths)} 张图片, 并发上限 {_MAX_CONCURRENT}")

        tasks = [self._describe_with_semaphore(p) for p in image_paths]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: dict[str, str] = {}
        degraded = 0
        for path, raw in zip(image_paths, raw_results):
            if isinstance(raw, BaseException):
                results[path] = ""
                degraded += 1
                logger.warning(f"【视觉服务】降级 ({path}): {raw}")
            else:
                results[path] = raw[1] if isinstance(raw, tuple) else ""

        ok = len(results) - degraded
        logger.info(f"【视觉服务】批量完成: {len(image_paths)} 张, 成功 {ok}, 降级 {degraded}")
        return {"results": results, "degraded": degraded, "total": len(image_paths)}


_vision_service: VisionService | None = None


def get_vision_service() -> VisionService:
    global _vision_service
    if _vision_service is None:
        _vision_service = VisionService()
    return _vision_service


def close_vision_service():
    """关闭 VisionService，释放 httpx 连接池。"""
    global _vision_service
    if _vision_service is not None:
        _vision_service.close()
        _vision_service = None
