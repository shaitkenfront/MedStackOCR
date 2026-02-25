from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_s3 as s3,
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
                    expiration=Duration.days(90),
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
                "RECEIPT_BUCKET": receipt_bucket.bucket_name,
                "RECEIPT_PREFIX": receipt_prefix,
                "APP_SECRETS_ARN": app_secret.secret_arn if app_secret else "",
                "APP_SECRETS_NAME": app_secrets_name,
                "DOC_AI_PROJECT_ID": docai_project_id,
                "DOC_AI_LOCATION": docai_location,
                "DOC_AI_PROCESSOR_ID": docai_processor_id,
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
        receipt_bucket.grant_read_write(worker_fn)
        if app_secret is not None:
            app_secret.grant_read(ingress_fn)
            app_secret.grant_read(worker_fn)

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

        CfnOutput(self, "LineWebhookUrl", value=f"{line_webhook_api.api_endpoint}{line_webhook_path}")
        CfnOutput(self, "LineEventsQueueUrl", value=line_events_queue.queue_url)
        CfnOutput(self, "ReceiptBucketName", value=receipt_bucket.bucket_name)
        CfnOutput(self, "FamilyRegistryTableName", value=family_registry_table.table_name)
