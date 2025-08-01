import json
import os
import urllib.parse

import pytest
from localstack_snapshot.snapshots.transformer import JsonpathTransformer
from tests.aws.services.cloudformation.conftest import skip_if_v2_provider

from localstack import config
from localstack.testing.pytest import markers
from localstack.testing.pytest.stepfunctions.utils import await_execution_terminated
from localstack.utils.strings import short_uid
from localstack.utils.sync import wait_until


@markers.aws.validated
def test_statemachine_definitionsubstitution(deploy_cfn_template, aws_client):
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__),
            "../../../templates/stepfunctions_statemachine_substitutions.yaml",
        )
    )

    assert len(stack.outputs) == 1
    statemachine_arn = stack.outputs["StateMachineArnOutput"]

    # execute statemachine
    ex_result = aws_client.stepfunctions.start_execution(stateMachineArn=statemachine_arn)

    def _is_executed():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=ex_result["executionArn"])[
                "status"
            ]
            != "RUNNING"
        )

    wait_until(_is_executed)
    execution_desc = aws_client.stepfunctions.describe_execution(
        executionArn=ex_result["executionArn"]
    )
    assert execution_desc["status"] == "SUCCEEDED"
    # sync execution is currently not supported since botocore adds a "sync-" prefix
    # ex_result = stepfunctions_client.start_sync_execution(stateMachineArn=statemachine_arn)

    assert "hello from statemachine" in execution_desc["output"]


@skip_if_v2_provider(
    reason="CFNV2:Engine During change set describe the a Ref to a not yet deployed resource returns null which is an invalid input for Fn::Split"
)
@markers.aws.validated
def test_nested_statemachine_with_sync2(deploy_cfn_template, aws_client):
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_nested_sync2.json"
        )
    )

    parent_arn = stack.outputs["ParentStateMachineArnOutput"]
    assert parent_arn

    ex_result = aws_client.stepfunctions.start_execution(
        stateMachineArn=parent_arn, input='{"Value": 1}'
    )

    def _is_executed():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=ex_result["executionArn"])[
                "status"
            ]
            != "RUNNING"
        )

    wait_until(_is_executed)
    execution_desc = aws_client.stepfunctions.describe_execution(
        executionArn=ex_result["executionArn"]
    )
    assert execution_desc["status"] == "SUCCEEDED"
    output = json.loads(execution_desc["output"])
    assert output["Value"] == 3


@markers.aws.needs_fixing
def test_apigateway_invoke(deploy_cfn_template, aws_client):
    deploy_result = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_apigateway.yaml"
        )
    )
    state_machine_arn = deploy_result.outputs["statemachineOutput"]

    execution_arn = aws_client.stepfunctions.start_execution(stateMachineArn=state_machine_arn)[
        "executionArn"
    ]

    def _sfn_finished_running():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=execution_arn)["status"]
            != "RUNNING"
        )

    wait_until(_sfn_finished_running)

    execution_result = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
    assert execution_result["status"] == "SUCCEEDED"
    assert "hello from stepfunctions" in execution_result["output"]


@markers.aws.validated
def test_apigateway_invoke_with_path(deploy_cfn_template, aws_client):
    deploy_result = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_apigateway_two_integrations.yaml"
        )
    )
    state_machine_arn = deploy_result.outputs["statemachineOutput"]

    execution_arn = aws_client.stepfunctions.start_execution(stateMachineArn=state_machine_arn)[
        "executionArn"
    ]

    def _sfn_finished_running():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=execution_arn)["status"]
            != "RUNNING"
        )

    wait_until(_sfn_finished_running)

    execution_result = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
    assert execution_result["status"] == "SUCCEEDED"
    assert "hello_with_path from stepfunctions" in execution_result["output"]


@markers.aws.only_localstack
def test_apigateway_invoke_localhost(deploy_cfn_template, aws_client):
    """tests the same as above but with the "generic" localhost version of invoking the apigateway"""
    deploy_result = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_apigateway.yaml"
        )
    )
    state_machine_arn = deploy_result.outputs["statemachineOutput"]
    api_url = deploy_result.outputs["LsApiEndpointA06D37E8"]

    # instead of changing the template, we're just mapping the endpoint here to the more generic path-based version
    state_def = aws_client.stepfunctions.describe_state_machine(stateMachineArn=state_machine_arn)[
        "definition"
    ]
    parsed = urllib.parse.urlparse(api_url)
    api_id = parsed.hostname.split(".")[0]
    state = json.loads(state_def)
    stage = state["States"]["LsCallApi"]["Parameters"]["Stage"]
    state["States"]["LsCallApi"]["Parameters"]["ApiEndpoint"] = (
        f"{config.internal_service_url()}/restapis/{api_id}"
    )
    state["States"]["LsCallApi"]["Parameters"]["Stage"] = stage

    aws_client.stepfunctions.update_state_machine(
        stateMachineArn=state_machine_arn, definition=json.dumps(state)
    )

    execution_arn = aws_client.stepfunctions.start_execution(stateMachineArn=state_machine_arn)[
        "executionArn"
    ]

    def _sfn_finished_running():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=execution_arn)["status"]
            != "RUNNING"
        )

    wait_until(_sfn_finished_running)

    execution_result = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
    assert execution_result["status"] == "SUCCEEDED"
    assert "hello from stepfunctions" in execution_result["output"]


@markers.aws.only_localstack
def test_apigateway_invoke_localhost_with_path(deploy_cfn_template, aws_client):
    """tests the same as above but with the "generic" localhost version of invoking the apigateway"""
    deploy_result = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_apigateway_two_integrations.yaml"
        )
    )
    state_machine_arn = deploy_result.outputs["statemachineOutput"]
    api_url = deploy_result.outputs["LsApiEndpointA06D37E8"]

    # instead of changing the template, we're just mapping the endpoint here to the more generic path-based version
    state_def = aws_client.stepfunctions.describe_state_machine(stateMachineArn=state_machine_arn)[
        "definition"
    ]
    parsed = urllib.parse.urlparse(api_url)
    api_id = parsed.hostname.split(".")[0]
    state = json.loads(state_def)
    stage = state["States"]["LsCallApi"]["Parameters"]["Stage"]
    state["States"]["LsCallApi"]["Parameters"]["ApiEndpoint"] = (
        f"{config.internal_service_url()}/restapis/{api_id}"
    )
    state["States"]["LsCallApi"]["Parameters"]["Stage"] = stage

    aws_client.stepfunctions.update_state_machine(
        stateMachineArn=state_machine_arn, definition=json.dumps(state)
    )

    execution_arn = aws_client.stepfunctions.start_execution(stateMachineArn=state_machine_arn)[
        "executionArn"
    ]

    def _sfn_finished_running():
        return (
            aws_client.stepfunctions.describe_execution(executionArn=execution_arn)["status"]
            != "RUNNING"
        )

    wait_until(_sfn_finished_running)

    execution_result = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
    assert execution_result["status"] == "SUCCEEDED"
    assert "hello_with_path from stepfunctions" in execution_result["output"]


@pytest.mark.skip("Terminates with FAILED on cloud; convert to SFN v2 snapshot lambda test.")
@markers.aws.needs_fixing
def test_retry_and_catch(deploy_cfn_template, aws_client):
    """
    Scenario:

    Lambda invoke (incl. 3 retries)
        => catch (Send SQS message with body "Fail")
        => next (Send SQS message with body "Success")

    The Lambda function simply raises an Exception, so it will always fail.
    It should fail all 4 attempts (1x invoke + 3x retries) which should then trigger the catch path
    and send a "Fail" message to the queue.
    """

    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/sfn_retry_catch.yaml"
        )
    )
    queue_url = stack.outputs["queueUrlOutput"]
    statemachine_arn = stack.outputs["smArnOutput"]
    assert statemachine_arn

    execution = aws_client.stepfunctions.start_execution(stateMachineArn=statemachine_arn)
    execution_arn = execution["executionArn"]

    await_execution_terminated(aws_client.stepfunctions, execution_arn)

    execution_result = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
    assert execution_result["status"] == "SUCCEEDED"

    receive_result = aws_client.sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=5)
    assert receive_result["Messages"][0]["Body"] == "Fail"


@markers.aws.validated
def test_cfn_statemachine_with_dependencies(deploy_cfn_template, aws_client):
    sm_name = f"sm_{short_uid()}"
    activity_name = f"act_{short_uid()}"
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/statemachine_machine_with_activity.yml"
        ),
        max_wait=150,
        parameters={"StateMachineName": sm_name, "ActivityName": activity_name},
    )

    rs = aws_client.stepfunctions.list_state_machines()
    statemachines = [sm for sm in rs["stateMachines"] if sm_name in sm["name"]]
    assert len(statemachines) == 1

    rs = aws_client.stepfunctions.list_activities()
    activities = [act for act in rs["activities"] if activity_name in act["name"]]
    assert len(activities) == 1

    stack.destroy()

    rs = aws_client.stepfunctions.list_state_machines()
    statemachines = [sm for sm in rs["stateMachines"] if sm_name in sm["name"]]

    assert not statemachines


@markers.aws.validated
@markers.snapshot.skip_snapshot_verify(
    paths=["$..encryptionConfiguration", "$..tracingConfiguration"]
)
def test_cfn_statemachine_default_s3_location(
    s3_create_bucket, deploy_cfn_template, aws_client, sfn_snapshot
):
    sfn_snapshot.add_transformers_list(
        [
            JsonpathTransformer("$..roleArn", "role-arn"),
            JsonpathTransformer("$..stateMachineArn", "state-machine-arn"),
            JsonpathTransformer("$..name", "state-machine-name"),
        ]
    )
    cfn_template_path = os.path.join(
        os.path.dirname(__file__),
        "../../../templates/statemachine_machine_default_s3_location.yml",
    )

    stack_name = f"test-cfn-statemachine-default-s3-location-{short_uid()}"

    file_key = f"file-key-{short_uid()}.json"
    bucket_name = s3_create_bucket()
    state_machine_template = {
        "Comment": "step: on create",
        "StartAt": "S0",
        "States": {"S0": {"Type": "Succeed"}},
    }

    aws_client.s3.put_object(
        Bucket=bucket_name, Key=file_key, Body=json.dumps(state_machine_template)
    )

    stack = deploy_cfn_template(
        stack_name=stack_name,
        template_path=cfn_template_path,
        max_wait=150,
        parameters={"BucketName": bucket_name, "ObjectKey": file_key},
    )

    stack_outputs = stack.outputs
    statemachine_arn = stack_outputs["StateMachineArnOutput"]

    describe_state_machine_output_on_create = aws_client.stepfunctions.describe_state_machine(
        stateMachineArn=statemachine_arn
    )
    sfn_snapshot.match(
        "describe_state_machine_output_on_create", describe_state_machine_output_on_create
    )

    file_key = f"2-{file_key}"
    state_machine_template["Comment"] = "step: on update"
    aws_client.s3.put_object(
        Bucket=bucket_name, Key=file_key, Body=json.dumps(state_machine_template)
    )
    deploy_cfn_template(
        stack_name=stack_name,
        template_path=cfn_template_path,
        is_update=True,
        parameters={"BucketName": bucket_name, "ObjectKey": file_key},
    )

    describe_state_machine_output_on_update = aws_client.stepfunctions.describe_state_machine(
        stateMachineArn=statemachine_arn
    )
    sfn_snapshot.match(
        "describe_state_machine_output_on_update", describe_state_machine_output_on_update
    )


@markers.aws.validated
@markers.snapshot.skip_snapshot_verify(
    paths=["$..encryptionConfiguration", "$..tracingConfiguration"]
)
def test_statemachine_create_with_logging_configuration(
    deploy_cfn_template, aws_client, sfn_snapshot
):
    sfn_snapshot.add_transformers_list(
        [
            JsonpathTransformer("$..roleArn", "role-arn"),
            JsonpathTransformer("$..stateMachineArn", "state-machine-arn"),
            JsonpathTransformer("$..name", "state-machine-name"),
            JsonpathTransformer("$..logGroupArn", "log-group-arn"),
        ]
    )
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__),
            "../../../templates/statemachine_machine_logging_configuration.yml",
        )
    )
    statemachine_arn = stack.outputs["StateMachineArnOutput"]
    describe_state_machine_result = aws_client.stepfunctions.describe_state_machine(
        stateMachineArn=statemachine_arn
    )
    sfn_snapshot.match("describe_state_machine_result", describe_state_machine_result)
