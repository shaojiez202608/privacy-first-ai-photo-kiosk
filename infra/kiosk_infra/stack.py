"""One regional stack. Every regional resource is created in ``deploy.region``; the only global
service is CloudFront, which serves the static kiosk page (no images, no session data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aws_cdk import Acknowledgment, Aws, CfnOutput, Duration, RemovalPolicy, Stack, Token, Validations
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authz
from aws_cdk import aws_apigatewayv2_integrations as integ
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3d
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from .params import foundation_model_id, is_cross_region_profile

ROOT = Path(__file__).resolve().parents[2]
CONTENT_FILTERS = ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT", "PROMPT_ATTACK")


def data_protection_policy(name: str, identifiers: list[str]) -> dict[str, Any]:
    arns = [f"arn:aws:dataprotection::aws:data-identifier/{i}" for i in identifiers]
    return {
        "Name": name, "Version": "2021-06-01",
        "Statement": [
            {"Sid": "audit", "DataIdentifier": arns, "Operation": {"Audit": {"FindingsDestination": {}}}},
            {"Sid": "mask", "DataIdentifier": arns, "Operation": {"Deidentify": {"MaskConfig": {}}}},
        ],
    }


def kiosk_settings(params: dict[str, Any]) -> dict[str, Any]:
    s, d = params["session"], params["deploy"]
    return {"region": d["region"], "kiosk_id": s["kiosk_id"], "notice_version": s["notice_version"],
            "retention_seconds": s["retention_seconds"], "share_link_ttl_seconds": s["share_link_ttl_seconds"],
            "ttl_grace_seconds": s["ttl_grace_seconds"], "max_upload_bytes": s["max_upload_bytes"],
            "max_side": s["max_side"], "min_side": s["min_side"], "max_aspect": s["max_aspect"],
            "max_pixels": s["max_pixels"], "model_provider": params["image_model"]["provider"],
            "moderation_provider": params["moderation"]["provider"], "operator_contact": s["operator_contact"]}


class KioskStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, params: dict[str, Any], bundle_dir: str | None = None,
                 web_dir: str | None = None, **kw: Any) -> None:
        super().__init__(scope, cid, **kw)
        d, sess, model, mod = params["deploy"], params["session"], params["image_model"], params["moderation"]
        prefix = d["resource_prefix"]
        destroy = d["removal_policy"] == "destroy"
        removal = RemovalPolicy.DESTROY if destroy else RemovalPolicy.RETAIN
        log_days = _retention(d["log_retention_days"])
        identifiers = params["logging"]["data_identifiers"]
        bundle = bundle_dir or current_bundle()
        self.acks: list[tuple[Construct, str, str]] = []

        # ---------------------------------------------------------------- keys (this region only)
        data_key = kms.Key(self, "DataKey", alias=f"alias/{prefix}-data", enable_key_rotation=True,
                           description="Prototype photo kiosk: images, session records, logs, kiosk token",
                           removal_policy=removal, pending_window=Duration.days(7))
        data_key.add_to_resource_policy(iam.PolicyStatement(
            sid="CloudWatchLogsForThisProjectsLogGroups",
            principals=[iam.ServicePrincipal(f"logs.{Aws.REGION}.amazonaws.com")],
            actions=["kms:Encrypt", "kms:Decrypt", "kms:ReEncryptFrom", "kms:ReEncryptTo",
                     "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext", "kms:DescribeKey"],
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn":
                                    f"arn:aws:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/{prefix}/*"}}))
        receipt_key = kms.Key(self, "ReceiptKey", alias=f"alias/{prefix}-receipts",
                              key_spec=kms.KeySpec.HMAC_256, key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
                              description="Prototype photo kiosk: HMAC for consent and deletion receipts",
                              removal_policy=removal, pending_window=Duration.days(7))

        # ---------------------------------------------------------------- log groups
        def log_group(lid: str, name: str) -> logs.LogGroup:
            lg = logs.LogGroup(self, lid, log_group_name=f"/{prefix}/{name}", retention=log_days,
                               encryption_key=data_key, removal_policy=removal)
            lg.node.default_child.data_protection_policy = data_protection_policy(f"{prefix}-{name}", identifiers)
            return lg

        audit_logs = log_group("AuditLogs", "audit")
        access_logs = log_group("ApiAccessLogs", "api-access")
        workflow_logs = log_group("WorkflowLogs", "workflow")
        handler_logs = log_group("CdkHandlerLogs", "cdk-handlers")

        # ---------------------------------------------------------------- storage
        images = s3.Bucket(
            self, "ImageBucket", encryption=s3.BucketEncryption.KMS, encryption_key=data_key, bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, minimum_tls_version=1.2,
            versioned=d["image_bucket_versioning"], object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal, auto_delete_objects=destroy,
            lifecycle_rules=[
                # Backstop only. Explicit deletion removes everything long before this applies.
                s3.LifecycleRule(id="backstop-expire-session-objects", prefix="s/", expiration=Duration.days(1),
                                 noncurrent_version_expiration=Duration.days(1),
                                 abort_incomplete_multipart_upload_after=Duration.days(1)),
                s3.LifecycleRule(id="backstop-expired-delete-markers", expired_object_delete_marker=True),
            ])
        table = ddb.Table(
            self, "SessionTable", partition_key=ddb.Attribute(name="session_ref", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST, encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key, time_to_live_attribute="ttl", removal_policy=removal,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False))
        token = sm.Secret(self, "KioskToken", secret_name=f"{prefix}/kiosk-token", encryption_key=data_key,
                          description="Kiosk device token checked by the API authorizer (not a user credential)",
                          generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True,
                                                                          password_length=48),
                          removal_policy=removal)

        # ---------------------------------------------------------------- moderation
        guardrail = None
        if mod["provider"] == "guardrails":
            strength = mod["filter_strength"]
            guardrail = bedrock.CfnGuardrail(
                self, "ImageGuardrail", name=f"{prefix}-image-guardrail",
                description="Image content filters for kiosk input and output images",
                blocked_input_messaging="Blocked by the content filter.",
                blocked_outputs_messaging="Blocked by the content filter.",
                content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength=strength,
                        output_strength="NONE" if t == "PROMPT_ATTACK" else strength,
                        input_modalities=["TEXT", "IMAGE"],
                        output_modalities=["TEXT"] if t == "PROMPT_ATTACK" else ["TEXT", "IMAGE"])
                    for t in CONTENT_FILTERS]))
            guardrail_version = bedrock.CfnGuardrailVersion(self, "ImageGuardrailVersion",
                                                            guardrail_identifier=guardrail.attr_guardrail_id,
                                                            description="Pinned version used by the processor")

        # ---------------------------------------------------------------- functions
        sm_name = f"{prefix}-session-expiry"
        sm_arn = f"arn:{Aws.PARTITION}:states:{Aws.REGION}:{Aws.ACCOUNT_ID}:stateMachine:{sm_name}"
        env = {
            "KIOSK_SETTINGS": json.dumps(kiosk_settings(params), sort_keys=True),
            "TABLE_NAME": table.table_name, "BUCKET_NAME": images.bucket_name, "DATA_KEY_ARN": data_key.key_arn,
            "RECEIPT_KEY_ARN": receipt_key.key_arn, "STATE_MACHINE_ARN": sm_arn,
            "PROCESSOR_FUNCTION_NAME": f"{prefix}-processor",
        }
        if guardrail is not None:
            env.update(GUARDRAIL_ID=guardrail.attr_guardrail_id, GUARDRAIL_VERSION=guardrail_version.attr_version)
        if model["provider"] == "bedrock":
            env.update(MODEL_ID=model["model_id"], MODEL_FAMILY=model["family"], MODEL_STRENGTH=str(model["strength"]))

        code = lambda_.Code.from_asset(bundle)

        def function(lid: str, name: str, handler: str, timeout: int, env_vars: dict[str, str]) -> lambda_.Function:
            role = iam.Role(self, f"{lid}Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                            description=f"Execution role of {prefix}-{name}")
            fn = lambda_.Function(
                self, lid, function_name=f"{prefix}-{name}", runtime=lambda_.Runtime.PYTHON_3_14,
                architecture=lambda_.Architecture.ARM_64, code=code, handler=handler,
                memory_size=d["lambda_memory_mb"], timeout=Duration.seconds(timeout), role=role,
                environment=env_vars, log_group=audit_logs, logging_format=lambda_.LoggingFormat.JSON,
                application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
                system_log_level_v2=lambda_.SystemLogLevel.WARN)
            role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                                   resources=[audit_logs.log_group_arn]))
            return fn

        api_fn = function("ApiFunction", "api", "handlers.api.handler", 29, env)
        processor = function("ProcessorFunction", "processor", "handlers.workers.process",
                             d["processor_timeout_s"], env)
        processor.configure_async_invoke(retry_attempts=0, max_event_age=Duration.minutes(15))
        expiry_fn = function("ExpiryFunction", "expiry", "handlers.workers.expire", 120, env)
        auth_fn = function("AuthorizerFunction", "authorizer", "handlers.authorizer.handler", 10,
                           {"KIOSK_TOKEN_SECRET_ARN": token.secret_arn})

        objects = images.arn_for_objects("s/*")
        data_key_use = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]

        def s3_purge() -> list[iam.PolicyStatement]:
            return [
                iam.PolicyStatement(actions=["s3:ListBucketVersions"], resources=[images.bucket_arn],
                                    conditions={"StringLike": {"s3:prefix": ["s/*"]}}),
                iam.PolicyStatement(actions=["s3:ListBucketMultipartUploads"], resources=[images.bucket_arn]),
                iam.PolicyStatement(actions=["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:AbortMultipartUpload"],
                                    resources=[objects]),
            ]

        def table_access(*actions: str) -> iam.PolicyStatement:
            return iam.PolicyStatement(actions=[f"dynamodb:{a}" for a in actions], resources=[table.table_arn])

        exec_arn = f"arn:{Aws.PARTITION}:states:{Aws.REGION}:{Aws.ACCOUNT_ID}:execution:{sm_name}:*"
        for stmt in [table_access("GetItem", "PutItem", "UpdateItem", "DeleteItem"),
                     iam.PolicyStatement(actions=["s3:PutObject", "s3:GetObject"], resources=[objects]),
                     *s3_purge(),
                     iam.PolicyStatement(actions=data_key_use, resources=[data_key.key_arn]),
                     iam.PolicyStatement(actions=["kms:GenerateMac"], resources=[receipt_key.key_arn]),
                     iam.PolicyStatement(actions=["states:StartExecution"], resources=[sm_arn]),
                     iam.PolicyStatement(actions=["states:StopExecution"], resources=[exec_arn]),
                     iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[processor.function_arn])]:
            api_fn.add_to_role_policy(stmt)
        for stmt in [table_access("GetItem", "UpdateItem"),
                     iam.PolicyStatement(actions=["s3:PutObject", "s3:GetObject"], resources=[objects]),
                     *s3_purge(),
                     iam.PolicyStatement(actions=data_key_use, resources=[data_key.key_arn])]:
            processor.add_to_role_policy(stmt)
        if guardrail is not None:
            processor.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"],
                                                             resources=[guardrail.attr_guardrail_arn]))
        self.model_finding: str | None = None
        if model["provider"] == "bedrock":
            mid = model["model_id"]
            base = foundation_model_id(mid)
            if is_cross_region_profile(mid):
                profile_arn = f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:inference-profile/{mid}"
                processor.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"],
                                                                 resources=[profile_arn]))
                # The profile routes to the foundation model in the regions of its geography; the
                # foundation model may only be invoked through this profile.
                processor.add_to_role_policy(iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[f"arn:{Aws.PARTITION}:bedrock:*::foundation-model/{base}"],
                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": profile_arn}}))
                self.model_finding = f"Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/{base}"
            else:
                processor.add_to_role_policy(iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{base}"]))
        for stmt in [table_access("GetItem", "DeleteItem"), *s3_purge(),
                     iam.PolicyStatement(actions=data_key_use, resources=[data_key.key_arn]),
                     iam.PolicyStatement(actions=["kms:GenerateMac"], resources=[receipt_key.key_arn])]:
            expiry_fn.add_to_role_policy(stmt)
        token.grant_read(auth_fn)

        # Only the API and the processor may read or write image objects; presigned links are signed
        # by the API role. Links older than the configured link lifetime are refused by the bucket.
        writers = [api_fn.role.role_arn, processor.role.role_arn]
        images.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyImageAccessExceptKioskFunctions", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], resources=[images.arn_for_objects("*")],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": writers}}))
        images.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyStaleSignatures", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["s3:GetObject"], resources=[images.arn_for_objects("*")],
            conditions={"NumericGreaterThan": {"s3:signatureAge": str(sess["share_link_ttl_seconds"] * 1000)}}))
        images.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyNonSigV4", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["s3:*"], resources=[images.bucket_arn, images.arn_for_objects("*")],
            conditions={"StringNotEquals": {"s3:signatureversion": "AWS4-HMAC-SHA256"}}))

        # ---------------------------------------------------------------- expiry workflow
        sfn_role = iam.Role(self, "WorkflowRole", assumed_by=iam.ServicePrincipal(
            "states.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                                                "ArnLike": {"aws:SourceArn": sm_arn}}),
            description="Session expiry workflow role")
        purge_task = tasks.LambdaInvoke(self, "PurgeSession", lambda_function=expiry_fn,
                                        payload=sfn.TaskInput.from_object({"session_ref.$": "$.session_ref"}),
                                        payload_response_only=True)
        purge_task.add_retry(errors=["States.ALL"], interval=Duration.seconds(30), max_attempts=5, backoff_rate=2)
        wait = sfn.Wait(self, "WaitRetention", time=sfn.WaitTime.seconds_path("$.wait_seconds"))
        machine = sfn.StateMachine(
            self, "ExpiryWorkflow", state_machine_name=sm_name, state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(wait.next(purge_task)), role=sfn_role,
            tracing_enabled=True, timeout=Duration.seconds(sess["retention_seconds"] + 3600),
            logs=sfn.LogOptions(destination=workflow_logs, level=sfn.LogLevel.ALL, include_execution_data=False),
            removal_policy=RemovalPolicy.DESTROY)

        # ---------------------------------------------------------------- frontend (optional)
        site_url = None
        dist = None
        site = None
        if params["frontend"]["enabled"]:
            site = s3.Bucket(self, "SiteBucket", encryption=s3.BucketEncryption.S3_MANAGED,
                             block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
                             minimum_tls_version=1.2, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                             removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)
            csp = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                   f"connect-src 'self' https://*.execute-api.{Aws.REGION}.amazonaws.com; "
                   f"img-src 'self' data: https://{images.bucket_name}.s3.{Aws.REGION}.amazonaws.com; "
                   "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            headers = cf.ResponseHeadersPolicy(
                self, "SiteHeaders", response_headers_policy_name=f"{prefix}-site-headers",
                security_headers_behavior=cf.ResponseSecurityHeadersBehavior(
                    content_security_policy=cf.ResponseHeadersContentSecurityPolicy(content_security_policy=csp,
                                                                                    override=True),
                    content_type_options=cf.ResponseHeadersContentTypeOptions(override=True),
                    frame_options=cf.ResponseHeadersFrameOptions(frame_option=cf.HeadersFrameOption.DENY,
                                                                 override=True),
                    referrer_policy=cf.ResponseHeadersReferrerPolicy(
                        referrer_policy=cf.HeadersReferrerPolicy.NO_REFERRER, override=True),
                    strict_transport_security=cf.ResponseHeadersStrictTransportSecurity(
                        access_control_max_age=Duration.days(365), include_subdomains=True, override=True)))
            geo = params["frontend"].get("geo_allowlist") or []
            dist = cf.Distribution(
                self, "SiteDistribution", default_root_object="index.html", comment=f"{prefix} kiosk page",
                geo_restriction=cf.GeoRestriction.allowlist(*geo) if geo else None,
                default_behavior=cf.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(site),
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cf.CachePolicy.CACHING_DISABLED, response_headers_policy=headers))
            site_url = f"https://{dist.distribution_domain_name}"

        cors = list(d.get("cors_extra_origins", [])) + ([site_url] if site_url else [])
        authorizer = authz.HttpLambdaAuthorizer("KioskTokenAuthorizer", auth_fn,
                                                authorizer_name=f"{prefix}-kiosk-token",
                                                response_types=[authz.HttpLambdaResponseType.SIMPLE],
                                                identity_source=["$request.header.X-Kiosk-Token"],
                                                results_cache_ttl=Duration.seconds(0))
        api = apigw.HttpApi(
            self, "Api", api_name=f"{prefix}-api", default_authorizer=authorizer,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=cors, allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST,
                                                   apigw.CorsHttpMethod.DELETE],
                allow_headers=["content-type", "x-kiosk-token", "x-session-secret"], max_age=Duration.minutes(10)))
        integration = integ.HttpLambdaIntegration("ApiIntegration", api_fn)
        for path, methods in (("/notice", [apigw.HttpMethod.GET]), ("/sessions", [apigw.HttpMethod.POST]),
                              ("/sessions/{id}/photo", [apigw.HttpMethod.POST]),
                              ("/sessions/{id}", [apigw.HttpMethod.GET, apigw.HttpMethod.DELETE])):
            api.add_routes(path=path, methods=methods, integration=integration)
        stage = api.default_stage.node.default_child
        # No source IP, user agent or request path in the access log: the route key is the template.
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps({"requestId": "$context.requestId", "time": "$context.requestTime",
                               "routeKey": "$context.routeKey", "status": "$context.status",
                               "latencyMs": "$context.responseLatency",
                               "authorizerError": "$context.authorizer.error",
                               "integrationStatus": "$context.integrationStatus"}))
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=d["api_throttle_rate"], throttling_burst_limit=d["api_throttle_burst"])

        if site is not None and dist is not None:
            s3d.BucketDeployment(
                self, "SiteDeployment", destination_bucket=site, log_group=handler_logs, memory_limit=256,
                sources=[s3d.Source.asset(web_dir or str(ROOT / "web")),
                         s3d.Source.json_data("config.json", {"apiUrl": api.api_endpoint, "region": Aws.REGION,
                                                              "kioskId": sess["kiosk_id"], "localMode": False})])

        for k, v in {"ApiUrl": api.api_endpoint, "KioskUrl": site_url or "frontend disabled",
                     "ImageBucketName": images.bucket_name, "SessionTableName": table.table_name,
                     "StateMachineArn": machine.state_machine_arn, "AuditLogGroup": audit_logs.log_group_name,
                     "AccessLogGroup": access_logs.log_group_name, "KioskTokenSecretArn": token.secret_arn,
                     "ReceiptKeyArn": receipt_key.key_arn, "DataKeyArn": data_key.key_arn,
                     "ApiRoleArn": api_fn.role.role_arn,
                     "GuardrailId": guardrail.attr_guardrail_id if guardrail is not None else "none"}.items():
            CfnOutput(self, k, value=v)

        self._acknowledge(prefix=prefix, images=images, site=site, dist=dist, table=table, token=token,
                          functions=[api_fn, processor, expiry_fn, auth_fn], sfn_role=sfn_role, api=api,
                          data_key=data_key, sm_name=sm_name)

    # ------------------------------------------------------------------ cdk-nag acknowledgements
    def _acknowledge(self, *, prefix: str, images: s3.Bucket, site: s3.Bucket | None, dist: Any, table: ddb.Table,
                     token: sm.Secret, functions: list[lambda_.Function], sfn_role: iam.Role, api: apigw.HttpApi,
                     data_key: kms.Key, sm_name: str) -> None:
        """Each acknowledgement is attached to the construct that triggers it. Finding ids are built
        from the same values as the policies, so other prefixes/models still synthesise cleanly.
        The list with reasons is in SECURITY-NOTES.md."""

        def ack(construct: Construct, rule: str, reason: str, finding: str | None = None) -> None:
            rid = f"AwsSolutions::AwsSolutions-{rule}" + (f"[{finding}]" if finding else "")
            Validations.of(construct).acknowledge(Acknowledgment(id=rid, reason=reason))

        ack(images, "S1", "Server access logs would record the IP address of every phone that downloads a "
                          "result; the prototype minimises personal data instead. Object-level audit is the "
                          "application audit log (and optional CloudTrail data events, see handover).")
        ack(table, "DDB3", "Point-in-time recovery would keep deleted session records restorable for the "
                           "recovery window, which contradicts deletion on request. The table holds no images.")
        ack(token, "SMG4", "Prototype kiosk device token; rotation is in the production hardening list "
                           "(handover.md). It is not a user credential.")
        if site is not None:
            ack(site, "S1", "Static, public kiosk page assets behind CloudFront; no personal data. Access "
                            "logs would only add viewer IP addresses.")
            for rule, reason in (
                ("CFR1", "Geo restriction is a parameter (frontend.geo_allowlist); empty in the sandbox."),
                ("CFR2", "The distribution only serves static page assets; WAF for CloudFront is a production "
                         "hardening item (handover.md)."),
                ("CFR3", "Viewer access logs contain IP addresses; disabled to minimise personal data."),
                ("CFR4", "Uses the default CloudFront certificate (no custom domain in the prototype); a "
                         "custom domain with a TLSv1.2_2021 policy is a production hardening item."),
            ):
                ack(dist, rule, reason)

        obj = f"Resource::<{self.get_logical_id(images.node.default_child)}.Arn>/s/*"
        execs = (f"Resource::arn:<AWS::Partition>:states:<AWS::Region>:<AWS::AccountId>:execution:{sm_name}:*")
        for fn in functions:
            policy = fn.role.node.find_child("DefaultPolicy")
            ack(policy, "IAM5", "Objects under the session prefix s/ of this stack's image bucket only; each "
                                "session's keys are created at run time.", obj)
            ack(policy, "IAM5", "Executions of this stack's expiry state machine; execution names are created "
                                "per session at run time.", execs)
            if self.model_finding:
                ack(policy, "IAM5", "A geographic cross-Region inference profile routes to the foundation model "
                                    "in several regions; the statement is limited by bedrock:InferenceProfileArn "
                                    "to this one profile, and is only present with allow_cross_region_inference.",
                    self.model_finding)
        sfn_policy = sfn_role.node.find_child("DefaultPolicy")
        for f in functions:
            ack(sfn_policy, "IAM5", "Versions/aliases of the expiry function (added by the LambdaInvoke task).",
                f"Resource::<{self.get_logical_id(f.node.default_child)}.Arn>:*")
        ack(sfn_policy, "IAM5", "CloudWatch Logs delivery and X-Ray actions used by Step Functions logging and "
                                "tracing do not support resource-level permissions.", "Resource::*")
        for child in self.node.children:
            if not child.node.id.startswith("Custom::CDKBucketDeployment") or site is None:
                continue
            ack(child, "L1", "CDK-managed BucketDeployment handler: its runtime is chosen by CDK, not by this "
                             "project; it runs only during deployment.")
            role = child.node.find_child("ServiceRole")
            ack(role, "IAM4", "CDK-managed BucketDeployment handler: CDK attaches AWSLambdaBasicExecutionRole; "
                              "not configurable.", "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/"
                                                   "AWSLambdaBasicExecutionRole")
            policy = role.node.find_child("DefaultPolicy")
            for action in ("s3:Abort*", "s3:DeleteObject*", "s3:GetBucket*", "s3:GetObject*", "s3:List*"):
                ack(policy, "IAM5", "CDK-managed BucketDeployment handler: CDK grant on the site bucket and the "
                                    "CDK asset objects (generated by CDK).", f"Action::{action}")
            ack(policy, "IAM5", "CDK-managed BucketDeployment handler writes the page assets to the site bucket.",
                f"Resource::<{self.get_logical_id(site.node.default_child)}.Arn>/*")
            def part(value: str, token: str) -> str:
                return token if Token.is_unresolved(value) else value

            assets = (f"Resource::arn:{part(self.partition, '<AWS::Partition>')}:s3:::cdk-hnb659fds-assets-"
                      f"{part(self.account, '<AWS::AccountId>')}-{part(self.region, '<AWS::Region>')}/*")
            ack(policy, "IAM5", "CDK-managed BucketDeployment handler reads the uploaded page asset from the CDK "
                                "bootstrap assets bucket.", assets)


def current_bundle() -> str:
    """The directory written by scripts/build_bundle.sh (named in build/bundle.current)."""
    marker = ROOT / "build" / "bundle.current"
    if not marker.is_file():
        raise SystemExit("Lambda bundle missing: run scripts/build_bundle.sh (or `make bundle`) first.")
    path = ROOT / marker.read_text().strip()
    if not (path / "kiosk" / "service.py").is_file():
        raise SystemExit(f"{path} is not a complete bundle: run scripts/build_bundle.sh again.")
    return str(path)


def _retention(days: int) -> logs.RetentionDays:
    return {1: logs.RetentionDays.ONE_DAY, 3: logs.RetentionDays.THREE_DAYS, 5: logs.RetentionDays.FIVE_DAYS,
            7: logs.RetentionDays.ONE_WEEK, 14: logs.RetentionDays.TWO_WEEKS, 30: logs.RetentionDays.ONE_MONTH,
            60: logs.RetentionDays.TWO_MONTHS, 90: logs.RetentionDays.THREE_MONTHS,
            120: logs.RetentionDays.FOUR_MONTHS, 150: logs.RetentionDays.FIVE_MONTHS,
            180: logs.RetentionDays.SIX_MONTHS, 365: logs.RetentionDays.ONE_YEAR}[days]
