from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as cloudfront_origins,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct


class MedstackHybridStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = str(self.node.try_get_context("prefix") or "medstackocr")
        line_webhook_path = str(self.node.try_get_context("line_webhook_path") or "/webhook/line")
        app_secrets_name = str(self.node.try_get_context("app_secrets_name") or "")
        docai_project_id = str(self.node.try_get_context("docai_project_id") or "")
        docai_location = str(self.node.try_get_context("docai_location") or "us")
        docai_processor_id = str(self.node.try_get_context("docai_processor_id") or "")
        receipt_prefix = str(self.node.try_get_context("receipt_prefix") or "raw")
        config_path = str(self.node.try_get_context("config_path") or "config.yaml")
        # LIFF の ID トークン検証に使う LINE Login チャネル ID（Messaging API チャネルと同じプロバイダーに作る）
        line_login_channel_id = str(self.node.try_get_context("line_login_channel_id") or "")
        # LIFF アプリの ID。LIFF アプリ作成時にエンドポイント URL（LiffEndpointUrl 出力）が必要なため、初回デプロイ後に設定する
        liff_id = str(self.node.try_get_context("liff_id") or "")
        app_secret = (
            secretsmanager.Secret.from_secret_name_v2(
                self,
                "AppSecrets",
                app_secrets_name,
            )
            if app_secrets_name
            else None
        )

        dlq = sqs.Queue(
            self,
            "LineEventsDlq",
            queue_name=f"{prefix}-line-events-dlq.fifo",
            fifo=True,
            retention_period=Duration.days(14),
        )
        line_events_queue = sqs.Queue(
            self,
            "LineEventsQueue",
            queue_name=f"{prefix}-line-events.fifo",
            fifo=True,
            content_based_deduplication=False,
            visibility_timeout=Duration.seconds(180),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=dlq,
            ),
        )
        dlq.apply_removal_policy(RemovalPolicy.DESTROY)
        line_events_queue.apply_removal_policy(RemovalPolicy.DESTROY)

        receipt_bucket = s3.Bucket(
            self,
            "ReceiptBucket",
            bucket_name=self.node.try_get_context("receipt_bucket_name"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireRawImages",
                    prefix=f"{receipt_prefix}/",
                    # 医療費控除の領収書は申告期限から5年間の保存が必要なため、余裕を見て6年保存する
                    expiration=Duration.days(365 * 6 + 2),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        event_table = dynamodb.Table(
            self,
            "LineEventDedupeTable",
            table_name=f"{prefix}-line-event-dedupe",
            partition_key=dynamodb.Attribute(name="event_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="expires_at_epoch",
        )

        receipts_table = dynamodb.Table(
            self,
            "ReceiptsTable",
            table_name=f"{prefix}-receipts",
            partition_key=dynamodb.Attribute(name="receipt_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        receipts_table.add_global_secondary_index(
            index_name="gsi_user_created",
            partition_key=dynamodb.Attribute(name="line_user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        receipts_table.add_global_secondary_index(
            index_name="gsi_message",
            partition_key=dynamodb.Attribute(name="line_message_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        receipt_fields_table = dynamodb.Table(
            self,
            "ReceiptFieldsTable",
            table_name=f"{prefix}-receipt-fields",
            partition_key=dynamodb.Attribute(name="receipt_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="field_name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        sessions_table = dynamodb.Table(
            self,
            "SessionsTable",
            table_name=f"{prefix}-sessions",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="expires_at_epoch",
        )
        sessions_table.add_global_secondary_index(
            index_name="line_user_id_updated_at_index",
            partition_key=dynamodb.Attribute(name="line_user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updated_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        aggregate_entries_table = dynamodb.Table(
            self,
            "AggregateEntriesTable",
            table_name=f"{prefix}-aggregate-entries",
            partition_key=dynamodb.Attribute(name="line_user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="service_date_receipt", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        aggregate_entries_table.add_global_secondary_index(
            index_name="receipt_id_index",
            partition_key=dynamodb.Attribute(name="receipt_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        family_registry_key = kms.Key(
            self,
            "FamilyRegistryKey",
            alias=f"alias/{prefix}-family-registry",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        family_registry_table = dynamodb.Table(
            self,
            "FamilyRegistryTable",
            table_name=f"{prefix}-family-registry",
            partition_key=dynamodb.Attribute(name="line_user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="record_type", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=family_registry_key,
        )
        learning_rules_table = dynamodb.Table(
            self,
            "LearningRulesTable",
            table_name=f"{prefix}-learning-rules",
            partition_key=dynamodb.Attribute(name="line_user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="rule_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=family_registry_key,
        )
        ocr_usage_guard_table = dynamodb.Table(
            self,
            "OcrUsageGuardTable",
            table_name=f"{prefix}-ocr-usage-guard",
            partition_key=dynamodb.Attribute(name="scope_key", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="window_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="expires_at_epoch",
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=family_registry_key,
        )

        docai_layer_asset_path = Path(__file__).resolve().parent / "layers" / "docai"
        if not (docai_layer_asset_path / "python").exists():
            raise FileNotFoundError(
                "DocAI layer asset is missing. "
                "Run `powershell -File infra/cdk/scripts/build_docai_layer.ps1` before `cdk deploy`."
            )
        docai_dependencies_layer = lambda_.LayerVersion(
            self,
            "DocAiDependenciesLayer",
            layer_version_name=f"{prefix}-docai-deps",
            code=lambda_.Code.from_asset(str(docai_layer_asset_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Dependencies for Google Document AI client",
        )

        lambda_asset_path = str(Path(__file__).resolve().parents[2])
        lambda_asset_excludes = [
            ".git/**",
            ".venv/**",
            ".venv*/**",
            "venv/**",
            "env/**",
            "__pycache__/**",
            "**/__pycache__/**",
            "*.pyc",
            "data/**",
            "external/**",
            "tests/**",
            "infra/**",
            "web/**",
            "cdk.out/**",
            "*.md",
            "PLAN*.md",
        ]
        ingress_fn = lambda_.Function(
            self,
            "LineIngressFunction",
            function_name=f"{prefix}-line-ingress",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="app.lambda_handlers.ingress_handler.lambda_handler",
            code=lambda_.Code.from_asset(lambda_asset_path, exclude=lambda_asset_excludes),
            timeout=Duration.seconds(5),
            memory_size=256,
            environment={
                "SQS_QUEUE_URL": line_events_queue.queue_url,
                "EVENT_DEDUPE_TABLE": event_table.table_name,
                "EVENT_DEDUPE_TTL_DAYS": "7",
                "LINE_WEBHOOK_PATH": line_webhook_path,
                "APP_SECRETS_ARN": app_secret.secret_arn if app_secret else "",
                "APP_SECRETS_NAME": app_secrets_name,
            },
        )

        worker_fn = lambda_.Function(
            self,
            "LineWorkerFunction",
            function_name=f"{prefix}-line-worker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="app.lambda_handlers.worker_handler.lambda_handler",
            code=lambda_.Code.from_asset(lambda_asset_path, exclude=lambda_asset_excludes),
            timeout=Duration.seconds(90),
            memory_size=1536,
            layers=[docai_dependencies_layer],
            environment={
                "CONFIG_PATH": config_path,
                "INBOX_BACKEND": "dynamodb",
                "DDB_TABLE_PREFIX": prefix,
                "DDB_EVENT_TABLE": event_table.table_name,
                "DDB_RECEIPTS_TABLE": receipts_table.table_name,
                "DDB_FIELDS_TABLE": receipt_fields_table.table_name,
                "DDB_SESSIONS_TABLE": sessions_table.table_name,
                "DDB_AGGREGATE_TABLE": aggregate_entries_table.table_name,
                "DDB_FAMILY_TABLE": family_registry_table.table_name,
                "DDB_LEARNING_TABLE": learning_rules_table.table_name,
                "DDB_OCR_GUARD_TABLE": ocr_usage_guard_table.table_name,
                "RECEIPT_BUCKET": receipt_bucket.bucket_name,
                "RECEIPT_PREFIX": receipt_prefix,
                "APP_SECRETS_ARN": app_secret.secret_arn if app_secret else "",
                "APP_SECRETS_NAME": app_secrets_name,
                "DOC_AI_PROJECT_ID": docai_project_id,
                "DOC_AI_LOCATION": docai_location,
                "DOC_AI_PROCESSOR_ID": docai_processor_id,
                "TEMPLATE_STORE_PATH": "/tmp/data/templates",
                "OCR_GUARD_ENABLED": "true",
                "OCR_GUARD_USER_PER_MINUTE": "3",
                "OCR_GUARD_USER_PER_DAY": "40",
                "OCR_GUARD_GLOBAL_PER_DAY": "1200",
            },
        )
        worker_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                line_events_queue,
                batch_size=10,
                report_batch_item_failures=True,
            )
        )

        line_events_queue.grant_send_messages(ingress_fn)
        event_table.grant_write_data(ingress_fn)
        line_events_queue.grant_consume_messages(worker_fn)
        event_table.grant_read_write_data(worker_fn)
        receipts_table.grant_read_write_data(worker_fn)
        receipt_fields_table.grant_read_write_data(worker_fn)
        sessions_table.grant_read_write_data(worker_fn)
        aggregate_entries_table.grant_read_write_data(worker_fn)
        family_registry_table.grant_read_write_data(worker_fn)
        learning_rules_table.grant_read_write_data(worker_fn)
        ocr_usage_guard_table.grant_read_write_data(worker_fn)
        receipt_bucket.grant_read_write(worker_fn)
        if app_secret is not None:
            app_secret.grant_read(ingress_fn)
            app_secret.grant_read(worker_fn)

        # LIFF 画面向けの明細 API。OCR 用の依存は不要なので Layer なしの軽量な Lambda にする。
        ledger_api_fn = lambda_.Function(
            self,
            "LedgerApiFunction",
            function_name=f"{prefix}-ledger-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="app.lambda_handlers.api_handler.lambda_handler",
            code=lambda_.Code.from_asset(lambda_asset_path, exclude=lambda_asset_excludes),
            timeout=Duration.seconds(15),
            memory_size=512,
            environment={
                "DDB_TABLE_PREFIX": prefix,
                "DDB_RECEIPTS_TABLE": receipts_table.table_name,
                "DDB_FIELDS_TABLE": receipt_fields_table.table_name,
                "DDB_SESSIONS_TABLE": sessions_table.table_name,
                "DDB_AGGREGATE_TABLE": aggregate_entries_table.table_name,
                "DDB_FAMILY_TABLE": family_registry_table.table_name,
                "DDB_LEARNING_TABLE": learning_rules_table.table_name,
                "RECEIPT_BUCKET": receipt_bucket.bucket_name,
                "LINE_LOGIN_CHANNEL_ID": line_login_channel_id,
            },
        )
        # 明細の削除で OCR 由来の領収書（項目値・セッション・原本画像）もまとめて消すため、それらへの書き込み権限を付ける
        receipts_table.grant_read_write_data(ledger_api_fn)
        receipt_fields_table.grant_read_write_data(ledger_api_fn)
        sessions_table.grant_read_write_data(ledger_api_fn)
        aggregate_entries_table.grant_read_write_data(ledger_api_fn)
        family_registry_table.grant_read_data(ledger_api_fn)
        # 支払先の名寄せ（「別の支払先」の記録と、次回 OCR 用の名前の対応表）を学習ルールテーブルに保存する
        learning_rules_table.grant_read_write_data(ledger_api_fn)
        # 領収書画像の閲覧用 URL 発行（存在確認に ListBucket も必要）と、明細削除時の画像削除
        receipt_bucket.grant_read(ledger_api_fn)
        receipt_bucket.grant_delete(ledger_api_fn)

        line_webhook_api = apigwv2.HttpApi(
            self,
            "LineWebhookApi",
            api_name=f"{prefix}-line-webhook",
        )
        line_webhook_api.add_routes(
            path=line_webhook_path,
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "LineIngressIntegration",
                ingress_fn,
            ),
        )

        ledger_api_integration = apigwv2_integrations.HttpLambdaIntegration(
            "LedgerApiIntegration",
            ledger_api_fn,
        )
        line_webhook_api.add_routes(
            path="/api/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.PATCH,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=ledger_api_integration,
        )

        # --- LIFF 画面: S3 の静的ファイルと /api/* を CloudFront の同じドメインで配信（CORS 不要にする） ---
        liff_web_bucket = s3.Bucket(
            self,
            "LiffWebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        liff_security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "LiffSecurityHeaders",
            response_headers_policy_name=f"{prefix}-liff-security-headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; "
                        "script-src 'self' https://static.line-scdn.net https://*.line-scdn.net; "
                        "connect-src 'self' https://*.line.me https://*.line-scdn.net https://*.line-apps.com; "
                        "img-src 'self' data: https:; "
                        "style-src 'self'; "
                        "frame-src https://*.line.me; "
                        "base-uri 'none'; form-action 'self'; object-src 'none'"
                    ),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
            ),
        )
        ledger_api_origin = cloudfront_origins.HttpOrigin(
            f"{line_webhook_api.api_id}.execute-api.{self.region}.{self.url_suffix}",
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )
        liff_distribution = cloudfront.Distribution(
            self,
            "LiffDistribution",
            comment=f"{prefix} LIFF ledger",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            default_behavior=cloudfront.BehaviorOptions(
                origin=cloudfront_origins.S3BucketOrigin.with_origin_access_control(liff_web_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=liff_security_headers,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=ledger_api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=liff_security_headers,
                ),
            },
        )
        web_asset_path = str(Path(__file__).resolve().parents[2] / "web" / "liff")
        s3deploy.BucketDeployment(
            self,
            "LiffWebDeployment",
            destination_bucket=liff_web_bucket,
            sources=[
                s3deploy.Source.asset(web_asset_path),
                s3deploy.Source.json_data("config.json", {"liffId": liff_id, "apiBase": "/api"}),
            ],
            # 画面の更新がすぐ反映されるよう、HTML/JS/CSS はキャッシュさせない（ファイルが小さく件数も少ないため）
            cache_control=[s3deploy.CacheControl.from_string("no-cache")],
            distribution=liff_distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "LiffEndpointUrl", value=f"https://{liff_distribution.distribution_domain_name}/")
        CfnOutput(self, "LineWebhookUrl", value=f"{line_webhook_api.api_endpoint}{line_webhook_path}")
        CfnOutput(self, "LedgerApiBaseUrl", value=f"{line_webhook_api.api_endpoint}/api")
        CfnOutput(self, "LineEventsQueueUrl", value=line_events_queue.queue_url)
        CfnOutput(self, "ReceiptBucketName", value=receipt_bucket.bucket_name)
        CfnOutput(self, "FamilyRegistryTableName", value=family_registry_table.table_name)
        CfnOutput(self, "LearningRulesTableName", value=learning_rules_table.table_name)
        CfnOutput(self, "OcrUsageGuardTableName", value=ocr_usage_guard_table.table_name)
        CfnOutput(self, "DocAiDependenciesLayerArn", value=docai_dependencies_layer.layer_version_arn)
