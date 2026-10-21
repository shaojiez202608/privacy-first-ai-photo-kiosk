"""Builds a KioskService for the cloud (from Lambda environment variables) or for local mode."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .audit import AuditLogger, MemoryAuditSink
from .local import (
    InlineInvoker,
    LocalHmacSigner,
    LocalScheduler,
    MemoryObjectStore,
    MemorySessionStore,
    MockModeration,
    NoModeration,
    TransformModel,
)
from .service import KioskService
from .settings import Settings


def aws_service() -> KioskService:
    import logging

    import boto3
    from botocore.config import Config

    from . import aws

    # Only audit events belong in the audit log group; library chatter stays out.
    for name in ("botocore", "boto3", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = Settings.from_env()
    env = os.environ
    cfg = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=60,
                 region_name=settings.region)
    # Presigned URLs use the regional endpoint and SigV4 (see aws.regional_s3_client).
    s3 = aws.regional_s3_client(settings.region, cfg)
    rt = boto3.client("bedrock-runtime", config=cfg)
    if settings.moderation_provider == "guardrails":
        moderation: Any = aws.GuardrailModeration(rt, env["GUARDRAIL_ID"], env["GUARDRAIL_VERSION"])
    elif settings.moderation_provider == "mock":
        moderation = MockModeration()
    else:
        moderation = NoModeration()
    if settings.model_provider == "bedrock":
        model: Any = aws.BedrockImageModel(rt, env["MODEL_ID"], env["MODEL_FAMILY"], float(env["MODEL_STRENGTH"]))
    else:
        model = TransformModel()
    return KioskService(
        settings,
        sessions=aws.DynamoSessionStore(boto3.client("dynamodb", config=cfg), env["TABLE_NAME"]),
        objects=aws.S3ObjectStore(s3, env["BUCKET_NAME"], env.get("DATA_KEY_ARN")),
        scheduler=aws.StepFunctionsScheduler(boto3.client("stepfunctions", config=cfg), env["STATE_MACHINE_ARN"]),
        moderation=moderation,
        model=model,
        signer=aws.KmsMacSigner(boto3.client("kms", config=cfg), env["RECEIPT_KEY_ARN"]),
        invoker=aws.LambdaInvoker(boto3.client("lambda", config=cfg), env.get("PROCESSOR_FUNCTION_NAME", "")),
        audit=AuditLogger(region=settings.region, kiosk_id=settings.kiosk_id),
    )


class LocalStack:
    """All in-memory adapters plus the service, for local mode, the offline demo and tests."""

    def __init__(self, settings: Settings, *, clock: Callable[[], float] | None = None,
                 background: bool = False, share_base_url: str = "http://127.0.0.1:8080/share",
                 model: Any = None, moderation: Any = None, strict_audit: bool = True) -> None:
        import time

        clock = clock or time.time
        self.audit_sink = MemoryAuditSink()
        self.sessions = MemorySessionStore()
        self.objects = MemoryObjectStore(share_base_url, clock=clock)
        self.scheduler = LocalScheduler(clock=clock)
        self.invoker = InlineInvoker(background=background)
        self.signer = LocalHmacSigner()
        self.service = KioskService(
            settings, sessions=self.sessions, objects=self.objects, scheduler=self.scheduler,
            moderation=moderation or MockModeration(), model=model or TransformModel(), signer=self.signer,
            invoker=self.invoker,
            audit=AuditLogger(self.audit_sink, region=settings.region, kiosk_id=settings.kiosk_id,
                              strict=strict_audit, clock=clock),
            clock=clock)
        self.invoker.target = self.service.process

    def run_due_expiries(self) -> list[dict]:
        return [self.service.expire(ref) for ref in self.scheduler.due()]
