"""质量引擎共享注册点。

路由层与其它领域服务（如 validation_run）都从这里取同一个
``SamplingQualityService`` 实例，避免服务层反向 import 路由模块。
真实项目里这里会替换成数据库支持的实现并做依赖注入。
"""
from __future__ import annotations

from app.engine.partition_sampling import SamplingQualityService

sampling_quality_service = SamplingQualityService()
