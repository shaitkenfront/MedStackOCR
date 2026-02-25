#!/usr/bin/env python3
from __future__ import annotations

import aws_cdk as cdk

from medstack_hybrid_stack import MedstackHybridStack


app = cdk.App()

MedstackHybridStack(
    app,
    "MedstackHybridStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region"),
    ),
)

app.synth()
